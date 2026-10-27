"""Production transport, real persistence and publisher RSS fallbacks; no paid requests."""
from datetime import date
from email.message import Message
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from alfa_app.config import Settings
from alfa_app.news import refresh, event_from_article
from alfa_app.production import make_production_server,RuntimeLock
from alfa_app.server import Application
from alfa_app.store import Store
from alfa_app.ranking import rank_event
from alfa_app.agent import normalize_feature_paths,check_draft
from test_service import valid_draft

URL='https://www.cbr.ru/press/event/?id=90001'
TEXT='Это искусственная публикация только для локального теста. В ней достаточно текста для проверки обработки RSS, сохранения происхождения и точных цитат. Она не является настоящей новостью.'


def rss(url=URL,description=TEXT):
    return f'<rss><channel><item><title>Искусственная публикация unit-теста</title><link>{url}</link><pubDate>Wed, 21 Oct 2026 10:00:00 +0300</pubDate><description><![CDATA[<p>{description}</p><script>do_not_execute()</script>]]></description></item></channel></rss>'.encode()


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
        self.app=Application(Settings(api_key='',allow_live=False,runtime_dir=self.path))

    def tearDown(self):
        self.app.jobs.close()
        self.temp.cleanup()

    def blocked_fetch(self,raw):
        def fetch(url):
            if '/rss/' in url:return raw,'utf-8'
            raise urllib.error.HTTPError(url,403,'Forbidden',Message(),io.BytesIO(b''))
        return fetch

    def test_blocked_article_imports_actual_rss_text_with_honest_provenance(self):
        raw=rss()
        report=refresh(self.app.store,self.path/'raw',feeds={'test':'https://www.cbr.ru/rss/test'},fetcher=self.blocked_fetch(raw),prefer_feed_text=False)
        self.assertEqual(report['inserted'],1);self.assertEqual(report['rss_text_imported'],1)
        self.assertEqual(report['errors'],[])
        self.assertEqual(report['warnings'][0]['http_status'],403)
        event=self.app.store.event_for_url(URL)
        self.assertEqual(event['text'],TEXT)
        self.assertFalse(event['full_article_available'])
        self.assertEqual(event['ingestion_method'],'publisher_rss_text')
        self.assertEqual(event['rss_snapshot_sha256'],sha256(raw).hexdigest())
        self.assertNotIn('do_not_execute',event['text'])
        for fact in event['facts']:
            self.assertEqual(event['text'][fact['char_start']:fact['char_end']],fact['text'])

    def test_publisher_rss_body_can_be_imported_without_requesting_an_article_page(self):
        raw=rss()
        def fetch(url):
            if '/rss/' not in url:raise AssertionError('Do not request HTML when the publisher provides RSS text')
            return raw,'utf-8'
        report=refresh(self.app.store,self.path/'raw',feeds={'test':'https://www.cbr.ru/rss/test'},fetcher=fetch)
        self.assertEqual(report['inserted'],1);self.assertEqual(report['warnings'],[])
        self.assertEqual(self.app.store.event_for_url(URL)['article_access'],'not_requested')

    def test_empty_description_cannot_be_fabricated_into_a_news_article(self):
        report=refresh(self.app.store,self.path/'raw',feeds={'test':'https://www.cbr.ru/rss/test'},fetcher=self.blocked_fetch(rss(description='')))
        self.assertEqual(report['inserted'],0);self.assertEqual(len(report['errors']),1)
        self.assertIsNone(self.app.store.event_for_url(URL))

    def test_short_rss_never_downgrades_a_full_article_including_url_aliases(self):
        full=event_from_article(URL,f'<h1>Test</h1><div class="landing-text">{TEXT}</div>'.encode(),'2026-10-21T10:00:00+03:00')
        self.app.store.put_event(full)
        alias='https://www.cbr.ru/press/event/?id=90002'
        self.app.store.put_event(dict(full,event_id='alias-test',source_url=alias))
        raw=rss(alias,description=TEXT+' Дополнение только в RSS.')
        report=refresh(self.app.store,self.path/'raw',feeds={'test':'https://www.cbr.ru/rss/test'},fetcher=self.blocked_fetch(raw))
        self.assertEqual(report['kept_full_text'],1)
        self.assertEqual(self.app.store.event_for_url(alias)['text'],TEXT)
        self.assertTrue(self.app.store.event_for_url(alias)['full_article_available'])

    def body(self):
        return {'company_id':'U_1595','role':'cfo','event_id':'news_9f10b60aed544eaa32d4',
                'mode':'sources','as_of':'2026-10-22','async':True}

    def wait_job(self,job_id):
        for _ in range(100):
            job=self.app.jobs.public(job_id)
            if job['state'] in ('done','failed'):return job
            time.sleep(0.02)
        self.fail('Job did not finish')

    def test_concurrent_duplicate_jobs_share_one_task_and_persist_result(self):
        entered,release=threading.Event(),threading.Event()
        real=self.app.analyse
        def blocked(body,progress=None):
            entered.set();release.wait(3);return real(body,progress)
        with patch.object(self.app,'analyse',side_effect=blocked) as analyse:
            first=self.app.dispatch_post('/api/insights',self.body())
            self.assertTrue(entered.wait(2))
            second=self.app.dispatch_post('/api/insights',self.body())
            self.assertEqual(first['job_id'],second['job_id'])
            release.set()
            result=self.wait_job(first['job_id'])
            self.assertEqual(result['state'],'done');self.assertEqual(analyse.call_count,1)
        self.assertEqual(result['run']['status'],'sources_ready')
        reopened=Store(self.path/'service.sqlite').job(first['job_id'])
        self.assertEqual(reopened['state'],'done')
        self.assertTrue(reopened['progress']['message'])

    def test_http_remains_responsive_while_background_analysis_is_running(self):
        entered,release=threading.Event(),threading.Event()
        real=self.app.analyse
        server=make_production_server(self.app,port=0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base='http://127.0.0.1:'+str(server.server_port)
        def blocked(body,progress=None):
            entered.set();release.wait(5);return real(body,progress)
        try:
            with patch.object(self.app,'analyse',side_effect=blocked):
                request=urllib.request.Request(base+'/api/insights',json.dumps(self.body()).encode(),
                    {'Content-Type':'application/json','X-Alfa-Action':self.app.csrf})
                with opener.open(request,timeout=2) as response:
                    self.assertEqual(response.status,202);job=json.load(response)
                self.assertTrue(entered.wait(2))
                with opener.open(base+'/api/health',timeout=2) as response:
                    self.assertTrue(json.load(response)['ready'])
                with opener.open(base+'/api/jobs/'+job['job_id'],timeout=2) as response:
                    self.assertEqual(json.load(response)['state'],'running')
                self.assertFalse(release.is_set())
                release.set();self.wait_job(job['job_id'])
            for headers in ({'Host':'attacker.invalid'},{'Origin':'https://attacker.invalid'}):
                request=urllib.request.Request(base+'/api/insights',json.dumps(self.body()).encode(),
                    {'Content-Type':'application/json','X-Alfa-Action':self.app.csrf,**headers})
                with self.assertRaises(urllib.error.HTTPError) as caught:opener.open(request,timeout=2)
                self.assertEqual(caught.exception.code,400 if 'Host' in headers else 403)
            with self.assertRaises(OSError):make_production_server(self.app,port=server.server_port)
        finally:
            release.set();server.shutdown();server.server_close();worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_runtime_lock_prevents_two_owners_and_restart_marks_interrupted_jobs(self):
        job_id,_=self.app.store.create_job('test-interrupted',self.body())
        server=make_production_server(self.app,port=0,own_runtime=True)
        try:
            other=RuntimeLock(self.path)
            with self.assertRaises(OSError):other.acquire()
            job=self.app.store.job(job_id)
            self.assertEqual(job['state'],'failed')
            self.assertIn('перезапущен',job['error']['message'])
        finally:server.server_close()
        other=RuntimeLock(self.path);other.acquire();other.close()

    def test_feedback_is_saved_idempotently_and_appears_in_real_metrics(self):
        body=self.body();body.pop('async')
        run=self.app.dispatch_post('/api/insights',body)
        feedback={'feedback_id':'a'*32,'run_id':run['run_id'],'rating':'incorrect','comment':'Искусственный отзыв теста.'}
        self.assertTrue(self.app.dispatch_post('/api/feedback',feedback)['saved'])
        self.app.dispatch_post('/api/feedback',dict(feedback,rating='helpful',comment='Исправлено.'))
        metrics=self.app.dispatch_get('/api/metrics',{})
        self.assertEqual(metrics['feedback_by_rating'],{'helpful':1})
        self.assertEqual(metrics['physical_attempts'],0)
        for change in ({'feedback_id':'bad'},{'rating':'bad'},{'comment':'x'*1001},{'run_id':'missing'}):
            with self.assertRaises(ValueError):self.app.dispatch_post('/api/feedback',dict(feedback,**change))

    def test_shutdown_does_not_start_a_review_after_an_inflight_draft_returns(self):
        entered,release=threading.Event(),threading.Event()
        stages=[]
        class Fake:
            def generate(inner,messages,identity,stage,max_tokens=None):
                stages.append(stage);entered.set();release.wait(3)
                return {'result':valid_draft(json.loads(messages[-1]['content'])),'cached':False}
        self.app.agent.llm=Fake()
        job=self.app.jobs.submit(dict(self.body(),mode='live'))
        self.assertTrue(entered.wait(2))
        closer=threading.Thread(target=self.app.jobs.close);closer.start()
        for _ in range(100):
            if self.app.jobs.closed:break
            time.sleep(0.01)
        release.set();closer.join(3)
        self.assertFalse(closer.is_alive())
        result=self.app.jobs.public(job['job_id'])
        self.assertEqual(result['state'],'failed');self.assertEqual(stages,['draft'])
        self.assertIn('между этапами',result['error']['message'])

    def test_household_mortgage_and_retail_investors_do_not_become_retail_business_news(self):
        # A borrower type and an investor type are not the client's credit
        # contract or retail sales. These are deliberately unseen fixtures.
        mortgage={'event_id':'test-mortgage','title':'Семейная ипотека на длительный срок',
                  'text':'Кредитование физических лиц: платёж зависит от ключевой ставки.',
                  'published_at':'2026-10-22T12:00:00+03:00'}
        investors=dict(mortgage,event_id='test-investors',title='Розничные инвесторы на рынке облигаций',
                       text='Инвесторы купили облигации на бирже.')
        retail=self.app.profile('U_1595');builder=self.app.profile('U_83932')
        for role in ('cfo','ceo'):
            self.assertEqual(rank_event(retail,mortgage,date(2026,10,23),role)['business_score'],0)
            self.assertGreaterEqual(rank_event(builder,mortgage,date(2026,10,23),role)['business_score'],6)
            for profile in (retail,builder):
                self.assertEqual(rank_event(profile,investors,date(2026,10,23),role)['business_score'],0)

    def test_feature_path_expansion_never_fabricates_fields_or_claims(self):
        profile=self.app.profile('U_1595')
        draft={'actions':[{'text':'Проверить, есть ли кредитный договор.',
                          'company_fields':['products.credit','made_up.credit','payments.outgoing_external_counterparty_count']}],
               'news_facts':[{'quote':'Неизменённая цитата.'}]}
        changes=normalize_feature_paths(draft,profile)
        self.assertEqual(changes,[{'from':'products.credit','to':'company.products.credit'}])
        self.assertEqual(draft['actions'][0]['company_fields'],['company.products.credit','made_up.credit','payments.outgoing_external_counterparty_count'])
        self.assertEqual(draft['actions'][0]['text'],'Проверить, есть ли кредитный договор.')
        self.assertEqual(draft['news_facts'][0]['quote'],'Неизменённая цитата.')
        self.assertNotIn('interpretations',draft)
