from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from object_storage_acceptance import require_private_object


class ObjectPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.unsigned_status = self.altered_status = 403
        owner = self
        class Storage(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                query = parse_qs(urlsplit(self.path).query)
                self.send_response(owner.altered_status if 'X-Amz-Signature' in query else owner.unsigned_status)
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Storage)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.signature = secrets.token_hex(32)
        self.url = f'http://127.0.0.1:{server.server_port}/bucket/object?X-Amz-Signature={self.signature}&X-Amz-Expires=60'

    def test_both_negative_requests_are_real_and_have_no_platform_credentials(self):
        require_private_object(self.url)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0][0], '/bucket/object')
        changed = parse_qs(urlsplit(self.requests[1][0]).query)
        self.assertNotEqual(changed['X-Amz-Signature'], [self.signature])
        self.assertEqual(changed['X-Amz-Expires'], ['60'])
        for _, headers in self.requests:
            self.assertNotIn('Authorization', headers)
            self.assertNotIn('X-Organization-Id', headers)

    def test_instrument_rejects_an_anonymously_readable_object(self):
        self.unsigned_status = 200
        with self.assertRaisesRegex(RuntimeError, 'unsigned.*got 200'):
            require_private_object(self.url)

    def test_instrument_rejects_wrong_failure_status_for_altered_signature(self):
        self.altered_status = 500
        with self.assertRaisesRegex(RuntimeError, 'altered-signature.*got 500'):
            require_private_object(self.url)

    def test_missing_signature_is_not_a_negative_control(self):
        with self.assertRaises(ValueError):
            require_private_object(self.url.split('?')[0])
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
