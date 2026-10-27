"""Local demo server. JSON API, same-origin actions, synthetic demo context only."""
from datetime import date
from functools import partial
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import re
import socket
import sqlite3
import threading
from urllib.parse import urlsplit, parse_qs
from alfa_research import open_database
from .agent import Agent,context_fingerprint,PROMPT_VERSION
from .config import ROOT, Settings, now_msk,budget_period,require_safe_bind
from .llm import LLMClient
from .ranking import ranked_feed
from .store import Store,JobQueueFull
from .tasks import task_for
from .semantic import SearchUnavailable,rank_events,search_signature
from .jobs import Jobs
from .http_api import SECURITY_HEADERS,STATIC,allowed_host,allowed_origin,json_object,failure


class AnalysisInProgress(Exception):
    pass


class Application:
    def __init__(self, settings=None, llm=None, seed=True):
        self.settings=settings or Settings.from_env()
        self.store=Store(self.settings.runtime_dir/'service.sqlite')
        self.llm=llm or LLMClient(self.settings,self.store)
        self.agent=Agent(self.settings,self.store,self.llm)
        self.search=search_signature(self.settings.research_database,self.settings.search_mode)
        self._active=set()
        self._active_lock=threading.Lock()
        # Leave HTTP workers available for status, progress and other actions
        # while older clients wait for synchronous publisher requests.
        self._sync_waiters=threading.BoundedSemaphore(2)
        self.csrf=secrets.token_urlsafe(24)
        self.profiles={}
        for path in sorted((ROOT/'data').glob('company_U_*.json')):
            profile=json.loads(path.read_text(encoding='utf-8'))
            self.profiles[profile['company_id']]=profile
        if seed:
            bundled_events={}
            for snapshot in (ROOT/'data/news_snapshot.jsonl',ROOT/'data/news_current.jsonl'):
                if snapshot.is_file():
                    for line in snapshot.read_text(encoding='utf-8').splitlines():
                        if line.strip():
                            event=json.loads(line)
                            self.store.put_event(event, update_existing=False)
                            bundled_events[event['event_id']]=event
            migration=ROOT/'data/news_cleanup_2026-10-23.json'
            if migration.is_file():
                for revision in json.loads(migration.read_text(encoding='utf-8'))['revisions']:
                    event=bundled_events.get(revision['event_id'])
                    if event and event['content_sha256']==revision['new_content_sha256']:
                        self.store.apply_event_revision(event,revision['previous_content_sha256'])
        self.jobs=Jobs(self)

    def profile(self, company_id):
        if not isinstance(company_id,str) or company_id not in self.profiles:
            raise ValueError('Компания не найдена в доступном наборе.')
        return self.profiles[company_id]

    def status(self):
        return {'application':'Alfa News Insights','llm':self.settings.public(),'csrf_token':self.csrf,
                'analysis_version':PROMPT_VERSION,
                'news_count':len(self.store.events()),'demo_session':True,
                'search':dict(self.search,ready=True),
                'notice':'Демонстрационный выбор компании и роли; реальная банковская сессия не подключена.'}

    def current_parent(self,parent_id,profile,event,as_of,role):
        if not parent_id:
            return None
        candidate=self.store.run(str(parent_id))
        if (not candidate or candidate.get('status')!='complete'
                or candidate.get('company_id')!=profile['company_id']
                or candidate.get('role')!=role
                or candidate.get('event',{}).get('event_id')!=event['event_id']
                or candidate.get('analysis_as_of')!=str(as_of)):
            return None
        expected=context_fingerprint(self.settings,profile,event,as_of,role,
                                     candidate.get('question',''),candidate.get('task'))[0]
        return candidate if candidate.get('context_fingerprint')==expected else None

    def dispatch_get(self,path,query):
        first=lambda key,default=None: query.get(key,[default])[0]
        if path=='/api/status':
            return self.status()
        if path=='/api/health':
            return {'ready':True,'search':dict(self.search,ready=True),'profiles':len(self.profiles),
                    'news_count':len(self.store.events()),'llm_configured':bool(self.settings.api_key)}
        if path=='/api/metrics':
            result=self.store.metrics(hashlib.sha256(self.settings.api_key.encode()).hexdigest(),budget_period())
            return dict(result,local_call_cap=self.settings.max_calls_per_period,
                        calls_remaining=(max(0,self.settings.max_calls_per_period-result['physical_attempts'])
                                         if self.settings.max_calls_per_period is not None else None))
        if path.startswith('/api/jobs/'):
            return self.jobs.public(path.rsplit('/',1)[-1])
        if path=='/api/companies':
            return [{'company_id':p['company_id'],'company':p['company'],
                     'network':p.get('network',{}),
                     'payments':{key:value for key,value in p.get('payments',{}).items()
                                 if key.endswith('counterparty_count') or key=='self_transfer_relation_rows'}}
                    for p in self.profiles.values()]
        if path.startswith('/api/companies/'):
            return self.profile(path.rsplit('/',1)[-1])
        if path in ('/api/news','/api/search'):
            profile=self.profile(first('company_id'))
            as_of=date.fromisoformat(first('as_of',str(now_msk().date())))
            role=first('role','cfo')
            if role not in ('cfo','ceo'):
                raise ValueError('Неизвестная роль.')
            rows=ranked_feed(profile,self.store.events(),as_of,role)
            for row in rows:
                row['context_fingerprint']=context_fingerprint(self.settings,profile,row['event'],as_of,role)[0]
            states=self.store.main_analysis_states(profile['company_id'],[row['context_fingerprint'] for row in rows])
            for row in rows:
                row.update(states.get(row['context_fingerprint'],{'cached_run_id':None,'latest_run_id':None,'latest_status':None}))
            if path=='/api/search':
                query=first('q','').strip()
                if not query or len(query)>500:
                    raise ValueError('Поисковый запрос должен содержать от 1 до 500 символов.')
                if self.settings.search_mode=='lexical':
                    rows=[row for row in rows if query.casefold() in
                          (row['event']['title']+' '+row['event'].get('text','')).casefold()]
                else:
                    by_id={row['event']['event_id']:row for row in rows}
                    rows=[dict(by_id[hit['event_id']],search_match=hit) for hit in
                          rank_events(query,[row['event'] for row in rows],self.settings.search_mode)]
                return {'items':rows,'as_of':str(as_of),'query':query,'search_mode':self.settings.search_mode}
            return {'items':rows,'as_of':str(as_of),'analysis_version':PROMPT_VERSION}
        if path.startswith('/api/runs/'):
            run=self.store.run(path.rsplit('/',1)[-1])
            if run is None:
                raise ValueError('Запуск не найден.')
            profile=self.profile(run['company_id'])
            current=self.store.event(run['event']['event_id'])
            parent=self.current_parent(run.get('parent_run_id'),profile,current,
                     date.fromisoformat(run['analysis_as_of']),run['role']) if current else None
            valid=current and context_fingerprint(self.settings,profile,current,date.fromisoformat(run['analysis_as_of']),
                   run['role'],run.get('question',''),run.get('task'),parent)[0]==run.get('context_fingerprint')
            if not valid:
                # Keep the historical record unchanged, but do not present its approval as current.
                return dict(run,status='stale_result',historical_status=run['status'],insight=None,answer=None,
                            stop_detail={'message':'Сохранённый ответ относится к прежней версии контекста или проверки.',
                                         'next_step':'Открыть новость и выполнить актуальную проверку.'})
            return run
        if path=='/api/source':
            source_id=first('source_id')
            page=int(first('page','1'))
            if not 1<=page<=9223372036854775807:
                raise ValueError('Invalid physical page number')
            connection=open_database(self.settings.research_database)
            try:
                row=connection.execute('SELECT p.text,d.metadata FROM pages p JOIN documents d ON d.source_id=p.source_id WHERE p.source_id=? AND p.page=?',(source_id,page)).fetchone()
                if not row:
                    raise ValueError('Страница не найдена.')
                return {'text':row['text'],'page':page,'metadata':json.loads(row['metadata']),
                        'warning':'OCR-текст, числа не прошли сплошную ручную проверку.'}
            finally:
                connection.close()
        raise ValueError('Маршрут не найден.')

    def dispatch_post(self,path,body):
        if 'async' in body and type(body['async']) is not bool:
            raise ValueError('async must be boolean')
        if path=='/api/refresh':
            limit=body.get('limit',5)
            if type(limit) is not int or not 1<=limit<=15:raise ValueError('Invalid source limit')
            request={'operation':'refresh','limit':limit}
            if body.get('async') is True:return self.jobs.submit(request)
            return self._synchronous_job(request)
        if path=='/api/jobs':
            return self.jobs.submit(body)
        if path=='/api/feedback':
            feedback_id=body.get('feedback_id','')
            run_id=body.get('run_id','')
            rating=body.get('rating')
            comment=body.get('comment','')
            if (not isinstance(feedback_id,str) or not re.fullmatch('[a-f0-9]{32}',feedback_id)
                or not isinstance(run_id,str) or not re.fullmatch('[a-f0-9]{32}',run_id)
                or rating not in ('helpful','incorrect','irrelevant')
                or not isinstance(comment,str) or len(comment)>1000 or not self.store.run(run_id)):
                raise ValueError('Invalid feedback')
            return self.store.save_feedback(feedback_id,run_id,rating,comment.strip())
        if path=='/api/insights':
            if body.get('async') is True:return self.jobs.submit(body)
            if body.get('mode')=='live':return self._synchronous_job(body)
            return self.analyse(body)
        raise ValueError('Маршрут не найден.')

    def _synchronous_job(self,request):
        if not self._sync_waiters.acquire(blocking=False):raise JobQueueFull('Synchronous waiters are full')
        try:
            return self.jobs.wait(self.jobs.submit(request)['job_id'])
        finally:
            self._sync_waiters.release()

    def analysis_context(self,body):
        profile=self.profile(body.get('company_id'))
        event_id=body.get('event_id','')
        if not isinstance(event_id,str) or not 1<=len(event_id)<=256:raise ValueError('Invalid event id')
        event=self.store.event(event_id)
        if event is None:
            raise ValueError('Событие не найдено.')
        mode=body.get('mode','sources')
        if mode not in ['sources','live']:
            raise ValueError('Режим должен быть sources или live.')
        regenerate=body.get('regenerate',False)
        request_id=body.get('request_id')
        if type(regenerate) is not bool or (request_id is not None and
                (not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id))):
            raise ValueError('Некорректный идентификатор попытки анализа.')
        if regenerate and (mode!='live' or not request_id):
            raise ValueError('Для новой генерации нужен идентификатор попытки и режим live.')
        role=body.get('role','cfo')
        if role not in ['cfo','ceo']:
            raise ValueError('Неизвестная роль.')
        question=body.get('question','')
        if not isinstance(question,str) or len(question)>500:
            raise ValueError('Уточняющий вопрос должен содержать до 500 символов.')
        task=body.get('task')
        if task is not None and not isinstance(task,str):raise ValueError('Неизвестная задача.')
        task=task_for(question,task)
        as_of=date.fromisoformat(body.get('as_of',str(now_msk().date())))
        parent_id=body.get('parent_run_id')
        parent=self.current_parent(parent_id,profile,event,as_of,role) if task=='explain' else None
        if parent_id and task=='explain' and not parent:
            raise ValueError('Связанный анализ устарел или не соответствует выбранному контексту.')
        fingerprint=context_fingerprint(self.settings,profile,event,as_of,role,question,task,parent)[0]
        return dict(profile=profile,event=event,mode=mode,role=role,question=question,task=task,
                    as_of=as_of,parent=parent,fingerprint=fingerprint,regenerate=regenerate,request_id=request_id)

    def analyse(self,body,progress=None):
        context=self.analysis_context(body)
        profile,event,mode,role,question,task,as_of,parent,fingerprint=(context[key] for key in
            ('profile','event','mode','role','question','task','as_of','parent','fingerprint'))
        if mode=='live' and not context['regenerate']:
            saved=self.store.current_result(profile['company_id'],fingerprint)
            if saved:
                return dict(saved,restored_from_cache=True,trace=saved['trace']+[
                    {'step':'restore_result','reason':'Готовый результат загружен из runtime-БД; новых запросов к LLM нет.'}])
        candidate=None
        if body.get('candidate_run_id') and not context['regenerate']:
            candidate=self.store.run(str(body['candidate_run_id']))
        elif mode=='live' and not context['regenerate']:
            candidate=self.store.draft_candidate(profile['company_id'],event['event_id'],role,as_of,question,task)
        guard_key=None
        if mode=='live' and task in ('insight','explain'):
            guard_key=fingerprint+(':regenerate:'+context['request_id'] if context['regenerate'] else '')
            with self._active_lock:
                if guard_key in self._active:
                    raise AnalysisInProgress()
                saved=None if context['regenerate'] else self.store.current_result(profile['company_id'],fingerprint)
                if saved:
                    return dict(saved,restored_from_cache=True,trace=saved['trace']+[
                        {'step':'restore_result','reason':'Готовый результат загружен из runtime-БД; новых запросов к LLM нет.'}])
                self._active.add(guard_key)
        try:
            options=dict(live=mode=='live',question=question,task=task,parent=parent,candidate_run=candidate)
            if context['regenerate']:options['request_nonce']=context['request_id']
            if progress:options['progress']=progress
            return self.agent.run(profile,event,as_of,role,**options)
        finally:
            if guard_key:
                with self._active_lock:
                    self._active.discard(guard_key)


