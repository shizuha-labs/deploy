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




class NotesBackingObjectTest(unittest.TestCase):
    def setUp(self):
        import ast
        import importlib.util
        import os
        from pathlib import Path
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.attachment = '00000000-0000-4000-8000-000000000001'
        self.note = '00000000-0000-4000-8000-000000000002'
        self.requests = []
        self.unsigned = self.altered = 403
        self.body = b'Notes externally signed attachment acceptance\n'
        self.bad_identity = self.bad_bytes = False
        self.owner, self.outsider = 'synthetic-owner', 'synthetic-outsider'
        test = self
        class Server(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                parsed = urlsplit(self.path)
                test.requests.append((self.path, dict(self.headers)))
                if parsed.path.startswith('/bucket/'):
                    signature = parse_qs(parsed.query).get('X-Amz-Signature', [])
                    status = (200 if signature == ['a' * 64] else test.altered) if signature else test.unsigned
                    self.send_response(status); self.end_headers()
                    if status == 200: self.wfile.write(b'wrong' if test.bad_bytes else test.body)
                else:
                    allowed = self.headers.get('Authorization') == 'Bearer ' + test.owner
                    self.send_response(200 if allowed else 403); self.end_headers()
                    if allowed:
                        if parsed.path.endswith('/file/'): self.wfile.write(test.body)
                        else: self.wfile.write(b'{}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_port}'
        self.signed = self.base + '/bucket/object?X-Amz-Signature=' + 'a' * 64
        self.edge = self.base + '/notes/api/attachments/' + self.attachment + '/file/'
        # Run the actual generated python -c program in a fresh process. Only
        # the ORM fixture is synthetic; lookups must bind all three identities.
        (self.root / 'django.py').write_text("import os\ndef setup():\n assert os.environ['DJANGO_SETTINGS_MODULE'] == 'notesmanager.settings'\n")
        (self.root / 'notes').mkdir(); (self.root / 'notes/__init__.py').write_text('')
        (self.root / 'notes/models.py').write_text(f'''from types import SimpleNamespace
class Manager:
 def get(self, **kwargs):
  assert kwargs == {{'pk': {self.attachment!r}, 'note_id': {self.note!r}, 'note__organization_id': 7}}
  class Storage:
   def url(self, name):
    assert name == 'exact-uploaded-object'
    return {self.signed!r}
  return SimpleNamespace(pk={self.attachment!r}, note_id={self.note!r}, file=SimpleNamespace(name='exact-uploaded-object', storage=Storage()))
class Attachment: objects = Manager()
''')
        os.environ.setdefault('SHIZUHA_INSTALLATION_DIR', '/unused-test-install')
        os.environ.setdefault('SHIZUHA_COMPOSE_PROJECT', 'test-project')
        spec = importlib.util.spec_from_file_location('notes_acceptance_under_test', Path(__file__).with_name('acceptance.py'))
        self.acceptance = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.acceptance)
        self.acceptance.BASE = self.base
        tree = ast.parse(Path(self.acceptance.__file__).read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        def assigns(n, name): return isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
        start = next(i for i,n in enumerate(main.body) if assigns(n, 'notebook'))
        end = next(i for i,n in enumerate(main.body) if assigns(n, 'conversation'))
        self.block = compile(ast.Module(body=main.body[start:end], type_ignores=[]), '<actual Notes acceptance caller>', 'exec')

    def run_caller(self, **changes):
        import contextlib
        import io
        import os
        import subprocess
        import sys
        from unittest.mock import patch
        actual_run = subprocess.run
        seen = []
        def execute(args, **kwargs):
            self.assertEqual(args[:6], self.acceptance.COMPOSE[:6])
            self.assertEqual(args[len(self.acceptance.COMPOSE):len(self.acceptance.COMPOSE)+5], ['exec','-T','notes','python','-c'])
            code, identities = args[-4], args[-3:]
            self.assertEqual(identities, [self.attachment, self.note, '7'])
            seen.append(True)
            env = dict(os.environ, PYTHONPATH=str(self.root))
            result = actual_run([sys.executable, '-c', code] + identities, env=env, **kwargs)
            if self.bad_identity and result.returncode == 0:
                import json
                body = json.loads(result.stdout); body['attachment_id'] = self.note
                result.stdout = json.dumps(body).encode()
            return result
        edge = changes.get('file_url', self.edge)
        response = {'id': self.attachment, 'file_url': edge, 'file': changes.get('file', edge)}
        original_api = self.acceptance.api
        def api(method, path, *args, **kwargs):
            if method == 'POST':return {'id': self.note}
            return original_api(method,path,*args,**kwargs)
        scope = dict(vars(self.acceptance), owner=self.owner, outsider=self.outsider, org=7, other_org=8, api=api,
                     upload=lambda *args, **kwargs: response)
        output=io.StringIO()
        with patch('subprocess.run', side_effect=execute), contextlib.redirect_stdout(output):
            exec(self.block, scope)
        self.assertNotIn(self.signed, output.getvalue())
        return seen

    def test_actual_notes_caller_uses_authenticated_edge_and_exact_backing_object(self):
        self.assertEqual(self.run_caller(), [True])
        public = [(path, headers) for path,headers in self.requests if '/bucket/' not in path]
        self.assertTrue(any(headers.get('Authorization') == 'Bearer '+self.owner for _,headers in public))
        self.assertTrue(any(headers.get('Authorization') == 'Bearer '+self.outsider for _,headers in public))
        self.assertTrue(any('Authorization' not in headers for _,headers in public))
        storage = [(path,headers) for path,headers in self.requests if '/bucket/' in path]
        self.assertEqual(len(storage),3)
        for _,headers in storage:
            self.assertNotIn('Authorization', headers);self.assertNotIn('X-Organization-Id',headers)

    def test_public_api_cannot_return_raw_signed_url_or_legacy_alias(self):
        for changes in ({'file_url':self.signed}, {'file':self.signed},
                        {'file_url':self.base + '/notes/api/attachments/' + self.note + '/file/'},
                        {'file_url':self.edge + '#other-object'}):
            with self.subTest(changes=list(changes)), self.assertRaises(RuntimeError):self.run_caller(**changes)

    def test_object_identity_bytes_and_private_controls_fail_closed(self):
        for kind in ('identity','bytes','unsigned','altered'):
            with self.subTest(kind=kind):
                self.bad_identity = kind == 'identity';self.bad_bytes = kind == 'bytes'
                self.unsigned = 200 if kind == 'unsigned' else 403
                self.altered = 200 if kind == 'altered' else 403
                with self.assertRaisesRegex(RuntimeError,'Notes backing object privacy proof failed') as error:self.run_caller()
                self.assertNotIn(self.signed,str(error.exception))

    def test_bad_attachment_identity_never_runs_compose(self):
        from unittest.mock import patch
        from object_storage_acceptance import require_private_notes_attachment
        with patch('subprocess.run') as run:
            for values in [('not-uuid',self.note,7),(self.attachment,self.note,False),(self.attachment,self.note,'')]:
                with self.subTest(values=values),self.assertRaises(ValueError):require_private_notes_attachment([], *values, self.body)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
