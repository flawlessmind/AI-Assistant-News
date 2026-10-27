"""Hybrid lexical recall and metadata revisions; real local data, no paid gateway."""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from alfa_app.agent import context_fingerprint
from alfa_app.config import ROOT, Settings
from alfa_app.semantic import rank_events,lexical_signal
from alfa_app.server import Application
from alfa_app.store import Store
from alfa_research import retrieve, verify_quote


class HybridRecallTests(unittest.TestCase):
    def test_exact_bank_term_below_dense_cutoff_is_retained(self):
        rows = retrieve(ROOT/'data/research.sqlite', 'флоутеры', date(2026,10,24), mode='hybrid', limit=100)
        expected = {3, 6, 21}
        self.assertTrue(expected.issubset({row['page'] for row in rows if row['source_id']=='alfa_fi_strategy'}))
        for row in rows:
            self.assertTrue(verify_quote(ROOT/'data/research.sqlite',row['source_id'],row['page'],row['text'])['quote_present_in_extracted_text'])

    def test_exact_news_term_below_dense_cutoff_is_retained(self):
        events = [json.loads(line) for line in (ROOT/'data/news_current.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        self.assertIn('news_a334f1beced5360f4ecf', {row['event_id'] for row in rank_events('исключением',events)})

    def test_whole_article_lexical_match_survives_embedding_truncation(self):
        event = {'event_id':'synthetic-tail','title':'Искусственная новость для регрессии',
                 'text':('Повторяющееся вступление о банковских услугах. '*250)+' Пилотный реестр агропромышленных кластеров одобрен.'}
        self.assertEqual([row['event_id'] for row in rank_events('агропромышленных кластеров',[event])],['synthetic-tail'])

    def test_partial_keyword_does_not_admit_an_unrelated_query(self):
        query = 'флоутеры квантовых фотонов'
        # The encoder can itself overrate mixed topics. A partial OR hit must
        # never add candidates beyond that existing semantic channel.
        search=lambda mode:retrieve(ROOT/'data/research.sqlite',query,date(2026,10,24),mode=mode,limit=100)
        self.assertEqual({r['evidence_id'] for r in search('hybrid')},{r['evidence_id'] for r in search('semantic')})
        events=[{'event_id':'synthetic','title':'Флоутеры','text':'Банковские облигации с плавающим купоном.'}]
        self.assertEqual({r['event_id'] for r in rank_events(query,events,'hybrid')},
                         {r['event_id'] for r in rank_events(query,events,'semantic')})

    def test_semantic_mode_and_date_filters_remain_strict(self):
        db=ROOT/'data/research.sqlite'
        self.assertEqual(retrieve(db,'флоутеры',date(2020,1,1),mode='hybrid'),[])
        self.assertEqual(retrieve(db,'флоутеры',date(2026,10,24),source_id='nonexistent',mode='hybrid'),[])
        dense=retrieve(db,'флоутеры',date(2026,10,24),mode='semantic',limit=100)
        self.assertTrue(all(row['semantic_score']>=.82 for row in dense))
        for query in ('Как приготовить борщ из свёклы','квантовая запутанность фотонов в вакууме'):
            self.assertEqual(retrieve(db,query,date(2026,10,24),mode='hybrid'),[])

    def test_lexical_union_requires_meaningful_whole_terms(self):
        self.assertFalse(lexical_signal('газ','Магазин одежды')[0])
        self.assertFalse(lexical_signal('и в','Оценки и в целом прогноз')[0])
        self.assertTrue(lexical_signal('КЛЮЧЕВАЯ\u00a0СТАВКА','Ключевая ставка сохранена')[0])
        with self.assertRaises(ValueError):rank_events('газ',[{'event_id':'unused','title':'Газ','text':'Природный газ'}],'invalid')


class MetadataUpdateTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        self.app=Application(Settings(runtime_dir=Path(self.temp.name)))
        self.event=copy.deepcopy(self.app.store.event('news_9f10b60aed544eaa32d4'))
    def tearDown(self):
        self.app.jobs.close();self.temp.cleanup()

    def test_same_text_corrects_title_and_publication_date_without_losing_history(self):
        self.app.store.save_run({'run_id':'a'*32,'company_id':'U_1595','status':'historical-test'})
        self.app.store.cache_set('synthetic-cache',{'preserved':True})
        changed=dict(self.event,title=self.event['title']+' — уточнение',published_at='2026-10-17T13:00:00+03:00')
        identity,status=self.app.store.put_event(changed)
        self.assertEqual((identity,status),(self.event['event_id'],'updated'))
        current=self.app.store.event(identity)
        self.assertEqual(current['title'],changed['title']);self.assertEqual(current['published_at'],changed['published_at'])
        self.assertEqual(current['text'],self.event['text']);self.assertEqual(current['content_sha256'],self.event['content_sha256'])
        self.assertEqual(self.app.store.run('a'*32)['status'],'historical-test')
        self.assertEqual(self.app.store.cache_get('synthetic-cache'),{'preserved':True})

    def test_technical_metadata_is_saved_without_invalidating_cached_analysis(self):
        profile=self.app.profile('U_1595');as_of=date(2026,10,24)
        before=context_fingerprint(self.app.settings,profile,self.event,as_of,'cfo')[0]
        run=self.app.agent.run(profile,self.event,as_of,live=False)
        run.update(status='complete',insight={'synthetic':True})
        self.app.store.save_run(run)
        changed=dict(self.event,retrieved_at='2026-10-24T01:00:00+03:00',raw_sha256='synthetic-new-html-snapshot')
        self.app.store.put_event(changed)
        current=self.app.store.event(self.event['event_id'])
        self.assertEqual(current['raw_sha256'],changed['raw_sha256'])
        self.assertEqual(current['retrieved_at'],changed['retrieved_at'])
        self.assertEqual(before,context_fingerprint(self.app.settings,profile,current,as_of,'cfo')[0])
        with patch.object(self.app.agent,'run',side_effect=AssertionError('Do not regenerate unchanged content')):
            restored=self.app.analyse(dict(company_id='U_1595',event_id=current['event_id'],mode='live',as_of=str(as_of)))
        self.assertEqual(restored['run_id'],run['run_id'])

    def test_seed_does_not_overwrite_runtime_metadata_and_reimport_is_a_duplicate(self):
        changed=dict(self.event,title='Уточнённый заголовок')
        self.app.store.put_event(changed)
        self.assertEqual(self.app.store.put_event(dict(self.event),update_existing=False)[1],'duplicate')
        self.assertEqual(self.app.store.event(self.event['event_id'])['title'],changed['title'])
        self.assertEqual(self.app.store.put_event(dict(changed))[1],'duplicate')

    def test_title_correction_with_same_text_withdraws_old_approval_without_generation(self):
        run=self.app.agent.run(self.app.profile('U_1595'),self.event,date(2026,10,24),live=False)
        run.update(status='complete',insight={'synthetic':True});self.app.store.save_run(run)
        self.app.store.put_event(dict(self.event,title=self.event['title']+' — исправленная редакция'))
        with patch.object(self.app.agent,'run',side_effect=AssertionError('GET must not generate')):
            reopened=self.app.dispatch_get('/api/runs/'+run['run_id'],{})
        self.assertEqual(reopened['status'],'stale_result');self.assertIsNone(reopened['insight'])
        self.assertEqual(self.app.store.run(run['run_id'])['status'],'complete')

    def test_rss_reimport_updates_metadata_and_keeps_both_original_snapshots(self):
        from alfa_app.news import refresh
        from test_production import TEXT
        url='https://www.cbr.ru/press/event/?id=synthetic-metadata-only'
        def rss(title,day):
            return ('<rss><channel><item><title>'+title+'</title><link>'+url+'</link>'
                    '<pubDate>'+day+'</pubDate><description><![CDATA[<p>'+TEXT+'</p>]]></description>'
                    '</item></channel></rss>').encode()
        folder=Path(self.temp.name)/'raw'
        before=rss('Первый заголовок','Thu, 22 Oct 2026 10:00:00 +0300')
        after=rss('Исправленный заголовок','Fri, 23 Oct 2026 10:00:00 +0300')
        for raw in (before,after):
            report=refresh(self.app.store,folder,feeds={'test':'https://www.cbr.ru/rss/test'},fetcher=lambda _: (raw,'utf-8'))
            self.assertEqual(report['errors'],[])
        self.assertEqual(report['updated'],1)
        current=self.app.store.event_for_url(url)
        self.assertEqual(current['title'],'Исправленный заголовок')
        self.assertTrue(current['published_at'].startswith('2026-10-23'))
        self.assertEqual(len(list(folder.glob('*.xml'))),2)

    def test_concurrent_import_of_the_same_url_is_atomic(self):
        event=dict(self.event,event_id='synthetic-concurrent',source_url='https://www.cbr.ru/press/concurrent-only',content_sha256='synthetic-concurrent-hash')
        barrier=threading.Barrier(12)
        def insert(_):
            barrier.wait();return self.app.store.put_event(dict(event))
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(insert,range(12)))
        self.assertEqual(sum(status=='inserted' for _,status in results),1)
        self.assertEqual(sum(status=='duplicate' for _,status in results),11)
