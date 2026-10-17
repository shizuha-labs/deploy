from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from revocation_acceptance import wait_for_denial

class RevocationInstrumentTest(unittest.TestCase):
    def setUp(self):
        self.status = 401
        self.requests = []
        owner = self
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('Authorization')))
                self.send_response(owner.status)
                self.end_headers()
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_port}'

    def test_real_authentication_denial_is_required(self):
        wait_for_denial(self.base, '/api/private/', 'fixture-session', 1, timeout=0)
        self.assertEqual(self.requests, [('/api/private/', 'Bearer fixture-session')])

    def test_still_authorized_is_a_failure(self):
        self.status = 200
        with self.assertRaisesRegex(RuntimeError, 'remained authorized'):
            wait_for_denial(self.base, '/api/private/', 'fixture-session', 1, timeout=0)

    def test_route_and_server_failures_cannot_masquerade_as_revocation(self):
        for status in (404, 500, 503):
            self.status = status
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'not proof'):
                wait_for_denial(self.base, '/api/private/', 'fixture-session', 1, timeout=0)
