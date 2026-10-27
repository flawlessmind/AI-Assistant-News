"""Local HTTP regression checks; no model, application database or external network."""
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alfa_app.__main__ import main
from alfa_app.server import make_server


class ServerPortTests(unittest.TestCase):
    def test_duplicate_port_is_rejected_and_original_server_still_responds(self):
        server=make_server(object(),port=0)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            if os.name=='nt':
                self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR),0)
                self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE),1)
            duplicate=None
            try:
                with self.assertRaises(OSError):
                    duplicate=make_server(object(),port=server.server_port)
            finally:
                if duplicate is not None:
                    duplicate.server_close()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{server.server_port}/',timeout=5) as response:
                self.assertEqual(response.status,200)
                self.assertIn(b'<!doctype html>',response.read().lower())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def test_cli_reports_unavailable_address_without_success_banner(self):
        for port,alternative,error in [(8000,8002,errno.EADDRINUSE),(8002,8003,errno.EACCES)]:
            with self.subTest(port=port,error=error):
                output,errors=io.StringIO(),io.StringIO()
                with patch('sys.argv',['alfa_app','serve','--port',str(port)]), \
                     patch('alfa_app.__main__.Settings.from_env'), \
                     patch('alfa_app.__main__.Application'), \
                     patch('alfa_app.__main__.make_production_server',side_effect=OSError(error,'unavailable')), \
                     redirect_stdout(output),redirect_stderr(errors):
                    result=main()
                self.assertEqual(result,1)
                self.assertEqual(output.getvalue(),'')
                self.assertIn(f'127.0.0.1:{port}',errors.getvalue())
                self.assertIn('занят или недоступен',errors.getvalue())
                self.assertIn(f'--port {alternative}',errors.getvalue())


if __name__=='__main__':
    unittest.main()