class Handler(BaseHTTPRequestHandler):
    def __init__(self,*args,application,**kwargs):
        self.application=application
        super().__init__(*args,**kwargs)

    def log_message(self,format,*args):
        # No request body, authorization or query strings in logs.
        return

    def send_payload(self,status,payload,content_type='application/json; charset=utf-8'):
        raw=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False).encode()
        if status>=400:self.drain_rejected_body()
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)))
        for name,value in SECURITY_HEADERS:self.send_header(name,value)
        self.end_headers()
        if getattr(self,'command',None)!='HEAD':self.wfile.write(raw)

    def drain_rejected_body(self):
        """Avoid a TCP reset hiding an early JSON rejection when the body arrives late."""
        if getattr(self,'_body_handled',False):return
        self._body_handled=True
        headers=getattr(self,'headers',None)
        if headers is None or len(headers.get_all('Content-Length',[]))!=1 or headers.get('Transfer-Encoding'):return
        try:length=int(headers['Content-Length'])
        except ValueError:return
        if not 1<=length<=65536:return
        previous=self.connection.gettimeout()
        self.connection.settimeout(1)
        try:self.rfile.read(length)
        except (TimeoutError,ConnectionError):self.close_connection=True
        finally:self.connection.settimeout(previous)

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def send_error(self,code,message=None,explain=None):
        # BaseHTTPRequestHandler otherwise reflects protocol errors in an HTML page.
        self.close_connection=True
        self.send_payload(code,{'error':'Некорректный HTTP-запрос.'})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    do_PUT=do_POST
    do_PATCH=do_POST
    do_DELETE=do_POST
    do_OPTIONS=do_POST

    def handle_api(self):
        path=''
        self._body_handled=False
        try:
            host=self.headers.get('Host','')
            hosts=getattr(getattr(self.application,'settings',None),'allowed_hosts',('127.0.0.1','localhost','::1'))
            if len(self.headers.get_all('Host',[]))!=1 or not allowed_host(host,hosts):
                return self.send_payload(400,{'error':'Адрес сервера не разрешён конфигурацией приложения.'})
            parts=urlsplit(self.path);path=parts.path
            if parts.scheme or parts.netloc:raise ValueError('Expected origin form')
            if len(parts.query)>4096:return self.send_payload(414,{'error':'Слишком длинный адрес запроса.'})
            if self.command in ('GET','HEAD'):
                if path in STATIC:
                    filename,mime=STATIC[path]
                    return self.send_payload(200,(ROOT/'web'/filename).read_bytes(),mime)
                if path=='/favicon.ico':return self.send_payload(204,b'')
                if not path.startswith('/api/'):return self.send_payload(404,{'error':'Маршрут не найден.'})
                return self.send_payload(200,self.application.dispatch_get(path,parse_qs(parts.query,keep_blank_values=(path=='/api/source'))))
            if self.command!='POST':return self.send_payload(405,{'error':'Метод не поддерживается.'})
            if not allowed_origin(self.headers.get('Origin'),host):
                return self.send_payload(403,{'error':'Действия разрешены только из текущего приложения.'})
            if not hmac.compare_digest(self.headers.get('X-Alfa-Action',''),self.application.csrf):
                return self.send_payload(403,{'error':'Недействительный контекст действия.'})
            if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
                return self.send_payload(415,{'error':'Ожидается application/json.'})
            if len(self.headers.get_all('Content-Length',[]))>1 or self.headers.get('Transfer-Encoding'):
                raise ValueError('Ambiguous body framing')
            length=int(self.headers.get('Content-Length','0'))
            if not 1<=length<=65536:
                return self.send_payload(413,{'error':'Размер запроса должен быть от 1 до 65536 байт.'})
            self._body_handled=True
            raw=self.rfile.read(length)
            if len(raw)!=length:raise ValueError('Incomplete body')
            body=json_object(raw)
            result=self.application.dispatch_post(path,body)
            self.send_payload(200,result)
        except (BrokenPipeError,ConnectionResetError):
            return
        except Exception as error:
            status,payload=failure(error,self.command,path)
            try:self.send_payload(status,payload)
            except (BrokenPipeError,ConnectionResetError):pass


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        if os.name=='nt':
            # Windows otherwise permits a second SO_REUSEADDR listener on the
            # same address, leaving requests routed to an arbitrary process.
            self.allow_reuse_address=False
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,0)
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


def make_server(application,host='127.0.0.1',port=8000):
    require_safe_bind(host,getattr(getattr(application,'settings',None),'allow_remote_bind',False))
    return LocalHTTPServer((host,port),partial(Handler,application=application))
