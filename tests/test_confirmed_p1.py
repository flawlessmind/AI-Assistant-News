"""Confirmed P1 regressions; synthetic credentials and transports only."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
from alfa_app.config import Settings,ROOT
from alfa_app.llm import LLMClient,LLMError
from alfa_app.server import Application
from alfa_app.store import Store
from test_service import completion


class PhysicalAttemptTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache/tests').mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        self.settings=Settings(api_key='offline-test-key',allow_live=True,runtime_dir=Path(self.tmp.name))
        self.store=Store(Path(self.tmp.name)/'service.sqlite')
    def tearDown(self):self.tmp.cleanup()

    def test_timeout_after_gateway_acceptance_never_resends(self):
        accepted=[]
        def transport(*args):
            accepted.append(json.loads(args[3]));raise TimeoutError('Synthetic timeout after acceptance')
        client=LLMClient(self.settings,self.store,transport)
        with self.assertRaises(LLMError) as caught:client.generate([],'same-context')
        self.assertEqual(len(accepted),1)
        self.assertEqual(caught.exception.code,'request_outcome_unknown')
        self.assertTrue(caught.exception.public()['diagnostics']['request_outcome_unknown'])
        self.assertIn('оплачен',caught.exception.message)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM calls').fetchall(),[('network_outcome_unknown',)])
            self.assertEqual(db.execute('SELECT calls FROM usage').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM llm_cache').fetchone()[0],0)

    def test_http_server_and_rate_errors_are_single_journaled_attempts(self):
        for status in (408,429,500,502,503,504):
            with self.subTest(status=status):
                sends=[]
                def transport(*args):sends.append(1);return status,b'{}'
                with self.assertRaises(LLMError) as caught:
                    LLMClient(self.settings,self.store,transport).generate([],str(status))
                self.assertEqual(sends,[1]);self.assertEqual(caught.exception.code,'request_outcome_unknown')
                self.assertEqual(caught.exception.diagnostics['http_status'],status)

    def test_explicit_new_attempt_and_cache_are_distinct_from_transport_retry(self):
        sends=[]
        def transport(*args):
            sends.append(1)
            if len(sends)==1:raise urllib.error.URLError('Synthetic lost connection')
            return completion({'ok':True})
        client=LLMClient(self.settings,self.store,transport)
        with self.assertRaises(LLMError):client.generate([],'original')
        self.assertEqual(len(sends),1)
        self.assertEqual(client.generate([],'user-requested-new')['result'],{'ok':True})
        self.assertTrue(client.generate([],'user-requested-new')['cached'])
        self.assertEqual(len(sends),2)


class QuestionJobContextTests(unittest.TestCase):
    def test_different_questions_modes_tasks_and_parents_have_complete_public_context(self):
        (ROOT/'.cache/tests').mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests') as folder:
            app=Application(Settings(runtime_dir=Path(folder)))
            body={'company_id':'U_1595','event_id':'news_9f10b60aed544eaa32d4','role':'cfo',
                  'as_of':'2026-10-23','mode':'sources','task':'explain','question':'Первый вопрос?'}
            release=threading.Event()
            def analyse(body,progress=None):release.wait(3);return {'run_id':'synthetic-unused'}
            try:
                with patch.object(app,'analyse',side_effect=analyse),patch.object(app.jobs,'public',wraps=app.jobs.public):
                    first=app.jobs.submit(body)
                    second=app.jobs.submit(dict(body,question='Второй вопрос?'))
                    other_mode=app.jobs.submit(dict(body,mode='live'))
                    other_task=app.jobs.submit(dict(body,task='sources'))
                    duplicate=app.jobs.submit(body)
                    self.assertEqual(first['job_id'],duplicate['job_id'])
                    self.assertEqual(len({job['job_id'] for job in (first,second,other_mode,other_task)}),4)
                    for job in (first,second,other_mode,other_task):
                        self.assertIn('mode',job['context']);self.assertIn('parent_run_id',job['context'])
                    self.assertEqual(first['context']['question'],'Первый вопрос?')
                    self.assertEqual(second['context']['question'],'Второй вопрос?')
                    self.assertIsNone(first['context']['parent_run_id'])
                    release.set()
            finally:release.set();app.jobs.close()
