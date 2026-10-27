"""Source identity, parameter bounds and bounded refresh; no external requests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from alfa_app.config import ROOT, Settings
from alfa_app.jobs import JobsUnavailable
from alfa_app.news import refresh
from alfa_app.production import make_production_server
from alfa_app.server import Application, make_server
from alfa_app.store import JobQueueFull, Store


def publication(name, text='Synthetic source text for the isolated regression.'):
    return dict(event_id='test-' + name, source_url='https://www.cbr.ru/press/' + name,
                title='Synthetic ' + name, text=text, content_sha256=sha256(text.encode()).hexdigest(),
                published_at='2026-10-20T12:00:00+03:00', retrieved_at='2026-10-20T13:00:00+03:00')


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache/tests').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.cache/tests')
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()


class AliasIntegrityTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.path / 'service.sqlite')
        self.a, self.b = publication('a'), publication('b')
        self.store.put_event(self.a)
        self.store.put_event(self.b)

    def assert_integrity(self):
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM aliases a LEFT JOIN news n ON n.id=a.event_id WHERE n.id IS NULL').fetchone()[0], 0)
            for identity, url, content_hash, raw in db.execute('SELECT id,url,content_hash,payload FROM news'):
                event = json.loads(raw)
                self.assertEqual((identity, url, content_hash),
                                 (event['event_id'], event['source_url'], event['content_sha256']))

    def test_changed_alias_does_not_overwrite_original_or_history(self):
        self.store.save_run(dict(run_id='history', company_id='synthetic', event=deepcopy(self.a)))
        self.store.cache_set('old-answer', {'event': self.a})
        revised = dict(self.b, text='Different B text.', content_sha256=sha256(b'Different B text.').hexdigest())
        identity, status = self.store.put_event(revised)
        self.assertNotEqual(identity, self.a['event_id'])
        self.assertEqual(status, 'inserted')
        self.assertEqual(self.store.event(self.a['event_id']), self.a)
        self.assertEqual(self.store.event_for_url(self.a['source_url']), self.a)
        self.assertEqual(self.store.event_for_url(self.b['source_url'])['text'], revised['text'])
        self.assertEqual(self.store.run('history')['event'], self.a)
        self.assertEqual(self.store.cache_get('old-answer')['event'], self.a)
        self.assertEqual(self.store.put_event(deepcopy(revised)), (identity, 'duplicate'))
        self.assertEqual(len(self.store.events()), 2)
        self.assert_integrity()

    def test_unchanged_alias_metadata_does_not_rewrite_canonical_source(self):
        mirror = dict(self.b, title='Different mirror title', retrieved_at='2026-10-21T13:00:00+03:00')
        before = deepcopy(mirror)
        self.assertEqual(self.store.put_event(mirror), (self.a['event_id'], 'duplicate'))
        self.assertEqual(self.store.event(self.a['event_id']), self.a)
        self.assertEqual(mirror, before)
        self.assert_integrity()

    def test_alias_lookup_resolves_to_an_existing_deduplicated_identity(self):
        resolved = self.store.event_for_url(self.b['source_url'])
        self.assertEqual(resolved, self.a)
        self.assertEqual(self.store.event(resolved['event_id']), resolved)

    def test_changed_canonical_source_keeps_the_fetched_alias_snapshot(self):
        revised = publication('a', 'New A text, while B has not changed.')
        self.store.put_event(revised)
        self.assertEqual(self.store.event_for_url(self.a['source_url'])['text'], revised['text'])
        saved_b = self.store.event_for_url(self.b['source_url'])
        self.assertEqual(saved_b['text'], self.b['text'])
        self.assertEqual(saved_b['source_url'], self.b['source_url'])
        self.assertEqual(saved_b['title'], self.b['title'])
        self.assertEqual(len(self.store.events()), 2)
        self.assert_integrity()

    def test_concurrent_source_updates_keep_distinct_stable_identities(self):
        revised_a = publication('a', 'New source A text.')
        revised_b = publication('b', 'New source B text.')
        with ThreadPoolExecutor(8) as executor:
            results = list(executor.map(self.store.put_event, [deepcopy(e) for e in (revised_a, revised_b) * 8]))
        self.assertEqual(self.store.event_for_url(self.a['source_url'])['text'], revised_a['text'])
        self.assertEqual(self.store.event_for_url(self.b['source_url'])['text'], revised_b['text'])
        self.assertEqual({r[0] for r in results}, {self.a['event_id'], self.b['event_id']})
        self.assertEqual(len(self.store.events()), 2)
        self.assert_integrity()

    def test_changed_alias_can_join_another_matching_source(self):
        c = publication('c', 'Another independently fetched text.')
        self.store.put_event(c)
        revised_b = dict(self.b, text=c['text'], content_sha256=c['content_sha256'])
        self.assertEqual(self.store.put_event(revised_b), (c['event_id'], 'duplicate'))
        self.assertEqual(self.store.event(self.a['event_id']), self.a)
        self.assertEqual(self.store.event(c['event_id']), c)
        self.assertEqual(self.store.event_for_url(self.b['source_url'])['text'], c['text'])
        self.assert_integrity()

    def test_alias_detachment_rolls_back_if_insert_fails(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON news BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.put_event(publication('b', 'Changed B text.'))
        self.assertEqual(self.store.event(self.a['event_id']), self.a)
        self.assertEqual(self.store.event_for_url(self.b['source_url'])['text'], self.b['text'])
        self.assertEqual(len(self.store.events()), 1)
        self.assert_integrity()

    def test_compare_and_swap_revision_preserves_alias_snapshot(self):
        revised_a = publication('a', 'A corrected after verified extraction.')
        self.assertEqual(self.store.apply_event_revision(revised_a, self.a['content_sha256']), 1)
        self.assertEqual(self.store.apply_event_revision(self.a, self.a['content_sha256']), 0)
        self.assertEqual(self.store.event_for_url(self.a['source_url'])['text'], revised_a['text'])
        self.assertEqual(self.store.event_for_url(self.b['source_url'])['text'], self.b['text'])
        self.assert_integrity()

    def test_legacy_alias_migration_retains_rows_and_accepts_fresh_source(self):
        path = self.path / 'legacy.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE news(id TEXT PRIMARY KEY,url TEXT UNIQUE NOT NULL,content_hash TEXT NOT NULL,payload TEXT NOT NULL);'
                             'CREATE TABLE aliases(url TEXT PRIMARY KEY,event_id TEXT NOT NULL);')
            before = (self.a['event_id'], self.a['source_url'], self.a['content_sha256'], json.dumps(self.a))
            db.execute('INSERT INTO news VALUES(?,?,?,?)', before)
            db.execute('INSERT INTO aliases VALUES(?,?)', (self.b['source_url'], self.a['event_id']))
        db.close()
        legacy = Store(path)
        with legacy.connect() as db:
            self.assertEqual(db.execute('SELECT * FROM news').fetchone(), before)
            self.assertEqual(db.execute('SELECT url,event_id,payload FROM aliases').fetchone(),
                             (self.b['source_url'], self.a['event_id'], None))
        revised_b = publication('b', 'Freshly fetched B text.')
        self.assertEqual(legacy.put_event(revised_b), (self.b['event_id'], 'inserted'))
        self.assertEqual(legacy.event(self.a['event_id']), self.a)
        self.assertEqual(legacy.event_for_url(self.b['source_url']), revised_b)

    def test_concurrent_legacy_schema_initialization_is_atomic(self):
        path = self.path / 'concurrent-legacy.sqlite'
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE aliases(url TEXT PRIMARY KEY,event_id TEXT NOT NULL)')
            db.commit()
        finally:
            db.close()
        rendezvous = threading.Barrier(2)
        connect = sqlite3.connect
        class InterleavedConnection(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if sql == 'PRAGMA table_info(aliases)':
                    rows = list(cursor)
                    if 'payload' not in {row[1] for row in rows}:
                        # Force the real check/ALTER interleaving. With a write
                        # transaction the second initializer cannot enter here.
                        try:
                            rendezvous.wait(.35)
                        except threading.BrokenBarrierError:
                            pass
                    return rows
                return cursor
        def coordinated(*args, **kwargs):
            return connect(*args, **dict(kwargs, factory=InterleavedConnection))
        with patch('alfa_app.store.sqlite3.connect', coordinated), ThreadPoolExecutor(2) as executor:
            stores = list(executor.map(lambda _: Store(path), range(2)))
        self.assertEqual(len(stores), 2)
        with stores[0].connect() as db:
            self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(aliases)')].count('payload'), 1)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_legacy_overwritten_payload_is_preserved_when_original_is_refetched(self):
        # Simulate the old defect: SQL URL A, payload B with canonical ID A.
        old_b = dict(publication('b', 'Previously fetched B revision.'), event_id=self.a['event_id'])
        with self.store.connect() as db:
            db.execute('UPDATE news SET content_hash=?,payload=? WHERE id=?',
                       (old_b['content_sha256'], json.dumps(old_b), self.a['event_id']))
            db.execute('UPDATE aliases SET payload=NULL')
        self.store.put_event(self.a)
        self.assertEqual(self.store.event(self.a['event_id']), self.a)
        saved = self.store.event_for_url(self.b['source_url'])
        self.assertEqual(saved['text'], old_b['text'])
        self.assertEqual(saved['source_url'], self.b['source_url'])
        self.assertNotEqual(saved['event_id'], self.a['event_id'])
        self.assertEqual(self.store.put_event(old_b), (saved['event_id'], 'duplicate'))
        self.assert_integrity()

    def test_unknown_legacy_alias_metadata_is_preserved_without_fabricating_a_source(self):
        with self.store.connect() as db:
            db.execute('UPDATE aliases SET payload=NULL')
        revised_a = publication('a', 'Fresh canonical A text.')
        self.store.put_event(revised_a)
        with self.store.connect() as db:
            snapshot = json.loads(db.execute('SELECT payload FROM aliases WHERE url=?', (self.b['source_url'],)).fetchone()[0])
        self.assertEqual(snapshot, self.a)  # Real old canonical, no invented B metadata.
        self.assertIsNone(self.store.event_for_url(self.b['source_url']))
        self.store.put_event(self.b)  # Verified B fetch restores a current independent identity.
        self.assertEqual(self.store.event_for_url(self.b['source_url']), self.b)
        self.assertEqual(self.store.event(self.a['event_id']), revised_a)
        self.assert_integrity()

    def test_alias_revision_preserves_original_approval_and_never_reuses_it_for_the_mirror(self):
        app = Application(Settings(runtime_dir=self.path / 'fingerprints'), seed=False)
        try:
            event = json.loads((ROOT / 'data/news_snapshot.jsonl').read_text(encoding='utf-8').splitlines()[0])
            separator = '&' if '?' in event['source_url'] else '?'
            mirror = dict(event, event_id='synthetic-mirror', source_url=event['source_url'] + separator + 'mirror=synthetic')
            app.store.put_event(event)
            app.store.put_event(mirror)
            body = dict(company_id='U_1595', event_id=event['event_id'], as_of='2026-10-23', mode='sources')
            run = app.analyse(body)
            # Synthetic approval fixture, never presented in the real runtime.
            run.update(status='complete', insight={'synthetic': True})
            app.store.save_run(run)
            revised = dict(mirror, text=mirror['text'] + ' Synthetic mirror revision.')
            revised['content_sha256'] = sha256(revised['text'].encode()).hexdigest()
            mirror_id, _ = app.store.put_event(revised)
            current_a = app.analysis_context(body)
            current_b = app.analysis_context(dict(body, event_id=mirror_id))
            self.assertEqual(current_a['fingerprint'], run['context_fingerprint'])
            self.assertNotEqual(current_b['fingerprint'], run['context_fingerprint'])
            self.assertEqual(app.dispatch_get('/api/runs/' + run['run_id'], {})['status'], 'complete')
            self.assertIsNone(app.store.current_result('U_1595', current_b['fingerprint']))
            changed_a = dict(event, text=event['text'] + ' Synthetic original revision.')
            changed_a['content_sha256'] = sha256(changed_a['text'].encode()).hexdigest()
            app.store.put_event(changed_a)
            historical = app.dispatch_get('/api/runs/' + run['run_id'], {})
            self.assertEqual(historical['status'], 'stale_result')
            self.assertIsNone(historical['insight'])
            self.assertEqual(app.store.run(run['run_id'])['event'], event)
            self.assertEqual(app.store.run(run['run_id'])['status'], 'complete')
            self.assertEqual(app.store.event(mirror_id)['text'], revised['text'])
        finally:
            app.jobs.close()


class SourceHTTPTests(WorkspaceCase):
    @contextmanager
    def serving(self, factory):
        app = Application(Settings(runtime_dir=self.path / factory.__name__, max_active_jobs=1), seed=False)
        server = factory(app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield app, server.server_port
        finally:
            server.shutdown()
            server.server_close()
            app.jobs.close()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def request(self, port, path, body=None, token=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
        try:
            headers = {'Content-Type': 'application/json', 'X-Alfa-Action': token} if body is not None else {}
            connection.request('POST' if body is not None else 'GET', path,
                               json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_page_bounds_have_client_errors_in_both_transports(self):
        invalid = ('9223372036854775808', '9' * 200, '-1', '0', 'abc', '', '1.0', '1e2', '-9223372036854775809')
        for factory in (make_server, make_production_server):
            with self.serving(factory) as (_, port):
                for value in invalid:
                    with self.subTest(server=factory.__name__, page=value):
                        status, payload = self.request(port, '/api/source?source_id=alfa_inflation&page=' + value)
                        self.assertEqual(status, 400)
                        self.assertIn('error', payload)
                for value in ('9223372036854775807', '2'):
                    status, _ = self.request(port, '/api/source?source_id=unknown-source&page=' + value)
                    self.assertEqual(status, 400)  # Valid integer, missing physical page.
                for suffix in ('', '&page=1', '&page=01'):
                    status, payload = self.request(port, '/api/source?source_id=alfa_inflation' + suffix)
                    self.assertEqual(status, 200)
                    self.assertEqual(payload['page'], 1)

    def test_empty_optional_parameters_keep_the_legacy_defaults_outside_source(self):
        for factory in (make_server, make_production_server):
            with self.serving(factory) as (_, port):
                for route in ('/api/news?company_id=U_1595', '/api/search?company_id=U_1595&q=test'):
                    status, payload = self.request(port, route + '&role=&as_of=')
                    self.assertEqual(status, 200)
                    self.assertEqual(payload['items'], [])

    def test_synchronous_refresh_respects_capacity_in_both_transports(self):
        for factory in (make_server, make_production_server):
            with self.serving(factory) as (app, port):
                app.store.create_job('occupied', {'operation': 'refresh', 'limit': 15}, capacity=1)
                with patch('alfa_app.server.refresh', return_value={'inserted': 0}, create=True) as direct, \
                     patch('alfa_app.jobs.refresh', return_value={'inserted': 0}) as background:
                    status, payload = self.request(port, '/api/refresh', {'limit': 5}, app.csrf)
                self.assertEqual(status, 503)
                self.assertEqual(payload['code'], 'queue_full')
                direct.assert_not_called()
                background.assert_not_called()

    def test_sync_response_shape_and_refresh_failures_in_both_transports(self):
        for factory in (make_server, make_production_server):
            with self.serving(factory) as (app, port):
                report = {'inserted': 0, 'updated': 0, 'errors': [{'source': 'test', 'error': 'TimeoutError'}]}
                with patch('alfa_app.jobs.refresh', return_value=report) as fetch:
                    status, payload = self.request(port, '/api/refresh', {'limit': 5}, app.csrf)
                self.assertEqual((status, payload), (200, report))
                self.assertEqual(fetch.call_count, 1)
                with patch('alfa_app.jobs.refresh', side_effect=OSError('sk-synthetic-private-value')) as fetch:
                    status, payload = self.request(port, '/api/refresh', {'limit': 5}, app.csrf)
                self.assertEqual(status, 503)
                self.assertNotIn('sk-synthetic', json.dumps(payload))
                self.assertEqual(fetch.call_count, 1)
                with app.store.connect() as db:
                    raw = db.execute("SELECT error FROM jobs WHERE state='failed'").fetchone()[0]
                    self.assertNotIn('модель', raw)
                    self.assertNotIn('sk-synthetic', raw)

    def test_legacy_live_analysis_without_request_id_cannot_bypass_capacity(self):
        event = json.loads((ROOT / 'data/news_snapshot.jsonl').read_text(encoding='utf-8').splitlines()[0])
        for factory in (make_server, make_production_server):
            with self.serving(factory) as (app, port):
                app.store.put_event(event)
                app.store.create_job('occupied', {'operation': 'refresh', 'limit': 15}, capacity=1)
                body = dict(company_id='U_1595', event_id=event['event_id'], mode='live', as_of='2026-10-23')
                with patch.object(app, 'analyse', return_value={'run_id': 'synthetic'}) as analyse:
                    status, payload = self.request(port, '/api/insights', body, app.csrf)
                self.assertEqual(status, 503)
                self.assertEqual(payload['code'], 'queue_full')
                analyse.assert_not_called()

    def test_synchronous_refresh_cannot_starve_health_in_production(self):
        entered, release = threading.Event(), threading.Event()
        arrived = threading.Condition()
        count = 0
        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(6))
            return {'inserted': 0}
        with self.serving(make_production_server) as (app, port):
            dispatch = app.dispatch_post
            def observed(path, body):
                nonlocal count
                with arrived:
                    count += 1
                    arrived.notify_all()
                return dispatch(path, body)
            with patch.object(app, 'dispatch_post', observed), \
                 patch('alfa_app.server.refresh', blocked, create=True), \
                 patch('alfa_app.jobs.refresh', blocked), ThreadPoolExecutor(8) as executor:
                futures = [executor.submit(self.request, port, '/api/refresh', {'limit': 5}, app.csrf) for _ in range(8)]
                try:
                    self.assertTrue(entered.wait(2))
                    with arrived:
                        self.assertTrue(arrived.wait_for(lambda: count == 8, timeout=2))
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
                    try:
                        connection.request('GET', '/api/health')
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        response.read()
                    finally:
                        connection.close()
                finally:
                    release.set()
                responses = [future.result(3)[0] for future in futures]
                self.assertEqual(responses.count(200), 2)
                self.assertEqual(responses.count(503), 6)


class RefreshQueueTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.app = Application(Settings(runtime_dir=self.path), seed=False)

    def tearDown(self):
        self.app.jobs.close()
        super().tearDown()

    @contextmanager
    def refresher(self, function):
        # Exercise both the old direct branch and the bounded worker branch.
        with patch('alfa_app.server.refresh', function, create=True), patch('alfa_app.jobs.refresh', function):
            yield

    def test_two_simultaneous_synchronous_refreshes_join_one_job(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        report = {'inserted': 1, 'updated': 0, 'errors': []}
        def blocked(*args, **kwargs):
            calls.append(kwargs['limit'])
            entered.set()
            self.assertTrue(release.wait(3))
            return report
        with self.refresher(blocked), ThreadPoolExecutor(2) as executor:
            try:
                first = executor.submit(self.app.dispatch_post, '/api/refresh', {'limit': 5})
                self.assertTrue(entered.wait(2))
                second = executor.submit(self.app.dispatch_post, '/api/refresh', {'limit': 5})
                time.sleep(.15)
                self.assertEqual(calls, [5])
            finally:
                release.set()
            self.assertEqual(first.result(2), report)
            self.assertEqual(second.result(2), report)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE state='done'").fetchone()[0], 1)

    def test_sync_and_async_refresh_share_the_same_work(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocked(*args, **kwargs):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(3))
            return {'inserted': 0}
        with self.refresher(blocked), ThreadPoolExecutor(1) as executor:
            try:
                future = executor.submit(self.app.dispatch_post, '/api/refresh', {'limit': 5})
                self.assertTrue(entered.wait(2))
                job = self.app.dispatch_post('/api/refresh', {'limit': 5, 'async': True})
                time.sleep(.1)
                self.assertEqual(calls, [1])
            finally:
                release.set()
            self.assertEqual(future.result(2), {'inserted': 0})
            self.assertEqual(self.app.jobs.wait(job['job_id']), {'inserted': 0})

    def test_refresh_stops_before_next_fetch_during_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        raw = b'<rss><channel><item><title>test</title><link>https://www.cbr.ru/press/a</link><pubDate>Tue, 20 Oct 2026 10:00:00 +0300</pubDate><description>short</description></item></channel></rss>'
        def fetcher(url):
            calls.append(url)
            entered.set()
            self.assertTrue(release.wait(3))
            return raw, 'utf-8'
        def synthetic(store, directory, **kwargs):
            return refresh(store, directory, feeds={'a': 'https://www.cbr.ru/rss/a',
                           'b': 'https://www.cbr.ru/rss/b'}, fetcher=fetcher, **kwargs)
        with self.refresher(synthetic), ThreadPoolExecutor(1) as executor:
            try:
                future = executor.submit(self.app.dispatch_post, '/api/refresh', {'limit': 5})
                self.assertTrue(entered.wait(2))
                self.app.jobs.request_stop()
            finally:
                release.set()
            with self.assertRaises(JobsUnavailable):
                future.result(2)
        self.app.jobs.close()
        self.assertEqual(len(calls), 1)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE state='failed'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)

    def test_explicit_repeated_refreshes_create_new_jobs_without_reusing_old_reports(self):
        with self.refresher(lambda *args, **kwargs: {'inserted': 1}):
            self.assertEqual(self.app.dispatch_post('/api/refresh', {'limit': 5}), {'inserted': 1})
        with self.refresher(lambda *args, **kwargs: {'inserted': 0, 'updated': 1}):
            self.assertEqual(self.app.dispatch_post('/api/refresh', {'limit': 5}), {'inserted': 0, 'updated': 1})
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE state='done'").fetchone()[0], 2)

    def test_restart_never_replays_pending_refresh_and_retains_finished_reports(self):
        store = self.app.store
        pending, _ = store.create_job('pending-refresh', {'operation': 'refresh', 'limit': 5})
        finished, _ = store.create_job('finished-refresh', {'operation': 'refresh', 'limit': 1})
        store.update_job(finished, state='done', result={'report': {'inserted': 2}})
        store.cache_set('retained', {'synthetic': True})
        self.app.jobs.close()
        with patch('alfa_app.jobs.refresh', side_effect=AssertionError('Unexpected automatic replay')):
            self.assertEqual(store.recover_jobs(), 1)
            self.assertEqual(store.recover_jobs(), 0)
            reopened = Application(Settings(runtime_dir=self.path), seed=False)
            try:
                self.assertEqual(reopened.jobs.public(pending)['state'], 'failed')
                self.assertEqual(reopened.jobs.public(finished)['report'], {'inserted': 2})
                self.assertEqual(reopened.store.cache_get('retained'), {'synthetic': True})
                with reopened.store.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)
            finally:
                reopened.jobs.close()

    def test_legacy_live_analysis_joins_active_context_without_a_request_id(self):
        event = json.loads((ROOT / 'data/news_snapshot.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.app.store.put_event(event)
        body = dict(company_id='U_1595', event_id=event['event_id'], mode='live', as_of='2026-10-23')
        original = self.app.analyse
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocked(request, progress=None):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(3))
            return original(dict(request, mode='sources'), progress=progress)
        with patch.object(self.app, 'analyse', blocked), ThreadPoolExecutor(2) as executor:
            try:
                first = executor.submit(self.app.dispatch_post, '/api/insights', body)
                self.assertTrue(entered.wait(2))
                second = executor.submit(self.app.dispatch_post, '/api/insights', body)
                time.sleep(.1)
                self.assertEqual(calls, [1])
            finally:
                release.set()
            self.assertEqual(first.result(3)['run_id'], second.result(3)['run_id'])


if __name__ == '__main__':
    unittest.main()
