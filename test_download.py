import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {'SHIZUHA_INSTALLATION_DIR': '/tmp/unused', 'SHIZUHA_COMPOSE_PROJECT': 'test'}):
    spec = importlib.util.spec_from_file_location('acceptance_download', Path(__file__).with_name('acceptance.py'))
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)


class DownloadBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        records = self.requests
        class Storage(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                records.append((self.server.server_port, dict(self.headers)))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', foreign + '/object?signature=disposable-test')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'actual object bytes')
        servers = [ThreadingHTTPServer(('127.0.0.1', 0), Storage) for _ in range(2)]
        self.local = 'http://127.0.0.1:' + str(servers[0].server_port)
        foreign = self.foreign = 'http://127.0.0.1:' + str(servers[1].server_port)
        for server in servers:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.base = patch.object(acceptance, 'BASE', self.local)
        self.base.start()
        self.addCleanup(self.base.stop)

    def test_same_origin_download_receives_user_credentials(self):
        self.assertEqual(acceptance.download('/object', 'disposable-token', 7), b'actual object bytes')
        self.assertEqual(self.requests[-1][1]['Authorization'], 'Bearer disposable-token')
        self.assertEqual(self.requests[-1][1]['X-Organization-Id'], '7')

    def test_foreign_presigned_url_never_receives_platform_credentials(self):
        self.assertEqual(acceptance.download(self.foreign + '/object?signature=example', 'disposable-token', 7), b'actual object bytes')
        self.assertNotIn('Authorization', self.requests[-1][1])
        self.assertNotIn('X-Organization-Id', self.requests[-1][1])

    def test_platform_redirect_to_storage_drops_platform_credentials_on_wire(self):
        self.assertEqual(acceptance.download('/redirect', 'disposable-token', 7), b'actual object bytes')
        self.assertIn('Authorization', self.requests[0][1])
        self.assertNotIn('Authorization', self.requests[1][1])
        self.assertNotIn('X-Organization-Id', self.requests[1][1])

    def test_non_http_urls_are_rejected_before_request(self):
        for target in ('file:///etc/passwd', 'ftp://example.org/file', 'https://user:password@example.org/file'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                acceptance.download(target, 'disposable-token', 7)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
