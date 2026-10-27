"""Fixed 40-story engineering acceptance across five sectors and two roles; no LLM."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from alfa_app.agent import Agent
from alfa_app.config import Settings
from alfa_app.news import canonical_url
from alfa_app.ranking import rank_event
from alfa_app.store import Store


def evaluate():
    control=json.loads((ROOT/'data/acceptance_cases_2026-10-23.json').read_text('utf-8'))
    as_of=date.fromisoformat(control['as_of'])
    profiles={uid:json.loads((ROOT/f'data/company_{uid}.json').read_text('utf-8')) for uid in control['profiles']}
    events={event['event_id']:event for event in (json.loads(line) for line in (ROOT/'data/news_current.jsonl').read_text('utf-8').splitlines())}
    original={json.loads(line)['event_id'] for line in (ROOT/'data/news_snapshot.jsonl').read_text('utf-8').splitlines()}
    rss_urls={canonical_url(item.findtext('link','')) for path in (ROOT/'data/news_sources').glob('quality_*_2026-10-23.xml')
              for item in ET.fromstring(path.read_bytes()).findall('.//item')}
    checks=[]
    def record(kind,passed,**details):checks.append(dict(kind=kind,passed=bool(passed),**details))
    for case in control['cases']:
        event=events[case['event_id']]
        record('source_provenance',event['event_id'] not in original and event['source_url'] in rss_urls
               and hashlib.sha256(event['text'].encode()).hexdigest()==event['content_sha256']
               and date.fromisoformat(event['published_at'][:10])<=as_of,
               event_id=event['event_id'],publisher=event['publisher'])
        for uid,profile in profiles.items():
            for role in ('cfo','ceo'):
                row=rank_event(profile,event,as_of,role)
                expected=uid in case['relevant_companies']
                observed=row['business_score']>=3
                record('ranking',observed==expected,event_id=event['event_id'],company_id=uid,role=role,
                       business_score=row['business_score'],expected_relevant=expected,observed_relevant=observed,
                       selection_reason=case['selection_reason'])
                role_reasons=[reason for reason in row['reasons'] if reason['company_field']=='user.selected_role']
                record('role_count',len(role_reasons)<=1,event_id=event['event_id'],company_id=uid,role=role)
    class NoLLM:
        def generate(self,*args,**kwargs):raise AssertionError('Acceptance must not call the LLM')
    with tempfile.TemporaryDirectory() as temporary:
        settings=Settings(runtime_dir=Path(temporary),allow_live=False,search_mode='hybrid')
        store=Store(Path(temporary)/'service.sqlite');agent=Agent(settings,store,NoLLM())
        event=next(item for item in events.values() if 'Региональная экономика' in item['title'])
        for uid,profile in profiles.items():
            answers=[]
            for role in ('cfo','ceo'):
                source=agent.run(profile,event,as_of,role,task='sources')
                record('source_only',source['status']=='sources_ready' and bool(source['bank_evidence'])
                       and not source['llm_request_attempted'],company_id=uid,role=role)
                gaps=agent.run(profile,event,as_of,role,task='missing_data')
                record('data_gaps',gaps['status']=='answered' and gaps['answer_origin']=='rules'
                       and not gaps['llm_request_attempted'],company_id=uid,role=role)
                answers.append(gaps['answer'])
            record('role_specific_gaps',answers[0]!=answers[1],company_id=uid)
        record('future_excluded',rank_event(profiles['U_87'],dict(event,published_at='2026-10-24T12:00:00+03:00'),as_of) is None)
    failed=[row for row in checks if not row['passed']]
    return {'as_of':str(as_of),'label_origin':control['label_origin'],'news_count':len(events),'publishers':sorted({event['publisher'] for event in events.values()}),
            'profiles':len(profiles),'ranking_cases':400,'passed':len(checks)-len(failed),'failed':len(failed),
            'new_generative_requests':0,'checks':checks}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args()
    result=evaluate();encoded=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key!='checks'},ensure_ascii=False))
    if result['failed']:print(json.dumps([row for row in result['checks'] if not row['passed']],ensure_ascii=False,indent=2));sys.exit(1)
