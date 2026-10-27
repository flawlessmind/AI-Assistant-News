"""Offline audit regressions. Synthetic credentials and local transports only."""
import copy
from dataclasses import replace
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from alfa_app.agent import (Agent, QUESTION_SYSTEM, REVIEW_SYSTEM, SYSTEM,
                            check_answer, check_draft, context_fingerprint, research_queries)
from alfa_app.config import KEY_INFO_URL, MSK, OFFICIAL_BASES, ROOT, Settings
from alfa_app.llm import LLMClient, LLMError, transport
from alfa_app.store import Store
from alfa_app.tasks import missing_data_answer
from alfa_app.server import Application


def completion(value):
    return 200,json.dumps({'choices':[{'finish_reason':'stop','message':{
        'content':json.dumps(value,ensure_ascii=False)}}]}).encode()


class BackendAuditTests(unittest.TestCase):
    def setUp(self):
        temporary_root=ROOT/'.cache/tests'
        temporary_root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'service.sqlite')
        self.settings=Settings(api_key='synthetic-audit-credential',allow_live=True,
                               runtime_dir=Path(self.temp.name),max_rounds=1)
        self.profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        self.event={'event_id':'synthetic-audit-event','title':'Решение по ключевой ставке',
                    'text':'Ключевая ставка сохранена. Условия финансирования требуют дальнейшего наблюдения.',
                    'published_at':'2026-10-21','event_date':None,
                    'source_url':'https://www.cbr.ru/press/event/?id=synthetic-audit',
                    'content_sha256':'synthetic-audit-content'}

    def test_both_official_endpoints_are_accepted_and_used(self):
        for base in OFFICIAL_BASES:
            with self.subTest(base=base):
                settings=replace(self.settings,base_url=base+'/')
                urls=[]
                def send(method,url,*args):
                    urls.append(url)
                    return completion({'ok':True})
                result=LLMClient(settings,self.store,send).generate([],base)
                self.assertFalse(result['cached'])
                self.assertEqual(urls,[base+'/chat/completions'])

    def test_credentials_only_go_to_exact_official_base_urls(self):
        invalid=[
            'http://changellenge.up.railway.app/v1',
            'https://changellenge.up.railway.app.evil.invalid/v1',
            'https://api.alfahack-gateway26.ru.evil.invalid/v1',
            'https://user@api.alfahack-gateway26.ru/v1',
            'https://api.alfahack-gateway26.ru@evil.invalid/v1',
            'https://api.alfahack-gateway26.ru:443/v1',
            'https://api.alfahack-gateway26.ru/v1/chat/completions',
            'https://api.alfahack-gateway26.ru/v1//',
            'https://api.alfahack-gateway26.ru/v1?x=1',
            'https://api.alfahack-gateway26.ru/v1#x',
            '\nhttps://api.alfahack-gateway26.ru/v1',
            'https://api.alfahack-gateway26.ru/v1\n',
        ]
        for base in invalid:
            with self.subTest(base=base),self.assertRaises(ValueError):
                replace(self.settings,base_url=base)

    def test_key_info_uses_only_documented_primary_endpoint(self):
        urls=[]
        def send(method,url,*args):
            urls.append((method,url))
            return 200,b'{"balance":12}'
        client=LLMClient(replace(self.settings,base_url=OFFICIAL_BASES[1]),self.store,send)
        self.assertIsNone(client.key_info()['balance_rub'])
        self.assertEqual(urls,[('GET',KEY_INFO_URL)])

    def test_switching_endpoint_and_model_does_not_reset_local_cap(self):
        calls=[]
        def send(*args):
            calls.append(1)
            return completion({'ok':True})
        first=replace(self.settings,max_calls_per_period=1)
        LLMClient(first,self.store,send).generate([],'first')
        other=replace(first,base_url=OFFICIAL_BASES[1],model='z-ai/glm-5.3-flash')
        with self.assertRaises(LLMError) as caught:
            LLMClient(other,self.store,send).generate([],'different-context')
        self.assertEqual(caught.exception.code,'local_call_limit')
        self.assertEqual(calls,[1])

    def test_gateway_budget_block_is_shared_across_endpoints_and_models(self):
        calls=[]
        def send(*args):
            calls.append(1)
            return 429,b'{"error":"Budget has been exceeded"}'
        clients=[LLMClient(self.settings,self.store,send),LLMClient(replace(self.settings,
            base_url=OFFICIAL_BASES[1],model='z-ai/glm-5.3-flash'),self.store,send)]
        for index,client in enumerate(clients):
            with self.assertRaises(LLMError) as caught:
                client.generate([],str(index))
            self.assertEqual(caught.exception.code,'budget_exceeded')
        self.assertEqual(calls,[1])

    def test_explicit_new_attempt_after_uncertain_result_uses_actual_period(self):
        moment=[datetime(2026,10,22,2,59,59,tzinfo=MSK)]
        attempts=[]
        def send(*args):
            attempts.append(1)
            if len(attempts)==1:
                moment[0]=datetime(2026,10,22,3,0,0,tzinfo=MSK)
                return 503,b'{}'
            return completion({'ok':True})
        client=LLMClient(replace(self.settings,max_calls_per_period=1),self.store,send,lambda:moment[0])
        with self.assertRaises(LLMError) as caught:client.generate([],'reset-boundary')
        self.assertEqual(caught.exception.code,'request_outcome_unknown')
        self.assertEqual(attempts,[1])
        self.assertEqual(client.generate([],'explicit-new-attempt')['result'],{'ok':True})
        with self.store.connect() as db:
            usage=db.execute('SELECT period,calls FROM usage ORDER BY period').fetchall()
        self.assertEqual(usage,[('2026-10-21',1),('2026-10-22',1)])

    def test_authentication_and_temporary_http_errors_have_single_counted_attempts(self):
        for status,attempts,code in [(401,1,'authentication_error'),(403,1,'authentication_error'),
                                     (429,1,'request_outcome_unknown'),(503,1,'request_outcome_unknown')]:
            with self.subTest(status=status):
                calls=[]
                def send(*args):
                    calls.append(1)
                    return status,b'{"error":"synthetic transport failure"}'
                client=LLMClient(self.settings,self.store,send)
                with patch('alfa_app.llm.time.sleep'),self.assertRaises(LLMError) as caught:
                    client.generate([],str(status))
                self.assertEqual(caught.exception.code,code)
                self.assertEqual(caught.exception.diagnostics['http_status'],status)
                self.assertEqual(len(calls),attempts)
                with self.store.connect() as db:
                    recorded=db.execute('SELECT COUNT(*) FROM calls WHERE status IN (?,?)',
                        ('http_'+str(status),'retryable_http_'+str(status))).fetchone()[0]
                self.assertEqual(recorded,attempts)

    def test_missing_data_respects_retail_subsector_construction_and_unknown_activity(self):
        cases=[('47.71','одежды',['строительного']),
               ('47.11','категориям товаров',['одежды','строительного']),
               ('41.20.1','строительного проекта',['одежды','Ассортимент']),
               ('62.01','основной деятельности',['одежды','строительного','Ассортимент']),
               ('','основной деятельности',['одежды','строительного'])]
        for code,expected,forbidden in cases:
            with self.subTest(code=code):
                profile=copy.deepcopy(self.profile)
                profile['company']['industry_okved_code']=code
                answer=missing_data_answer(profile,'ceo')
                text=json.dumps(answer,ensure_ascii=False)
                self.assertIn(expected,text)
                for phrase in forbidden:
                    self.assertNotIn(phrase,text)
                self.assertEqual(answer['origin'],'rules')
                self.assertIn('есть ли действующий долг',text)

    def test_unknown_sector_search_fallback_does_not_invent_construction(self):
        profile=copy.deepcopy(self.profile)
        profile['company']['industry_okved_code']='62.01'
        queries,_=research_queries([],profile)
        self.assertTrue(queries)
        self.assertFalse(any('строитель' in query or 'розничн' in query for query in queries))

    def test_general_prompts_do_not_impose_the_old_demonstration_on_other_sectors(self):
        for prompt in [SYSTEM,REVIEW_SYSTEM,QUESTION_SYSTEM]:
            for phrase in ['одежд','автомобил','июл','сентябр']:
                self.assertNotIn(phrase,prompt.lower())
        contexts=[]
        class NoPaidModel:
            def generate(inner,messages,*args):
                contexts.append(json.loads(messages[-1]['content']))
                return {'result':{'status':'need_evidence','additional_queries':[]},'cached':False}
        for code,name in [('41.20.1','Строительство'),('62.01','Разработка программного обеспечения')]:
            profile=copy.deepcopy(self.profile)
            profile['company'].update(industry_okved_code=code,industry_name=name)
            run=Agent(self.settings,self.store,NoPaidModel()).run(profile,self.event,date(2026,10,22),live=True)
            self.assertIsNone(run['insight'])
            self.assertEqual(contexts[-1]['profile']['company']['industry_name'],name)

    def test_cache_fingerprint_changes_with_actual_loaded_prompt(self):
        args=(self.settings,self.profile,self.event,date(2026,10,22),'cfo')
        before=context_fingerprint(*args)[0]
        with patch('alfa_app.agent.SYSTEM',SYSTEM+'\nДополнительная проверка области применимости.'):
            self.assertNotEqual(context_fingerprint(*args)[0],before)

    def test_verified_okved_metadata_is_not_a_financial_effect(self):
        source={'evidence_id':'synthetic-source','source_id':'synthetic-source','page':1,
                'publication_date':'2026-10-21','report_period':'7М26','title':'Итоги 7М26',
                'text':'Банк отмечает сохранение неопределённости во внешней среде.'}
        claim={'text':'Проверить применимость к ОКВЭД 47.71.','conditional':True,
               'company_fields':['company.industry_okved_code'],'evidence_ids':[source['evidence_id']]}
        draft={'status':'complete','title':'Проверить условия','uncertainties':['Влияние не установлено.'],
               'news_facts':[],'bank_positions':[],
               **{group:[copy.deepcopy(claim)] for group in ['interpretations','risks','opportunities','actions']}}
        issues=check_draft(draft,self.profile,self.event,[source],self.settings.research_database)
        self.assertNotIn('unsupported_quantified_company_effect',issues)
        draft['risks'][0]['text']='Выручка может снизиться на 15 процентов.'
        self.assertIn('unsupported_quantified_company_effect',
                      check_draft(draft,self.profile,self.event,[source],self.settings.research_database))

    def test_explanation_news_fact_rejects_a_number_absent_from_its_real_quote(self):
        answer={'status':'answered','title':'Решение по ставке','summary':'Уровень ставки не уточнён.',
                'points':[{'kind':'source_fact','text':'Ключевая ставка повышена до 99 процентов.',
                           'quote':self.event['text'],'evidence_ids':[self.event['event_id']]}],
                'uncertainties':['Индивидуальные договоры неизвестны.']}
        issues=check_answer(answer,self.profile,self.event,[],self.settings.research_database)
        self.assertIn('news_number_not_supported_by_quote',issues)
        answer['points'][0]['text']='Ключевая ставка сохранена.'
        self.assertNotIn('news_number_not_supported_by_quote',
                        check_answer(answer,self.profile,self.event,[],self.settings.research_database))

    def test_explanation_cache_is_bound_to_its_current_parent_content_and_approval(self):
        contexts=[]
        class NoPaidModel:
            def generate(inner,messages,identity,stage,*args):
                if stage=='review':
                    return {'result':{'approved':True,'issues':[],'additional_queries':[]},'cached':False}
                context=json.loads(messages[-1]['content'])
                contexts.append((identity,context))
                return {'result':{'status':'answered','title':'Пояснение',
                    'summary':context['parent_insight']['title'],
                    'points':[{'kind':'limitation','text':'Влияние на компанию требует проверки.'}],
                    'uncertainties':['Данные компании неполны.']},'cached':False}
        app=Application(self.settings,llm=NoPaidModel(),seed=False)
        app.store.put_event(self.event)
        parents=[]
        for title in ['Первый сценарий','Другой сценарий']:
            parent=app.agent.run(self.profile,self.event,date(2026,10,22))
            evidence_id=parent['bank_evidence'][0]['evidence_id']
            parent.update(status='complete',insight={'title':title,
                'bank_positions':[{'evidence_ids':[evidence_id]}],
                'interpretations':[],'risks':[],'actions':[]})
            app.store.save_run(parent)
            parents.append(parent)
        body={'company_id':self.profile['company_id'],'event_id':self.event['event_id'],
              'role':'cfo','mode':'live','question':'Поясни готовый вывод',
              'task':'explain','as_of':'2026-10-22','parent_run_id':parents[0]['run_id']}
        first=app.dispatch_post('/api/insights',body)
        repeated=app.dispatch_post('/api/insights',body)
        self.assertEqual(first['status'],'answered')
        self.assertTrue(repeated['restored_from_cache'])
        self.assertEqual(repeated['run_id'],first['run_id'])
        self.assertEqual(len(contexts),1)
        body['parent_run_id']=parents[1]['run_id']
        second=app.dispatch_post('/api/insights',body)
        self.assertEqual(second['status'],'answered')
        self.assertNotEqual(first['context_fingerprint'],second['context_fingerprint'])
        self.assertEqual(len(contexts),2)
        self.assertNotEqual(contexts[0][0],contexts[1][0])
        self.assertEqual(second['answer']['summary'],'Другой сценарий')
        parents[0]['insight']['title']='Изменённый сценарий'
        app.store.save_run(parents[0])
        historical=app.dispatch_get('/api/runs/'+first['run_id'],{})
        self.assertEqual(historical['status'],'stale_result')
        self.assertIsNone(historical['answer'])
        parents[1]['status']='validation_failed'
        app.store.save_run(parents[1])
        self.assertEqual(app.dispatch_get('/api/runs/'+second['run_id'],{})['status'],'stale_result')
        with patch.object(app.agent,'run',side_effect=AssertionError('Revoked parent must not trigger generation')):
            with self.assertRaises(ValueError):
                app.dispatch_post('/api/insights',body)


class AuthorizationRedirectTests(unittest.TestCase):
    def test_http_redirect_does_not_forward_the_bearer_header(self):
        seen=[]
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                seen.append((self.path,self.headers.get('Authorization')))
                self.send_response(int(self.path.removeprefix('/')))
                self.send_header('Location','/redirect-target')
                self.send_header('Content-Length','0')
                self.end_headers()
            def do_GET(self):
                seen.append((self.path,self.headers.get('Authorization')))
                self.send_response(200)
                self.end_headers()
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),RedirectHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            for expected in [301,302,303,307,308]:
                with self.subTest(status=expected):
                    seen.clear()
                    status,_=transport('POST',f'http://127.0.0.1:{server.server_port}/{expected}',
                                       {'Authorization':'Bearer synthetic-only'},b'{}',2)
                    self.assertEqual(status,expected)
                    self.assertEqual(seen,[(f'/{expected}','Bearer synthetic-only')])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__=='__main__':
    unittest.main()
