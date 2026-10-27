"""Queue, whole-article context, publisher cleanup and file-cache regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from dataclasses import replace
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from alfa_app.agent import context_fingerprint,check_draft
from alfa_app.config import ROOT,Settings,require_safe_bind
from alfa_app.file_identity import sha256_file,clear_hash_cache
from alfa_app.llm import LLMClient
from alfa_app.news import event_from_article,parse_article
from alfa_app.news_context import select_news_context,selected_quotes
from alfa_app.production import make_production_server
from alfa_app.server import Application,make_server
from alfa_app.store import Store,JobQueueFull


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache/tests').mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        self.path=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)


class QueueTests(WorkspaceCase):
    def test_queue_configuration_is_positive_and_defaults_to_sixteen(self):
        self.assertEqual(Settings().max_active_jobs,16)
        for value in (0,-1,False,None,'2'):
            with self.subTest(value=value),self.assertRaises(ValueError):Settings(max_active_jobs=value)
        with patch('alfa_app.config.load_env'),patch.dict(os.environ,{'ALFA_MAX_ACTIVE_JOBS':'3'},clear=True):
            self.assertEqual(Settings.from_env().max_active_jobs,3)

    def test_concurrent_distinct_submissions_respect_capacity_and_duplicates_still_join(self):
        store=Store(self.path/'service.sqlite');barrier=threading.Barrier(12)
        def submit(index):
            barrier.wait()
            try:return store.create_job('question-'+str(index),{},capacity=3)[0]
            except JobQueueFull:return None
        with ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(submit,range(12)))
        accepted=[result for result in results if result]
        self.assertEqual(len(accepted),3)
        job=store.job(accepted[0])
        self.assertEqual(store.create_job(job['context_key'],{},capacity=3),(accepted[0],False))
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],3)

    def test_full_queue_returns_503_in_both_servers_and_accepts_duplicate_before_capacity(self):
        for factory in (make_server,make_production_server):
            with self.subTest(factory=factory.__name__):
                folder=self.path/factory.__name__
                app=Application(Settings(runtime_dir=folder,max_active_jobs=2))
                real=app.analyse;release=threading.Event()
                def blocked(body,progress=None):release.wait(4);return real(body,progress)
                server=factory(app,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                base='http://127.0.0.1:'+str(server.server_port)
                body={'company_id':'U_1595','event_id':'news_9f10b60aed544eaa32d4','role':'cfo',
                      'as_of':'2026-10-23','mode':'sources','task':'sources','async':True,'question':'Первый вопрос?'}
                def post(body):
                    request=urllib.request.Request(base+'/api/insights',json.dumps(body).encode(),
                        {'Content-Type':'application/json','X-Alfa-Action':app.csrf})
                    with opener.open(request,timeout=3) as response:return json.load(response)
                try:
                    with patch.object(app,'analyse',side_effect=blocked):
                        first=post(body);second=post(dict(body,question='Второй вопрос?'))
                        self.assertNotEqual(first['job_id'],second['job_id'])
                        self.assertEqual(post(body)['job_id'],first['job_id'])
                        with self.assertRaises(urllib.error.HTTPError) as caught:post(dict(body,question='Третий вопрос?'))
                        self.assertEqual(caught.exception.code,503)
                        self.assertEqual(json.load(caught.exception)['code'],'queue_full')
                        release.set()
                        for _ in range(100):
                            if all(app.store.job(job['job_id'])['state']=='done' for job in (first,second)):break
                            time.sleep(.02)
                        self.assertEqual(app.store.job(first['job_id'])['state'],'done')
                        self.assertEqual(app.store.job(second['job_id'])['state'],'done')
                finally:release.set();server.shutdown();server.server_close();app.jobs.close();thread.join(3)

    def test_restart_recovers_interrupted_jobs_without_resending_or_deleting_cache(self):
        store=Store(self.path/'service.sqlite')
        queued,_=store.create_job('queued',{});running,_=store.create_job('running',{})
        complete,_=store.create_job('done',{})
        store.update_job(running,state='running');store.update_job(complete,state='done',result={'report':{'ok':True}})
        store.cache_set('retained',{'result':{'ok':True}})
        self.assertEqual(store.recover_jobs(),2);self.assertEqual(store.recover_jobs(),0)
        reopened=Store(self.path/'service.sqlite')
        self.assertEqual(reopened.job(complete)['state'],'done')
        self.assertEqual(reopened.job(queued)['state'],'failed');self.assertEqual(reopened.job(running)['state'],'failed')
        self.assertEqual(reopened.cache_get('retained')['result'],{'ok':True})
        with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0],0)

    def test_external_binding_needs_explicit_opt_in_in_both_servers(self):
        app=Application(Settings(runtime_dir=self.path))
        try:
            for factory in (make_server,make_production_server):
                with self.assertRaisesRegex(ValueError,'127.0.0.1'):factory(app,host='0.0.0.0',port=0)
            require_safe_bind('127.0.0.1');require_safe_bind('localhost');require_safe_bind('::1')
            require_safe_bind('0.0.0.0',True)
        finally:app.jobs.close()


class LongNewsTests(WorkspaceCase):
    def test_key_fact_and_limitation_at_end_are_selected_with_literal_offsets(self):
        end='Оговорка: предварительные данные за III квартал 2026 года не распространяются на малые компании.'
        fact='Поставки промышленных насосов приостановлены до декабря 2026 года.'
        text=('Вводная часть экономического обзора.\n'+('Общее описание результатов наблюдения без дополнительных выводов.\n'*180)+fact+'\n'+end)
        context=select_news_context(text,'Изменение поставок промышленных насосов','Что известно о приостановке поставок насосов?')
        self.assertIn(fact,context['text']);self.assertIn(end,context['text'])
        self.assertLessEqual(len(context['text']),3500)
        self.assertTrue(context['context_incomplete']);self.assertIsNotNone(context['context_warning'])
        for span in context['fragments']:self.assertEqual(text[span['char_start']:span['char_end']],span['text'])

    def test_omitted_limitations_are_explicit_and_short_articles_are_unchanged(self):
        text='\n'.join(f'Предварительная оценка за период 2026 года, раздел {index}: только согласованный сценарий.' for index in range(150))
        context=select_news_context(text,'Оценка','Сценарий',limit=600)
        self.assertGreater(context['omitted_important_fragments'],0)
        self.assertIn('полнота существенного контекста не подтверждена',context['context_warning'])
        self.assertLessEqual(len(context['text']),600)
        short='Дата публикации 23.10.2026. Данные предварительные, исключения сохраняются.'
        self.assertEqual(select_news_context(short)['text'],short)
        self.assertFalse(select_news_context(short)['text_truncated'])

    def test_agent_sends_tail_fact_and_validates_quotes_against_original_article(self):
        app=Application(Settings(api_key='synthetic-only',allow_live=True,runtime_dir=self.path,max_rounds=1))
        original=copy.deepcopy(app.store.event('news_9f10b60aed544eaa32d4'))
        tail='Однако рост потребительских цен не означает изменения выручки конкретной компании.'
        original['text']=('Обзор экономики и потребительских цен.\n'+('Сведения об общей экономической обстановке.\n'*200)+tail)
        original['content_sha256']=hashlib.sha256(original['text'].encode()).hexdigest()
        captured=[]
        def send(method,url,headers,body,timeout):captured.append(json.loads(body));return 401,b'{}'
        app.agent.llm=LLMClient(app.settings,app.store,send)
        try:
            run=app.agent.run(app.profile('U_1595'),original,date(2026,10,23),live=True)
            self.assertEqual(len(captured),1)
            news=json.loads(captured[0]['messages'][-1]['content'])['news']
            self.assertIn(tail,news['text']);self.assertLessEqual(len(news['text']),3500)
            self.assertTrue(news['context_incomplete']);self.assertTrue(run['news_context']['context_incomplete'])
            for quote in news['quote_options']:self.assertIn(quote,original['text'])
            fabricated=original['text'][:30]+' […] '+tail
            self.assertNotIn(fabricated,original['text'])
        finally:app.jobs.close()


class PublisherCleanupTests(WorkspaceCase):
    def test_startup_applies_only_known_old_revisions_and_preserves_history(self):
        manifest=json.loads((ROOT/'data/news_cleanup_2026-10-23.json').read_text('utf-8'))
        events={event['event_id']:event for event in map(json.loads,(ROOT/'data/news_current.jsonl').read_text('utf-8').splitlines())}
        store=Store(self.path/'service.sqlite')
        for revision in manifest['revisions']:
            old=dict(events[revision['event_id']],content_sha256=revision['previous_content_sha256'],text='Synthetic old source for conditional migration')
            store.put_event(old)
        historical={'run_id':'c'*32,'company_id':'U_1595','status':'sources_ready','event':copy.deepcopy(old)}
        store.save_run(historical);store.cache_set('kept',{'old':'full response'})
        app=Application(Settings(runtime_dir=self.path))
        try:
            for revision in manifest['revisions']:
                self.assertEqual(app.store.event(revision['event_id'])['content_sha256'],revision['new_content_sha256'])
            self.assertEqual(app.store.run('c'*32)['event']['text'],historical['event']['text'])
            self.assertEqual(app.store.cache_get('kept'),{'old':'full response'})
        finally:app.jobs.close()

    def test_changed_article_retires_previous_approval_without_deleting_historical_run(self):
        app=Application(Settings(runtime_dir=self.path))
        try:
            profile=app.profile('U_1595');event=app.store.event('news_9f10b60aed544eaa32d4')
            run=app.agent.run(profile,event,date(2026,10,23),live=False)
            run.update(status='complete',insight={'synthetic':'verified fixture'});app.store.save_run(run)
            self.assertEqual(app.dispatch_get('/api/runs/'+run['run_id'],{})['status'],'complete')
            updated=dict(event,text=event['text']+'\nДополнительная оговорка.',content_sha256='synthetic-new-content-hash')
            self.assertEqual(app.store.apply_event_revision(updated,event['content_sha256']),1)
            current=app.dispatch_get('/api/runs/'+run['run_id'],{})
            self.assertEqual(current['status'],'stale_result');self.assertIsNone(current['insight'])
            self.assertEqual(app.store.run(run['run_id'])['status'],'complete')
        finally:app.jobs.close()

    def test_rosstat_structural_chrome_is_removed_without_cutting_real_paragraphs_or_tables(self):
        substantive='Подписка предприятий на обследование не заменяет отчёт. Данные за июль 2026 года предварительные.'
        raw=('''<main><h1>Наблюдение предприятий</h1><div class="content"><p>'''+substantive+'''</p>
          <table><tr><th>Период</th><th>Индекс</th></tr><tr><td>Июль 2026</td><td>101,2</td></tr></table>
          <p>Оговорка: малые предприятия не учитываются.</p><nav>Меню</nav>
          <div class="newsletter"><p>Подпишитесь на рассылку</p></div><p>Содержательный абзац после виджета.</p></div>
          <div class="big-banner"><div class="content">Сервис подписки</div></div>
          <div class="toggle-card"><div class="content">Войдите для комментария</div></div>
          <footer><div class="content">Служебные контакты</div></footer></main>''').encode()
        article=parse_article(raw,content_classes={'content'})
        for value in (substantive,'Июль 2026','101,2','малые предприятия','Содержательный абзац после виджета'):
            self.assertIn(value,article['text'])
        for value in ('Меню','Подпишитесь','Сервис подписки','Войдите','Служебные контакты'):
            self.assertNotIn(value,article['text'])

    def test_cbr_content_preserves_date_and_exceptions_and_ignores_embedded_chrome(self):
        text='Решение принято 23 октября 2026 года. Оценка относится только к крупным предприятиям; сезонность не учитывается.'
        raw=('<h1>Решение</h1><div class="news-info-line_date">23.10.2026</div><div class="landing-text"><p>'+text+
             '</p><div class="social-share">Поделиться</div><table><tr><td>Исключение</td><td>2025 год</td></tr></table></div><footer>Контакты</footer>').encode()
        event=event_from_article('https://www.cbr.ru/press/event/?id=123',raw,None)
        self.assertIn(text,event['text']);self.assertIn('Исключение',event['text']);self.assertIn('2025 год',event['text'])
        self.assertTrue(event['published_at'].startswith('2026-10-23'));self.assertNotIn('Поделиться',event['text'])
        self.assertNotIn('Контакты',event['text'])

    def test_eight_real_cleaned_articles_match_original_html_and_literal_fact_offsets(self):
        manifest=json.loads((ROOT/'data/news_cleanup_2026-10-23.json').read_text('utf-8'))
        events={event['event_id']:event for event in map(json.loads,(ROOT/'data/news_current.jsonl').read_text('utf-8').splitlines())}
        self.assertEqual(len(manifest['revisions']),8)
        for revision in manifest['revisions']:
            raw=(ROOT/revision['raw_source']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),revision['raw_sha256'])
            event=events[revision['event_id']]
            self.assertEqual(parse_article(raw,content_classes={'content'})['text'],event['text'])
            self.assertEqual(hashlib.sha256(event['text'].encode()).hexdigest(),event['content_sha256'])
            for fact in event['facts']:self.assertEqual(event['text'][fact['char_start']:fact['char_end']],fact['text'])
            self.assertNotIn('Подпишитесь на рассылку',event['text']);self.assertNotIn('Ошибка в тексте',event['text'])

    def test_conditional_revision_preserves_newer_news_history_and_cached_models(self):
        store=Store(self.path/'service.sqlite')
        event={'event_id':'test-revision','source_url':'https://www.cbr.ru/press/event/?id=test-revision',
            'content_sha256':'old','text':'Old source','published_at':'2026-10-23'}
        store.put_event(event);store.cache_set('old-draft',{'full':'retained'})
        historical={'run_id':'a'*32,'company_id':'U_1595','status':'sources_ready','event':copy.deepcopy(event)}
        store.save_run(historical)
        updated=dict(event,text='Clean source',content_sha256='clean')
        self.assertEqual(store.apply_event_revision(updated,'old'),1)
        self.assertEqual(store.apply_event_revision(updated,'old'),0)
        self.assertEqual(store.run('a'*32)['event']['text'],'Old source')
        self.assertEqual(store.cache_get('old-draft'),{'full':'retained'})
        newer=dict(event,text='Later independently fetched text',content_sha256='newer');store.put_event(newer)
        self.assertEqual(store.apply_event_revision(updated,'old'),0)
        self.assertEqual(store.event(event['event_id'])['content_sha256'],'newer')


class FileIdentityTests(WorkspaceCase):
    def test_unchanged_hash_is_read_once_and_invalidated_after_edit_or_replace(self):
        clear_hash_cache();path=self.path/'identity.bin';path.write_bytes(b'original')
        original_open=Path.open;reads=[]
        def tracked_open(target,*args,**kwargs):
            if target==path.resolve() and args and args[0]=='rb':reads.append(1)
            return original_open(target,*args,**kwargs)
        with patch.object(Path,'open',tracked_open):
            first=sha256_file(path);self.assertEqual(first,sha256_file(path));self.assertEqual(reads,[1])
            path.write_bytes(b'changed and longer')
            second=sha256_file(path);self.assertNotEqual(first,second);self.assertEqual(len(reads),2)
            stamp=path.stat();replacement=self.path/'replacement';replacement.write_bytes(b'changed and newer!')
            os.utime(replacement,ns=(stamp.st_atime_ns,stamp.st_mtime_ns));replacement.replace(path)
            self.assertNotEqual(second,sha256_file(path));self.assertEqual(len(reads),3)

    def test_repeated_feed_hashes_each_unchanged_file_once_and_uses_one_state_query(self):
        app=Application(Settings(runtime_dir=self.path,search_mode='hybrid'))
        clear_hash_cache();original_open=Path.open;reads=[]
        targets={app.settings.research_database.resolve(),app.settings.research_database.with_suffix('.vectors.json').resolve()}
        def tracked_open(target,*args,**kwargs):
            if target.resolve() in targets and args and args[0]=='rb':reads.append(target.resolve())
            return original_open(target,*args,**kwargs)
        try:
            with (patch.object(Path,'open',tracked_open),patch.object(app.store,'main_analysis_states',wraps=app.store.main_analysis_states) as states,
                 patch.object(app.store,'current_main_run_id',side_effect=AssertionError('No per-row result lookup')),
                 patch.object(app.store,'latest_analysis_result',side_effect=AssertionError('No per-row status lookup'))):
                query={'company_id':['U_1595'],'role':['cfo'],'as_of':['2026-10-23']}
                first=app.dispatch_get('/api/news',query);second=app.dispatch_get('/api/news',query)
                self.assertEqual(first,second);self.assertEqual(states.call_count,2)
                self.assertEqual(len(reads),2);self.assertEqual(set(reads),targets)
        finally:app.jobs.close()

    def test_source_edit_invalidates_context_and_index_instead_of_reusing_old_result(self):
        database=self.path/'research.sqlite';shutil.copyfile(ROOT/'data/research.sqlite',database)
        index=database.with_suffix('.vectors.json');shutil.copyfile(ROOT/'data/research.vectors.json',index)
        app=Application(Settings(runtime_dir=self.path/'runtime',research_database=database,search_mode='hybrid'))
        profile=app.profile('U_1595');event=app.store.event('news_9f10b60aed544eaa32d4')
        try:
            first=context_fingerprint(app.settings,profile,event,date(2026,10,23),'cfo')[0]
            with database.open('ab') as stream:stream.write(b'changed-source')
            second=context_fingerprint(app.settings,profile,event,date(2026,10,23),'cfo')[0]
            self.assertNotEqual(first,second)
            from alfa_app.semantic import index_vectors,SearchUnavailable
            with self.assertRaises(SearchUnavailable):index_vectors(database)
            index.write_text(index.read_text('utf-8')+'\n',encoding='utf-8')
            third=context_fingerprint(app.settings,profile,event,date(2026,10,23),'cfo')[0]
            self.assertNotEqual(second,third)
        finally:app.jobs.close()
