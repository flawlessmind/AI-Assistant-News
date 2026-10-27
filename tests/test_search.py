"""Real local embeddings and application contracts. No external calls or fake vectors."""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from alfa_app.config import ROOT, Settings
from alfa_app.agent import context_fingerprint
from alfa_app.semantic import SearchUnavailable, index_vectors, fingerprint_part, search_signature
from alfa_app.server import Application, AnalysisInProgress
from alfa_research import retrieve, verify_quote

DATABASE = ROOT/'data/research.sqlite'
AS_OF = date(2026, 10, 22)


class LocalSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def search(self, query, **kwargs):
        return retrieve(DATABASE, query, kwargs.pop('as_of', AS_OF), mode='hybrid', **kwargs)

    def test_paraphrase_finds_labor_page_without_any_word_match(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network in local search')):
            rows = self.search('Работодателям приходится повышать оплату персонала', limit=3)
        self.assertEqual((rows[0]['source_id'], rows[0]['page']), ('alfa_labor_7m', 2))
        self.assertEqual(rows[0]['query_terms_matched'], [])
        self.assertEqual(rows[0]['retrieval_method'], 'hybrid')
        self.assertTrue(verify_quote(DATABASE, rows[0]['source_id'], rows[0]['page'],
                                    rows[0]['text'])['quote_present_in_extracted_text'])

    def test_consumer_paraphrase_retrieves_consumption_report(self):
        rows = self.search('Люди перестали так активно тратить деньги в магазинах', limit=3)
        self.assertIn(('alfa_consumption', 1), [(r['source_id'], r['page']) for r in rows])
        for row in rows:
            self.assertEqual(len(row['text']), row['char_end']-row['char_start'])

    def test_unrelated_topics_return_no_evidence(self):
        for query in ('Как приготовить борщ из свёклы', 'квантовая запутанность фотонов в вакууме',
                      'Как заменить видеокарту в ноутбуке?'):
            with self.subTest(query=query):
                self.assertEqual(self.search(query), [])

    def test_dates_and_source_filters_apply_before_vector_ranking(self):
        query = 'Нехватка сотрудников заставляет фирмы поднимать зарплаты'
        self.assertEqual(self.search(query, as_of=date(2020, 1, 1)), [])
        self.assertEqual(self.search(query, as_of=date(2027, 10, 22), max_age_days=0), [])
        rows = self.search(query, source_id='alfa_labor_7m')
        self.assertTrue(rows)
        self.assertEqual({row['source_id'] for row in rows}, {'alfa_labor_7m'})
        historical = self.search(query, source_id='alfa_labor_7m', as_of=date(2027, 10, 22),
                                 max_age_days=0, include_historical=True)
        self.assertTrue(historical)
        self.assertTrue(all(row['historical_for_query'] for row in historical))

    def copy_index(self):
        database = self.directory/'research.sqlite'
        shutil.copy2(DATABASE, database)
        shutil.copy2(DATABASE.with_suffix('.vectors.json'), database.with_suffix('.vectors.json'))
        return database

    def test_missing_index_fails_explicitly(self):
        database = self.directory/'research.sqlite'
        shutil.copy2(DATABASE, database)
        with self.assertRaises(SearchUnavailable):
            index_vectors(database)

    def test_stale_incomplete_or_corrupt_index_cannot_report_ready(self):
        database = self.copy_index()
        path = database.with_suffix('.vectors.json')
        original = json.loads(path.read_text(encoding='utf-8'))
        variants = []
        for field, value in [('corpus_sha256', 'old'), ('revision', 'wrong'), ('dimension', 1)]:
            variants.append(dict(original, **{field: value}))
        vectors = dict(original['vectors']); vectors.pop(next(iter(vectors)))
        variants.append(dict(original, vectors=vectors))
        vectors = dict(original['vectors']); vectors[next(iter(vectors))] = [0.0]*384
        variants.append(dict(original, vectors=vectors))
        for number, payload in enumerate(variants):
            with self.subTest(number=number):
                path.write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaises(SearchUnavailable):
                    search_signature(database, 'hybrid')

    def test_search_policy_and_index_are_part_of_result_identity(self):
        database = self.copy_index()
        first = fingerprint_part(database, 'hybrid')
        self.assertNotEqual(first, fingerprint_part(database, 'lexical'))
        path = database.with_suffix('.vectors.json')
        with path.open('a', encoding='utf-8') as stream:
            stream.write('\n')
        self.assertNotEqual(first, fingerprint_part(database, 'hybrid'))

    def application(self):
        return Application(Settings(runtime_dir=self.directory/'runtime', api_key='', allow_live=False,
                                    search_mode='hybrid'))

    def test_semantic_news_search_preserves_personalization_and_date(self):
        app = self.application()
        query = {'company_id':['U_1595'], 'role':['cfo'], 'as_of':[str(AS_OF)]}
        feed = app.dispatch_get('/api/news', query)['items']
        result = app.dispatch_get('/api/search', dict(query,
                   q=['В магазинах изменились ценники на повседневные покупки']))
        self.assertTrue(result['items'])
        hit = result['items'][0]
        self.assertEqual(hit['event']['event_id'], 'news_9f10b60aed544eaa32d4')
        self.assertFalse(hit['search_match']['exact_match'])
        original = next(row for row in feed if row['event']['event_id'] == hit['event']['event_id'])
        self.assertEqual(hit['reasons'], original['reasons'])
        self.assertEqual(hit['context_fingerprint'], original['context_fingerprint'])
        before = app.dispatch_get('/api/search', dict(query, as_of=['2020-01-01'], q=['Инфляция']))
        self.assertEqual(before['items'], [])
        with app.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)

    def test_search_parameters_are_validated(self):
        app = self.application()
        query = {'company_id':['U_1595'], 'role':['cfo']}
        for change in ({'q':['']}, {'q':['x'*501]}, {'q':['test'], 'role':['invalid']}):
            with self.assertRaises(ValueError):
                app.dispatch_get('/api/search', dict(query, **change))

    def test_identical_concurrent_analysis_is_blocked_and_guard_released_after_failure(self):
        # The core lock still rejects duplicate execution; HTTP callers now
        # join the durable job instead (covered by test_source_integrity).
        app = self.application()
        body = {'company_id':'U_1595', 'event_id':'news_9f10b60aed544eaa32d4', 'role':'cfo',
                'as_of':str(AS_OF), 'mode':'live'}
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            raise RuntimeError('Synthetic failure before transport')
        def first():
            try:
                app.analyse(body)
            except RuntimeError as error:
                errors.append(str(error))
        with patch.object(app.agent, 'run', side_effect=blocked) as run:
            worker = threading.Thread(target=first)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(AnalysisInProgress):
                    app.analyse(body)
                self.assertEqual(run.call_count, 1)
            finally:
                release.set(); worker.join(5)
        self.assertEqual(len(errors), 1)
        with patch.object(app.agent, 'run', return_value={'status':'generation_unavailable'}) as run:
            self.assertEqual(app.analyse(body)['status'], 'generation_unavailable')
            self.assertEqual(run.call_count, 1)

    def test_completed_result_wins_a_race_between_cache_check_and_analysis_lock(self):
        app = self.application()
        body = {'company_id':'U_1595', 'event_id':'news_9f10b60aed544eaa32d4', 'role':'cfo',
                'as_of':str(AS_OF), 'mode':'live'}
        saved = {'status':'complete', 'trace':[], 'run_id':'synthetic-finished-while-waiting'}
        with patch.object(app.store, 'current_result', side_effect=[None, saved]), \
             patch.object(app.agent, 'run', side_effect=AssertionError('Duplicate generation')):
            result = app.analyse(body)
        self.assertEqual(result['run_id'], saved['run_id'])
        self.assertTrue(result['restored_from_cache'])
