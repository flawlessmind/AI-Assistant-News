"""HEAD has GET headers and an empty body in WSGI and both HTTP transports."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from alfa_app.config import ROOT,Settings
from alfa_app.server import Application,make_server
from alfa_app.production import wsgi,make_production_server


class HeadTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.cache/tests').mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        self.app=Application(Settings(runtime_dir=Path(self.tmp.name)))
    def tearDown(self):self.app.jobs.close();self.tmp.cleanup()

    def test_wsgi_returns_no_body_for_static_json_and_error_head_responses(self):
        handler=wsgi(self.app)
        for path,host in [('/','127.0.0.1'),('/api/health','127.0.0.1'),('/not-found','127.0.0.1'),('/','attacker.invalid')]:
            with self.subTest(path=path,host=host):
                responses=[]
                def start(status,headers):responses.append((status,dict(headers)))
                environment={'REQUEST_METHOD':'GET','HTTP_HOST':host,'PATH_INFO':path,'QUERY_STRING':'','wsgi.input':io.BytesIO()}
                get_body=b''.join(handler(environment,start))
                head_body=b''.join(handler(dict(environment,REQUEST_METHOD='HEAD'),start))
                self.assertTrue(get_body);self.assertEqual(head_body,b'')
                self.assertEqual(responses[0],responses[1])

    def test_get_and_head_share_status_content_type_and_length_in_both_servers(self):
        for factory in (make_server,make_production_server):
            with self.subTest(factory=factory.__name__):
                server=factory(self.app,port=0);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    base='http://127.0.0.1:'+str(server.server_port)
                    for path in ('/','/app.js','/api/health'):
                        with opener.open(base+path,timeout=3) as response:
                            body=response.read();length=response.headers['Content-Length'];mime=response.headers['Content-Type'];status=response.status
                        with opener.open(urllib.request.Request(base+path,method='HEAD'),timeout=3) as response:
                            self.assertEqual(response.read(),b'');self.assertEqual(response.status,status)
                            self.assertEqual(response.headers['Content-Length'],length)
                            self.assertEqual(response.headers['Content-Type'],mime)
                            self.assertEqual(int(length),len(body))
                finally:server.shutdown();server.server_close();worker.join(3)
