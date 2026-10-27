"""Exercise a fresh no-key application over real local HTTP, without external calls."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from alfa_app.config import Settings
from alfa_app.server import Application
from alfa_app.production import make_production_server as make_server


def verify():
    with tempfile.TemporaryDirectory() as temporary:
        settings = Settings(api_key='', allow_live=False, runtime_dir=Path(temporary), search_mode='hybrid')
        app = Application(settings)
        server = make_server(app, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(path):
            with opener.open(base+path, timeout=15) as response:
                return json.load(response)

        def post(body, token=app.csrf, origin=None):
            headers = {'Content-Type': 'application/json', 'X-Alfa-Action': token}
            if origin:
                headers['Origin'] = origin
            request = urllib.request.Request(base+'/api/insights',
                      json.dumps(body, ensure_ascii=False).encode(), headers)
            with opener.open(request, timeout=15) as response:
                return json.load(response)

        try:
            status = get('/api/status')
            assert not status['llm']['key_configured'] and not status['llm']['live_enabled']
            health = get('/api/health')
            assert health['ready'] and health['search']['mode'] == 'hybrid'
            assert health['search']['chunks'] == 74 and health['search']['dimension'] == 384
            companies = get('/api/companies')
            rows = []
            for company in companies:
                for role in ('cfo', 'ceo'):
                    query = urllib.parse.urlencode({'company_id': company['company_id'],
                            'role': role, 'as_of': '2026-10-22'})
                    feed = get('/api/news?'+query)['items']
                    search = get('/api/search?'+query+'&'+urllib.parse.urlencode({
                        'q':'В магазинах изменились ценники на повседневные покупки'}))
                    assert search['search_mode'] == 'hybrid' and search['items']
                    hit = search['items'][0]
                    assert hit['event']['event_id'] == 'news_9f10b60aed544eaa32d4'
                    assert not hit['search_match']['exact_match']
                    original = next(row for row in feed if row['event']['event_id'] == hit['event']['event_id'])
                    assert hit['reasons'] == original['reasons']
                    assert hit['context_fingerprint'] == original['context_fingerprint']
                    event = next(item for item in feed if item['business_score'] > 0)
                    body = {'company_id': company['company_id'], 'role': role,
                            'event_id': event['event']['event_id'], 'as_of': '2026-10-22',
                            'mode': 'sources', 'task': 'sources'}
                    sources = post(body)
                    assert sources['status'] == 'sources_ready' and sources['bank_evidence']
                    gaps = post(dict(body, mode='live', task='missing_data',
                                question='Каких данных не хватает для полного вывода?'))
                    assert gaps['status'] == 'answered' and gaps['answer_origin'] == 'rules'
                    assert get('/api/runs/'+gaps['run_id'])['status'] == 'answered'
                    unavailable = post(dict(body, mode='live', task='insight'))
                    assert unavailable['status'] == 'generation_unavailable'
                    assert unavailable['insight'] is None
                    evidence = sources['bank_evidence'][0]
                    source = get('/api/source?'+urllib.parse.urlencode({
                             'source_id': evidence['source_id'], 'page': evidence['page']}))
                    assert evidence['text'] in source['text']
                    rows.append({'company_id': company['company_id'], 'role': role,
                                 'event_id': event['event']['event_id'],
                                 'business_score': event['business_score'],
                                 'reasons': event['reasons'],
                                 'sources_status': sources['status'],
                                 'semantic_news_match':hit['search_match'],
                                 'evidence_count': len(sources['bank_evidence']),
                                 'gaps_status': gaps['status'],
                                 'generation_status': unavailable['status'],
                                 'context_fingerprint': sources['context_fingerprint']})
            assert len({row['context_fingerprint'] for row in rows}) == len(rows)
            rejected = []
            for overrides in ({'token': 'wrong'}, {'origin': 'https://invalid.example'}):
                try:
                    post(body, **overrides)
                    raise AssertionError('Unsafe action accepted')
                except urllib.error.HTTPError as error:
                    assert error.code == 403
                    rejected.append(error.code)
            with app.store.connect() as connection:
                calls = connection.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
            assert calls == 0
            for suffix in ('q=', 'q='+('x'*501), 'q=test&role=invalid'):
                try:
                    get('/api/search?company_id=U_1595&'+suffix)
                    raise AssertionError('Invalid search accepted')
                except urllib.error.HTTPError as error:
                    assert error.code == 400
            return {'no_key': True, 'news_count': status['news_count'],
                    'search':health['search'],
                    'companies': len(companies), 'matrix': rows,
                    'unsafe_action_codes': rejected, 'new_llm_calls': calls,
                    'analysis_version': status['analysis_version']}
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = json.dumps(verify(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding='utf-8')
    print(report)
