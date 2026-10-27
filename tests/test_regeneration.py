"""Explicit retries, independent article jobs and optional caps; no external LLM."""
from dataclasses import replace
from datetime import date
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from alfa_app.agent import context_fingerprint
from alfa_app.config import ROOT,Settings
from alfa_app.llm import LLMClient
from alfa_app.server import Application
from alfa_app.store import GatewayBudgetBlocked,JobQueueFull
from test_service import completion,valid_draft


class RegenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        self.settings=Settings(api_key='synthetic-only',allow_live=True,runtime_dir=Path(self.tmp.name))
        self.app=Application(self.settings)
        self.body={'company_id':'U_1595','role':'cfo','event_id':'news_9f10b60aed544eaa32d4',
                   'mode':'live','as_of':'2026-10-23'}
    def tearDown(self):
        self.app.jobs.close();self.tmp.cleanup()

    def wait_job(self,job_id):
        for _ in range(150):
            result=self.app.jobs.public(job_id)
            if result['state'] in ('done','failed'):return result
            time.sleep(.02)
        self.fail('Job did not finish')

    def test_regeneration_bypasses_ready_result_and_model_cache(self):
        calls=[]
        def send(method,url,headers,body,timeout):
            payload=json.loads(body);context=json.loads(payload['messages'][-1]['content']);calls.append(context)
            return completion({'approved':True,'issues':[],'additional_queries':[]} if 'draft' in context else valid_draft(context))
        self.app.agent.llm=LLMClient(self.settings,self.app.store,send)
        first=self.app.analyse(self.body);self.assertEqual(first['status'],'complete')
        self.assertEqual(len(calls),2)
        cached=self.app.analyse(self.body);self.assertEqual(cached['run_id'],first['run_id'])
        new_body=dict(self.body,regenerate=True,request_id='a'*32,async_=True)
        new_body.pop('async_');new_body['async']=True
        job=self.app.dispatch_post('/api/insights',new_body);done=self.wait_job(job['job_id'])
        self.assertEqual(done['run']['status'],'complete');self.assertEqual(len(calls),4)
        self.assertNotEqual(first['run_id'],done['run']['run_id'])
        self.assertTrue(done['run']['regeneration_requested'])
        repeated=self.app.dispatch_post('/api/insights',new_body)
        self.assertEqual(repeated['job_id'],job['job_id']);self.assertEqual(len(calls),4)

    def test_regeneration_does_not_reuse_a_failed_draft(self):
        profile=self.app.profile('U_1595');event=self.app.store.event(self.body['event_id'])
        prior=self.app.agent.run(profile,event,date(2026,10,23),live=False)
        prior.update(status='validation_failed',llm_request_attempted=True);self.app.store.save_run(prior)
        self.app.agent.llm=LLMClient(self.settings,self.app.store,lambda *args:(401,b'{}'))
        with patch.object(self.app.store,'draft_candidate',side_effect=AssertionError('Do not reuse a failed draft')):
            result=self.app.analyse(dict(self.body,regenerate=True,request_id='b'*32))
        self.assertEqual(result['status'],'generation_unavailable')

    def test_different_articles_run_together_and_duplicates_join_the_active_job(self):
        lock=threading.Lock();entered=[];both=threading.Event();release=threading.Event()
        original=self.app.analyse
        def blocked(body,progress=None):
            with lock:
                entered.append(body['event_id'])
                if len(entered)==2:both.set()
            release.wait(3)
            return original(dict(body,mode='sources',regenerate=False),progress)
        second=next(event for event in self.app.store.events() if event['event_id']!=self.body['event_id'] and event['published_at'][:10]<='2026-10-23')
        with patch.object(self.app,'analyse',side_effect=blocked):
            try:
                first=self.app.jobs.submit(dict(self.body,request_id='c'*32))
                other=self.app.jobs.submit(dict(self.body,event_id=second['event_id'],request_id='d'*32))
                self.assertNotEqual(first['job_id'],other['job_id'])
                self.assertTrue(both.wait(2),'The second article must enter while the first is still running')
                duplicate=self.app.jobs.submit(dict(self.body,request_id='e'*32))
                self.assertEqual(first['job_id'],duplicate['job_id'])
            finally:release.set()
            self.assertEqual(self.wait_job(first['job_id'])['state'],'done')
            self.assertEqual(self.wait_job(other['job_id'])['state'],'done')

    def test_latest_failed_attempt_is_exposed_beside_earlier_ready_result(self):
        profile=self.app.profile('U_1595');event=self.app.store.event(self.body['event_id'])
        run=self.app.agent.run(profile,event,date(2026,10,23),live=False)
        ready=copy.deepcopy(run);ready.update(status='complete',insight={'synthetic':True},run_id='1'*32)
        self.app.store.save_run(ready)
        failed=copy.deepcopy(run);failed.update(status='validation_failed',llm_request_attempted=True,run_id='2'*32)
        self.app.store.save_run(failed)
        feed=self.app.dispatch_get('/api/news',{'company_id':['U_1595'],'role':['cfo'],'as_of':['2026-10-23']})
        row=next(row for row in feed['items'] if row['event']['event_id']==event['event_id'])
        self.assertEqual(row['latest_run_id'],failed['run_id']);self.assertEqual(row['latest_status'],'validation_failed')
        self.assertEqual(row['cached_run_id'],ready['run_id'])

    def test_no_application_call_cap_preserves_usage_and_official_budget_block(self):
        self.assertIsNone(Settings().max_calls_per_period);self.assertIsNone(Settings(max_calls_per_period=0).max_calls_per_period)
        with self.assertRaises(ValueError):Settings(max_calls_per_period=-1)
        first=self.app.store.reserve('synthetic-scope','2026-10-23',None,'allowed')
        with self.app.store.connect() as db:db.execute('UPDATE usage SET calls=1000 WHERE scope=?',('synthetic-scope',))
        second=self.app.store.reserve('synthetic-scope','2026-10-23',None,'allowed')
        self.assertEqual(self.app.store.metrics('synthetic-scope','2026-10-23')['physical_attempts'],1001)
        self.app.store.finish_call(second,'budget_exceeded',block=True)
        with self.assertRaises(GatewayBudgetBlocked):self.app.store.reserve('synthetic-scope','2026-10-23',None,'different-model')
        metrics=self.app.dispatch_get('/api/metrics',{})
        self.assertIsNone(metrics['local_call_cap']);self.assertIsNone(metrics['calls_remaining'])

    def test_regeneration_requires_a_valid_idempotency_key_and_live_mode(self):
        for values in ({'regenerate':True},{'regenerate':'yes','request_id':'a'*32},
                       {'regenerate':True,'request_id':'invalid'},
                       {'regenerate':True,'request_id':'a'*32,'mode':'sources'}):
            with self.subTest(values=values),self.assertRaises(ValueError):self.app.analysis_context(dict(self.body,**values))

    def test_default_queue_is_bounded_and_custom_capacity_can_be_larger(self):
        for index in range(16):
            _,created=self.app.store.create_job('synthetic-context-'+str(index),{'operation':'analysis'})
            self.assertTrue(created)
        with self.assertRaises(JobQueueFull):self.app.store.create_job('default-cap',{})
        self.assertTrue(self.app.store.create_job('larger-cap',{},capacity=18)[1])
