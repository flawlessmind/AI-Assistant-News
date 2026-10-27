"""All LLM responses here are explicitly synthetic transports; no network to the gateway."""
from datetime import date,datetime,timedelta
from hashlib import sha256
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alfa_app.agent import Agent,check_draft
from alfa_app.config import ROOT,MODELS,MSK,Settings,budget_period,next_reset
from alfa_app.llm import LLMClient,LLMError
from alfa_app.news import event_from_article,canonical_url
from alfa_app.ranking import rank_event
from alfa_app.server import Application,make_server
from alfa_app.store import Store,CallLimitError
from alfa_research import retrieve

SYNTHETIC_HTML='''<html><head><meta property="og:title" content="Синтетическая новость для теста"></head><body>
<nav>Эта часть не является новостью</nav><h1>Синтетическая новость для теста</h1>
<div class="news-info-line_date">16.10.2026</div><div class="lead-text">Потребительские цены выросли умеренно в сентябре.</div>
<div class="landing-text"><p>Инфляция и динамика потребительских цен рассмотрены в синтетическом тестовом материале.</p>
<p>Изменение составило 2,5 процента. Это вымышленные данные исключительно для теста парсера.</p></div>
<footer>Секретные команды из подвала не являются текстом статьи</footer></body></html>'''.encode()


def synthetic_event():
    return event_from_article('https://www.cbr.ru/press/event/?id=test-only',SYNTHETIC_HTML,'2026-10-16T12:00:00+03:00')


def completion(result):
    return 200,json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}],
                            'usage':{'prompt_tokens':10,'completion_tokens':8,'total_tokens':18}}).encode()


