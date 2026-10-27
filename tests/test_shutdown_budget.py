"""Bounded shutdown and concurrent call budgets with a synthetic gateway."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alfa_app.config import ROOT, Settings
from alfa_app.llm import LLMClient, LLMError
from alfa_app.production import ProductionServer
from alfa_app.server import Application
from alfa_app.store import Store
from test_service import completion, valid_draft


class ShutdownBudgetTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        self.settings=Settings(api_key='synthetic-only',allow_live=True,runtime_dir=Path(self.temp.name),max_calls_per_period=5)
        self.store=Store(Path(self.temp.name)/'service.sqlite')
    def tearDown(self):self.temp.cleanup()

    def test_compose_grace_covers_one_maximum_inflight_call_and_cleanup(self):
        grace=int(re.search(r'stop_grace_period:\s*(\d+)s',(ROOT/'compose.yaml').read_text(encoding='utf-8'))[1])
        self.assertGreaterEqual(grace,Settings(timeout=180).timeout+30)

    def test_jobs_stop_before_waiting_for_synchronous_http_handlers(self):
        observations=[]
        jobs=SimpleNamespace(closed=False)
        jobs.request_stop=lambda:setattr(jobs,'closed',True)
        server=object.__new__(ProductionServer)
        server.stopped=False;server.application=SimpleNamespace(jobs=jobs)
        server.server=SimpleNamespace(task_dispatcher=SimpleNamespace(shutdown=lambda:observations.append(jobs.closed)),
                                      close=lambda:None,active_channels={})
        server.shutdown()
        self.assertEqual(observations,[True])

    def test_cli_sigterm_and_sigint_request_stop_before_server_cleanup(self):
        import signal
        import alfa_app.__main__ as cli
        for signum in (signal.SIGTERM,signal.SIGINT):
            with self.subTest(signal=signum):
                handlers={};observed=[]
                def make_server(application,*args,**kwargs):
                    def close():
                        observed.append(application.jobs.closed);application.jobs.close()
                    return SimpleNamespace(server_port=0,serve_forever=lambda:handlers[signum](signum,None),server_close=close)
                settings=Settings(runtime_dir=Path(self.temp.name))
                with patch.object(cli.Settings,'from_env',return_value=settings),patch.object(cli,'make_production_server',side_effect=make_server),\
                     patch.object(cli.signal,'signal',side_effect=lambda key,value:handlers.__setitem__(key,value)),\
                     patch('sys.argv',['alfa_app','serve']),patch('builtins.print'):
                    self.assertEqual(cli.main(),0)
                self.assertEqual(observed,[True])

    def test_restart_marks_reserved_call_unknown_without_refunding_or_resending(self):
        call=self.store.reserve('synthetic-scope','2026-10-24',5,'synthetic-model')
        job,_=self.store.create_job('synthetic-context',{'request_id':'a'*32})
        self.store.update_job(job,state='running')
        self.store.cache_set('old-complete-draft',{'kept':True})
        self.assertEqual(self.store.recover_jobs(),1)
        with self.store.connect() as db:
            status,diagnostics=db.execute('SELECT status,diagnostics FROM calls WHERE id=?',(call,)).fetchone()
            self.assertEqual(status,'interrupted_outcome_unknown')
            self.assertTrue(json.loads(diagnostics)['request_outcome_unknown'])
            self.assertEqual(db.execute('SELECT calls FROM usage').fetchone()[0],1)
        self.assertEqual(self.store.create_job('synthetic-context',{'request_id':'a'*32}),(job,False))
        self.assertEqual(self.store.cache_get('old-complete-draft'),{'kept':True})
        self.assertIn('бюджет',self.store.job(job)['error']['message'])

    def test_concurrent_attempts_cannot_exceed_the_configured_shared_cap(self):
        sends=[];lock=threading.Lock();start=threading.Barrier(20)
        def transport(*args):
            with lock:sends.append(1)
            return completion({'ok':True})
        def attempt(index):
            start.wait()
            try:
                LLMClient(self.settings,self.store,transport).generate([],'different-'+str(index))
                return 'ok'
            except LLMError as error:
                self.assertEqual(error.code,'local_call_limit')
                self.assertIn('предел',error.message)
                return 'limited'
        with ThreadPoolExecutor(max_workers=20) as pool:results=list(pool.map(attempt,range(20)))
        self.assertEqual(results.count('ok'),5);self.assertEqual(results.count('limited'),15)
        self.assertEqual(len(sends),5)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT calls FROM usage').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0],5)

    def test_cached_result_is_available_at_cap_and_review_cannot_bypass_it(self):
        sends=[]
        def transport(*args):sends.append(1);return completion({'ok':True})
        settings=Settings(api_key='synthetic-only',allow_live=True,runtime_dir=Path(self.temp.name),max_calls_per_period=1)
        client=LLMClient(settings,self.store,transport)
        client.generate([],'same')
        self.assertTrue(client.generate([],'same')['cached'])
        with self.assertRaises(LLMError) as caught:client.generate([],'review-identity','review')
        self.assertEqual(caught.exception.code,'local_call_limit');self.assertEqual(len(sends),1)
