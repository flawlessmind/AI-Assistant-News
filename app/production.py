"""Production WSGI transport and exclusive runtime ownership for persistent jobs."""
import hmac
import errno
from http import HTTPStatus
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
from urllib.parse import urlsplit,parse_qs
from .config import ROOT,require_safe_bind
from .semantic import SearchUnavailable
from .store import JobQueueFull
from .http_api import SECURITY_HEADERS,STATIC,allowed_host,allowed_origin,json_object,failure

logger=logging.getLogger(__name__)


def wsgi(application):
    def handle(environ,start_response):
        def respond(status,payload,content_type='application/json; charset=utf-8'):
            raw=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False).encode()
            start_response(str(status)+' '+HTTPStatus(status).phrase,
                           [('Content-Type',content_type),('Content-Length',str(len(raw)))]+SECURITY_HEADERS)
            return [] if environ.get('REQUEST_METHOD')=='HEAD' else [raw]
        try:
            host=environ.get('HTTP_HOST','')
            if not allowed_host(host,application.settings.allowed_hosts):
                return respond(400,{'error':'Адрес сервера не разрешён конфигурацией приложения.'})
            method=environ['REQUEST_METHOD']
            path=environ.get('PATH_INFO','')
            query=environ.get('QUERY_STRING','')
            if len(query)>4096:return respond(414,{'error':'Слишком длинный адрес запроса.'})
            if method in ('GET','HEAD'):
                if path in STATIC:
                    name,mime=STATIC[path]
                    return respond(200,(ROOT/'web'/name).read_bytes(),mime)
                if path=='/favicon.ico':return respond(204,b'')
                if not path.startswith('/api/'):
                    return respond(404,{'error':'Маршрут не найден.'})
                return respond(200,application.dispatch_get(path,parse_qs(query,keep_blank_values=(path=='/api/source'))))
            if method!='POST':return respond(405,{'error':'Метод не поддерживается.'})
            origin=environ.get('HTTP_ORIGIN')
            if not allowed_origin(origin,host):
                return respond(403,{'error':'Действия разрешены только из текущего приложения.'})
            if not hmac.compare_digest(environ.get('HTTP_X_ALFA_ACTION',''),application.csrf):
                return respond(403,{'error':'Недействительный контекст действия.'})
            if environ.get('CONTENT_TYPE','').split(';')[0].strip().lower()!='application/json':
                return respond(415,{'error':'Ожидается application/json.'})
            length=int(environ.get('CONTENT_LENGTH','0'))
            if not 1<=length<=65536:return respond(413,{'error':'Размер запроса должен быть от 1 до 65536 байт.'})
            raw=environ['wsgi.input'].read(length)
            if len(raw)!=length:raise ValueError('Incomplete body')
            body=json_object(raw)
            result=application.dispatch_post(path,body)
            return respond(202 if path=='/api/jobs' or body.get('async') is True and path in ('/api/insights','/api/refresh') else 200,result)
        except Exception as error:
            status,payload=failure(error,environ.get('REQUEST_METHOD'),environ.get('PATH_INFO'))
            return respond(status,payload)
    return handle


class RuntimeInUse(OSError):
    pass


class RuntimeLock:
    def __init__(self,runtime):
        self.path=Path(runtime)/'server.lock'
        self.stream=None

    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        stream=self.path.open('a+b')
        if not self.path.stat().st_size:
            stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeInUse('Эта runtime-папка уже используется другим сервером. Откройте работающий сервер или остановите его перед повторным запуском.')
        self.stream=stream

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream=None


class ProductionServer:
    def __init__(self,application,host,port,own_runtime=False):
        require_safe_bind(host,application.settings.allow_remote_bind)
        from waitress.server import create_server
        self.application=application
        self.runtime_lock=RuntimeLock(application.settings.runtime_dir) if own_runtime else None
        self.server=None
        self.stopped=False
        self.closed=False
        bound=None
        try:
            if self.runtime_lock:self.runtime_lock.acquire()
            bound=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            if os.name=='nt':bound.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            else:bound.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            bound.bind((host,port))
            bound.listen(128)
            self.server=create_server(wsgi(application),sockets=[bound],threads=8,
                channel_timeout=30,connection_limit=128,max_request_header_size=16384,
                max_request_body_size=65536,expose_tracebacks=False,ident='AlfaNewsInsights',
                asyncore_loop_timeout=0.1)
            self.server_port=int(self.server.effective_port)
            if own_runtime:application.store.recover_jobs()
        except BaseException:
            if bound:bound.close()
            if self.runtime_lock:self.runtime_lock.close()
            raise

    def serve_forever(self):
        try:self.server.run()
        except OSError as error:
            # Closing the server from another thread can race with select().
            # Ignore only the expected closed descriptor during our own stop.
            if not self.stopped or (error.errno!=errno.EBADF and getattr(error,'winerror',None)!=10038):raise

    def shutdown(self):
        if self.stopped:return
        self.stopped=True
        self.application.jobs.request_stop()
        # Workers signal the event loop after sending a response. Drain them
        # before closing its trigger, otherwise Linux reports EBADF on exit.
        self.server.task_dispatcher.shutdown()
        self.server.close()
        for channel in list(self.server.active_channels.values()):channel.close()

    def server_close(self):
        if self.closed:return
        self.closed=True
        self.shutdown()
        self.application.jobs.close()
        if self.runtime_lock:self.runtime_lock.close()


def make_production_server(application,host='127.0.0.1',port=8000,own_runtime=False):
    return ProductionServer(application,host,port,own_runtime=own_runtime)
