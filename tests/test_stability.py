"""Attempt identity, concurrency and HTTP regressions. All LLM traffic is synthetic."""
from concurrent.futures import ThreadPoolExecutor
import json
import http.client
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alfa_app.config import ROOT, Settings
from alfa_app.llm import LLMClient
from alfa_app.server import Application
from alfa_app.server import make_server
from alfa_app.production import make_production_server
from alfa_app.production import ProductionServer
from alfa_app.jobs import JobsUnavailable
from alfa_app.store import Store
from test_service import completion, valid_draft


class AttemptIdentityTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache/tests').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.cache/tests')
        self.app = Application(Settings(api_key='synthetic-only', allow_live=True,
                                        runtime_dir=Path(self.temp.name)))
        self.body = dict(company_id='U_1595', role='cfo', event_id='news_9f10b60aed544eaa32d4',
                         mode='live', as_of='2026-10-23')

    def tearDown(self):
        self.app.jobs.close()
        self.temp.cleanup()

    def wait_job(self, job_id):
        for _ in range(250):
            job = self.app.jobs.public(job_id)
            if job['state'] in ('done', 'failed'):
                return job
            time.sleep(.02)
        self.fail('Synthetic job did not finish')

    def test_joined_request_id_remains_idempotent_after_failure_and_restart(self):
        store = self.app.store
        first, _ = store.create_job('same-context', {'request_id': 'a' * 32})
        joined, created = store.create_job('same-context', {'request_id': 'b' * 32})
        self.assertEqual(first, joined)
        self.assertFalse(created)
        store.update_job(first, state='failed', error={'message': 'Synthetic interrupted task'})
        reopened = Store(store.path)
        retried, created = reopened.create_job('same-context', {'request_id': 'b' * 32})
        self.assertEqual(retried, first)
        self.assertFalse(created)
        fresh, created = reopened.create_job('same-context', {'request_id': 'c' * 32})
        self.assertNotEqual(fresh, first)
        self.assertTrue(created)

    def test_same_id_is_idempotent_for_every_persistent_job_state(self):
        for state in ('queued', 'running', 'done', 'failed'):
            with self.subTest(state=state):
                context = 'context-' + state
                request = {'request_id': 'd' * 32}
                first, _ = self.app.store.create_job(context, request)
                self.app.store.update_job(first, state=state)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    copies = list(pool.map(lambda _: self.app.store.create_job(context, request), range(12)))
                self.assertEqual(copies, [(first, False)] * 12)

    def test_synchronous_network_resend_does_not_repeat_a_paid_timeout(self):
        sends = []
        def timeout(*args):
            sends.append(1)
            raise TimeoutError('Synthetic response lost after acceptance')
        self.app.agent.llm = LLMClient(self.app.settings, self.app.store, timeout)
        body = dict(self.body, request_id='c' * 32)
        first = self.app.dispatch_post('/api/insights', body)
        self.assertEqual(first['status'], 'generation_unavailable')
        second = self.app.dispatch_post('/api/insights', body)
        self.assertEqual(second['run_id'], first['run_id'])
        self.assertEqual(len(sends), 1)

    def test_executor_rejection_does_not_leave_a_queued_ghost(self):
        with patch.object(self.app.jobs.executor, 'submit', side_effect=RuntimeError('Synthetic executor shutdown')):
            with self.assertRaises(JobsUnavailable):
                self.app.jobs.submit(dict(self.body, request_id='e' * 32))
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs').fetchall(), [('failed',)])

    def test_storage_error_before_work_is_recorded_without_generation(self):
        import sqlite3
        job_id, _ = self.app.store.create_job('synthetic-unstarted-job', dict(self.body, operation='analysis'))
        original = self.app.store.job
        reads = []
        def interrupted_read(value):
            reads.append(value)
            if len(reads) == 1:
                raise sqlite3.OperationalError('Synthetic short storage outage')
            return original(value)
        with patch.object(self.app.store, 'job', side_effect=interrupted_read), patch.object(self.app, 'analyse') as analyse:
            self.app.jobs.work(job_id)
            analyse.assert_not_called()
        self.assertEqual(self.app.store.job(job_id)['state'], 'failed')

    def test_non_boolean_async_cannot_fall_through_to_paid_synchronous_analysis(self):
        with patch.object(self.app, 'analyse') as analyse:
            for value in ('true', 1, [], {}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.app.dispatch_post('/api/insights', dict(self.body, **{'async': value}))
            analyse.assert_not_called()

    def test_concurrent_regenerations_have_separate_attempts_and_no_duplicate_sends(self):
        release = threading.Event()
        barrier = threading.Barrier(10)
        executions = []
        original = self.app.analyse
        def blocked(body, progress=None):
            executions.append(body['request_id'])
            release.wait(3)
            return original(dict(body, mode='sources', regenerate=False), progress)
        def submit(index):
            barrier.wait()
            return self.app.jobs.submit(dict(self.body, regenerate=True, request_id=('a' if index % 2 else 'b') * 32))
        with patch.object(self.app, 'analyse', side_effect=blocked):
            try:
                with ThreadPoolExecutor(max_workers=10) as pool:
                    jobs = list(pool.map(submit, range(10)))
                self.assertEqual(len({job['job_id'] for job in jobs}), 2)
            finally:
                release.set()
            for job in jobs:
                self.assertEqual(self.wait_job(job['job_id'])['state'], 'done')
            self.assertCountEqual(executions, ['a' * 32, 'b' * 32])

    def test_regeneration_while_active_is_independent_but_same_nonce_is_deduplicated(self):
        release = threading.Event()
        entered = threading.Event()
        lock = threading.Lock()
        drafts = []
        sends = []

        def transport(method, url, headers, body, timeout):
            context = json.loads(json.loads(body)['messages'][-1]['content'])
            with lock:
                sends.append(context)
                if 'draft' not in context:
                    drafts.append(context)
                    if len(drafts) == 2:
                        entered.set()
            if 'draft' not in context:
                release.wait(3)
            return completion({'approved': True, 'issues': [], 'additional_queries': []}
                              if 'draft' in context else valid_draft(context))

        self.app.agent.llm = LLMClient(self.app.settings, self.app.store, transport)
        original = dict(self.body, request_id='a' * 32)
        fresh = dict(self.body, regenerate=True, request_id='b' * 32)
        try:
            first = self.app.jobs.submit(original)
            regenerated = self.app.jobs.submit(fresh)
            duplicate = self.app.jobs.submit(fresh)
            self.assertNotEqual(first['job_id'], regenerated['job_id'])
            self.assertEqual(regenerated['job_id'], duplicate['job_id'])
            self.assertTrue(entered.wait(2), 'Both independent attempts must reach the draft stage')
        finally:
            release.set()
        one = self.wait_job(first['job_id'])
        two = self.wait_job(regenerated['job_id'])
        self.assertEqual(one['run']['status'], 'complete')
        self.assertEqual(two['run']['status'], 'complete')
        self.assertNotEqual(one['run']['run_id'], two['run']['run_id'])
        self.assertEqual(len(sends), 4)
        self.assertEqual(self.app.jobs.submit(fresh)['job_id'], regenerated['job_id'])
        self.assertEqual(len(sends), 4)


class HTTPFailureTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            settings=SimpleNamespace(allowed_hosts=('127.0.0.1', 'localhost', '::1'), allow_remote_bind=False),
            csrf='synthetic-action-token', jobs=SimpleNamespace(close=lambda: None, request_stop=lambda: None),
            dispatch_get=lambda *args: {'ready': True}, dispatch_post=lambda *args: {'accepted': True})

    def exchange(self, server, method='GET', path='/api/health', raw=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def servers(self):
        return (make_server, make_production_server)

    def test_rejected_small_body_is_drained_before_closing_the_connection(self):
        server=make_server(self.app,port=0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            for method in ('PUT','POST'):
                for _ in range(6):
                    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                    try:
                        payload=b'{"padding":"'+b'x'*2048+b'"}'
                        connection.putrequest(method,'/api/health')
                        connection.putheader('Content-Length',str(len(payload)))
                        connection.putheader('Content-Type','application/json')
                        connection.endheaders()
                        time.sleep(.005)  # The server can reject the headers before this packet arrives.
                        connection.send(payload)
                        response=connection.getresponse()
                        self.assertEqual(response.status,405 if method=='PUT' else 403)
                        self.assertIn('error',json.loads(response.read()))
                    finally:connection.close()
        finally:server.shutdown();server.server_close();worker.join(3)

    def test_closed_socket_during_shutdown_is_expected_but_live_socket_errors_propagate(self):
        server = object.__new__(ProductionServer)
        server.server = SimpleNamespace(run=lambda: (_ for _ in ()).throw(OSError(9, 'Synthetic closed descriptor')))
        server.stopped = True
        server.serve_forever()
        server.stopped = False
        with self.assertRaises(OSError):
            server.serve_forever()

    def test_sqlite_and_static_failures_are_safe_503_with_useful_private_logs(self):
        import sqlite3
        for factory in self.servers():
            with self.subTest(server=factory.__name__):
                server = factory(self.app, port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    for target, path, error in (
                        (patch.object(self.app, 'dispatch_get', side_effect=sqlite3.OperationalError('sk-synthetic-private-value')), '/api/health', 'OperationalError'),
                        (patch.object(Path, 'read_bytes', side_effect=OSError('sk-synthetic-private-value')), '/', 'OSError')):
                        with target, self.assertLogs('alfa_app.http_api', level='ERROR') as logs:
                            status, _, body = self.exchange(server, path=path + '?secret=sk-synthetic-private-value')
                        self.assertEqual(status, 503)
                        self.assertIn('error', json.loads(body))
                        self.assertNotIn(b'sk-synthetic-private-value', body)
                        self.assertIn(error, '\n'.join(logs.output))
                        self.assertNotIn('sk-synthetic-private-value', '\n'.join(logs.output))
                finally:
                    server.shutdown(); server.server_close(); worker.join(3)

    def test_unexpected_dispatch_error_returns_safe_json_and_server_stays_available(self):
        for factory in self.servers():
            with self.subTest(server=factory.__name__):
                server = factory(self.app, port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    for method in ('GET', 'POST'):
                        with patch.object(self.app, 'dispatch_' + method.lower(),
                                          side_effect=RuntimeError('sk-synthetic-private-value')):
                            status, headers, body = self.exchange(server, method, raw=b'{}' if method == 'POST' else None,
                                headers={'Content-Type': 'application/json', 'X-Alfa-Action': self.app.csrf})
                        self.assertEqual(status, 500)
                        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
                        self.assertIn('error', json.loads(body))
                        self.assertNotIn(b'sk-synthetic-private-value', body)
                        self.assertNotIn(b'Traceback', body)
                    self.assertEqual(self.exchange(server)[0], 200)
                finally:
                    server.shutdown(); server.server_close(); worker.join(3)

    def test_host_origin_json_and_body_bounds_match_across_transports(self):
        for factory in self.servers():
            with self.subTest(server=factory.__name__):
                server = factory(self.app, port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    valid = {'Content-Type': 'application/json', 'X-Alfa-Action': self.app.csrf}
                    cases = [
                        ('GET', '/api/health', None, {'Host': 'attacker.invalid'}, 400),
                        ('POST', '/api/insights', b'{}', dict(valid, Origin='ftp://127.0.0.1:' + str(server.server_port)), 403),
                        ('POST', '/api/insights', b'{"question":NaN}', valid, 400),
                        ('POST', '/api/insights', b'[]', valid, 400),
                        ('POST', '/api/insights', b'{"x":', valid, 400),
                        ('POST', '/api/insights', b'', valid, 413),
                        ('POST', '/api/insights', b'{}', {'Content-Type': 'application/json'}, 403),
                        ('PUT', '/api/health', b'{}', valid, 405),
                    ]
                    for method, path, body, headers, expected in cases:
                        with self.subTest(method=method, body=body, headers=headers):
                            status, _, payload = self.exchange(server, method, path, body, headers)
                            self.assertEqual(status, expected)
                            self.assertIsInstance(json.loads(payload)['error'], str)
                finally:
                    server.shutdown(); server.server_close(); worker.join(3)