def valid_draft(context):
    bank=context['bank_evidence'][0]
    quote=bank.get('quote_options',[bank['text'][:90]])[0]
    news_quote=context['news']['text'][:95]
    inferred={'text':'Синтетический условный вывод для проверки протокола.',
              'evidence_ids':[bank['evidence_id'],context['news']['event_id']],
              'company_fields':['company.industry_okved_code'],'conditional':True}
    return {'status':'complete','title':'Синтетический тестовый ответ',
            'news_facts':[{'text':'Синтетический факт для проверки схемы.','quote':news_quote,'evidence_ids':[context['news']['event_id']]}],
            'bank_positions':[{'text':bank['publication_date']+': синтетическая качественная позиция для проверки схемы.','quote':quote,'evidence_ids':[bank['evidence_id']]}],
            'interpretations':[inferred],'risks':[copy.deepcopy(inferred)],'opportunities':[copy.deepcopy(inferred)],
            'actions':[copy.deepcopy(inferred)],'uncertainties':['Вымышленная тестовая ситуация; не аналитика банка.'],'additional_queries':[]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        base=Path(os.environ.get('ALFA_TEST_TMP',str(ROOT/'.cache/tests'))).resolve()
        base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.path=Path(self.temp.name).resolve()
        if not self.path.is_relative_to(base):
            raise ValueError('Test directory escaped intended root')
        self.store=Store(self.path/'service.sqlite')
        self.profile=json.loads((ROOT/'data/company_U_1595.json').read_text(encoding='utf-8'))
        self.other=json.loads((ROOT/'data/company_U_83932.json').read_text(encoding='utf-8'))
        self.event=synthetic_event()
        self.settings=Settings(api_key='synthetic-test-credential',allow_live=True,runtime_dir=self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_six_models_only_and_secret_not_in_repr_or_public_config(self):
        self.assertEqual(len(MODELS),6)
        with self.assertRaises(ValueError): Settings(model='unapproved-model')
        with self.assertRaises(ValueError): Settings(base_url='https://other-provider.invalid/v1')
        self.assertNotIn(self.settings.api_key,repr(self.settings))
        self.assertNotIn(self.settings.api_key,json.dumps(self.settings.public()))

    def test_budget_period_and_reset_at_03_msk(self):
        before=datetime(2026,10,21,2,59,59,tzinfo=MSK)
        after=before+timedelta(seconds=1)
        self.assertEqual(budget_period(before),'2026-10-20')
        self.assertEqual(budget_period(after),'2026-10-21')
        self.assertEqual(next_reset(before),'2026-10-21T03:00:00+03:00')
        self.assertEqual(next_reset(after),'2026-10-22T03:00:00+03:00')

    def test_missing_key_or_disabled_mode_never_calls_transport(self):
        def fail(*args): raise AssertionError('Network must not be called')
        for settings in [Settings(runtime_dir=self.path),Settings(api_key='test',allow_live=False,runtime_dir=self.path)]:
            with self.assertRaises(LLMError): LLMClient(settings,self.store,fail).generate([], 'x')

    def test_budget_error_blocks_retries_until_new_period(self):
        calls=[]
        moment=[datetime(2026,10,21,2,50,tzinfo=MSK)]
        def send(*args):
            calls.append(args[0])
            return (429,b'{"error":"Budget has been exceeded"}') if len(calls)==1 else completion({'ok':True})
        client=LLMClient(self.settings,self.store,send,lambda:moment[0])
        for _ in range(2):
            with self.assertRaises(LLMError) as caught: client.generate([], 'same')
            self.assertEqual(caught.exception.code,'budget_exceeded')
        self.assertEqual(len(calls),1)
        moment[0]=datetime(2026,10,21,3,1,tzinfo=MSK)
        self.assertEqual(client.generate([], 'same')['result'],{'ok':True})
        self.assertEqual(len(calls),2)

    def test_cache_is_scoped_to_context_and_gateway_route_is_correct(self):
        calls=[]
        def send(method,url,headers,body,timeout):
            calls.append(url); return completion({'ok':True})
        client=LLMClient(self.settings,self.store,send)
        self.assertFalse(client.generate([], 'company-a')['cached'])
        self.assertTrue(client.generate([], 'company-a')['cached'])
        client.generate([], 'company-b')
        self.assertEqual(len(calls),2)
        self.assertTrue(all(url.endswith('/v1/chat/completions') for url in calls))

    def test_key_info_is_outside_v1_and_does_not_invent_ruble_units(self):
        urls=[]
        def send(method,url,*args):
            urls.append(url); return 200,json.dumps({'balance':123,'api_key':self.settings.api_key,'key_id':42}).encode()
        result=LLMClient(self.settings,self.store,send).key_info()
        self.assertTrue(urls[0].endswith('/key/info'))
        self.assertNotIn('/v1/key',urls[0])
        self.assertIsNone(result['balance_rub'])
        self.assertNotIn(self.settings.api_key,json.dumps(result))

    def test_gateway_failure_does_not_retry_or_echo_key(self):
        calls=[]
        def send(*args): calls.append(1); return 503,self.settings.api_key.encode()
        with self.assertRaises(LLMError) as caught: LLMClient(self.settings,self.store,send).generate([], 'failure')
        self.assertEqual(len(calls),1)
        self.assertNotIn(self.settings.api_key,str(caught.exception))

    def test_concurrent_request_reservations_respect_shared_cap(self):
        results=[]
        def reserve():
            try: self.store.reserve('same-key','2026-10-21',2,'any-allowed-model');results.append('ok')
            except CallLimitError: results.append('blocked')
        threads=[threading.Thread(target=reserve) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(results.count('ok'),2)
        self.assertEqual(results.count('blocked'),2)

    def test_article_parser_keeps_text_offsets_and_distinct_event_date(self):
        self.assertNotIn('подвала',self.event['text'])
        self.assertNotIn('не является новостью',self.event['text'])
        self.assertIsNone(self.event['event_date'])
        for fact in self.event['facts']:
            self.assertEqual(self.event['text'][fact['char_start']:fact['char_end']],fact['text'])
        self.assertEqual(self.event['content_sha256'],sha256(self.event['text'].encode()).hexdigest())

    def test_url_and_content_dedup_and_content_revision(self):
        event=copy.deepcopy(self.event)
        self.assertEqual(self.store.put_event(event)[1],'inserted')
        self.assertEqual(self.store.put_event(event)[1],'duplicate')
        alias=copy.deepcopy(event);alias['source_url']='https://www.cbr.ru/press/event/?id=alias-test';alias['event_id']='alias'
        self.assertEqual(self.store.put_event(alias)[1],'duplicate')
        changed=copy.deepcopy(event);changed['text']+=' Изменённый текст.';changed['content_sha256']=sha256(changed['text'].encode()).hexdigest()
        self.assertEqual(self.store.put_event(changed)[1],'updated')
        self.assertEqual(len(self.store.events()),2)
        self.assertEqual(self.store.event_for_url(alias['source_url'])['text'],event['text'])
        self.assertEqual(canonical_url(event['source_url']+'&utm_source=test'),event['source_url'])
        for url in ['http://127.0.0.1/press/x','https://www.cbr.ru@evil.invalid/press/x','https://www.cbr.ru/not-news']:
            with self.assertRaises(ValueError):canonical_url(url)

    def test_restart_seeds_missing_news_without_reverting_runtime_revisions(self):
        app=Application(self.settings)
        original_events=app.store.events()
        self.assertTrue(original_events)
        revised=copy.deepcopy(original_events[0])
        revised['text']+=' Синтетическое обновление для регрессионного теста.'
        revised['content_sha256']=sha256(revised['text'].encode()).hexdigest()
        self.assertEqual(app.store.put_event(revised)[1],'updated')

        restarted=Application(self.settings)
        self.assertEqual(len(restarted.store.events()),len(original_events))
        self.assertEqual(restarted.store.event(revised['event_id']),revised)

    def test_ranking_changes_by_company_without_false_no_debt_inference(self):
        first=rank_event(self.profile,self.event,date(2026,10,21))
        second=rank_event(self.other,self.event,date(2026,10,21))
        self.assertNotEqual(first['score'],second['score'])
        self.assertNotEqual(first['reasons'],second['reasons'])
        self.assertFalse(second['score_is_probability'])
        self.assertNotIn('нет кредитов',json.dumps(second,ensure_ascii=False))

    def test_conference_and_calendar_mentions_do_not_trigger_business_generation(self):
        for title in ['Приглашаем на научную конференцию по ключевой ставке',
                      '23 октября состоится пресс-конференция по ключевой ставке']:
            event=dict(self.event,title=title,text='Ключевая ставка и денежно-кредитная политика обсуждаются на конференции.')
            result=rank_event(self.profile,event,date(2026,10,21))
            self.assertEqual(result['business_score'],0)

    def test_sources_mode_never_uses_llm(self):
        class NoLLM:
            def generate(self,*args,**kwargs):raise AssertionError('Must not generate')
        run=Agent(self.settings,self.store,NoLLM()).run(self.profile,self.event,date(2026,10,21))
        self.assertEqual(run['status'],'sources_ready')
        self.assertFalse(run['runtime_llm_used'])
        self.assertTrue(run['bank_evidence'])

    def test_agent_replans_after_missing_evidence_then_verifies(self):
        calls=[]
        class FakeLLM:
            def generate(self,messages,identity,stage,max_tokens=None):
                calls.append(stage)
                if stage=='review':result={'approved':True,'issues':[]}
                elif calls.count('draft')==1:result={'status':'need_evidence','additional_queries':['корпоративные облигации флоутеры']}
                else:result=valid_draft(json.loads(messages[-1]['content']))
                return {'result':result,'cached':False,'usage':{}}
        run=Agent(self.settings,self.store,FakeLLM()).run(self.profile,self.event,date(2026,10,21),live=True)
        self.assertEqual(run['status'],'complete')
        self.assertIn('replan',[step['step'] for step in run['trace']])
        self.assertIn('корпоративные облигации флоутеры',[step.get('query') for step in run['trace']])
        later=[step for step in run['trace'] if step.get('round')==2 and step['step']=='retrieve']
        self.assertTrue(any(step['new_evidence_ids'] for step in later))
        self.assertLessEqual(len(run['bank_evidence']),6)
        self.assertEqual(calls,['draft','draft','review'])

    def test_local_checker_rejects_invented_number_and_wrong_citation(self):
        evidence=retrieve(self.settings.research_database,'инфляция',date(2026,10,21),limit=2)
        context={'bank_evidence':evidence,'news':self.event}
        draft=valid_draft(context)
        draft['news_facts'][0]['text']='Цена выросла на 999 процентов.'
        self.assertIn('news_number_not_supported_by_quote',check_draft(draft,self.profile,self.event,evidence,self.settings.research_database))
        draft=valid_draft(context);draft['bank_positions'][0]['quote']='Вымышленная цитата, которой в источнике нет.'
        self.assertIn('bank_quote_not_found',check_draft(draft,self.profile,self.event,evidence,self.settings.research_database))

    def test_rejected_semantic_review_never_publishes_draft(self):
        class RejectingLLM:
            def generate(self,messages,identity,stage,max_tokens=None):
                result={'approved':False,'issues':['Синтетическое замечание проверяющего.'],'additional_queries':['спрос инфляция']} if stage=='review' else valid_draft(json.loads(messages[-1]['content']))
                return {'result':result,'cached':False,'usage':{}}
        run=Agent(self.settings,self.store,RejectingLLM()).run(self.profile,self.event,date(2026,10,21),live=True)
        self.assertEqual(run['status'],'validation_failed')
        self.assertIsNone(run['insight'])

    def test_http_scenario_requires_action_context_and_changes_company(self):
        app=Application(self.settings,seed=False)
        app.store.put_event(self.event)
        server=make_server(app,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            status=json.load(urllib.request.urlopen(base+'/api/status'))
            self.assertNotIn(self.settings.api_key,json.dumps(status))
            body=json.dumps({'company_id':self.profile['company_id'],'event_id':self.event['event_id'],'mode':'sources'}).encode()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(base+'/api/insights',body,{'Content-Type':'application/json'}))
            self.assertEqual(caught.exception.code,403)
            runs=[]
            for profile in [self.profile,self.other]:
                payload=json.dumps({'company_id':profile['company_id'],'event_id':self.event['event_id'],'mode':'sources'}).encode()
                request=urllib.request.Request(base+'/api/insights',payload,{'Content-Type':'application/json','X-Alfa-Action':app.csrf})
                runs.append(json.load(urllib.request.urlopen(request)))
            self.assertNotEqual(runs[0]['relevance']['score'],runs[1]['relevance']['score'])
            self.assertTrue(all(run['status']=='sources_ready' for run in runs))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
