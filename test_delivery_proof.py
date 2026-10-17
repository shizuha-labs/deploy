import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from delivery_proof import capture_inputs, from_archive


class DeliveryProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = {'schema': 1, 'images': {}, 'sources': {'runtime': {'url': 'https://github.com/example/runtime.git', 'commit': 'a' * 40}}}
        self.installer = {'schema': 1, 'url': 'https://github.com/example/installer.git', 'commit': 'b' * 40, 'tree': 'c' * 40}
        (self.root / 'source-lock.json').write_text(json.dumps(self.lock))
        (self.root / 'public-installer.json').write_text(json.dumps(self.installer))

    def archive(self, *, private=False, mutate=None, duplicate=False):
        capture_inputs(self.root / 'source-lock.json', self.root, private)
        payloads = {'delivery-input.json': (self.root / 'delivery-input.json').read_bytes(),
                    'gates.json': b'{"runtime-agent":{"status":"passed"}}'}
        receipt = {'schema': 1, 'exit_code': 0, 'files': {
            name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in payloads.items()}}
        payloads['receipt.json'] = json.dumps(receipt).encode()
        if mutate:
            mutate(payloads)
        path = self.root / 'review.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in payloads.items():
                info = tarfile.TarInfo(name); info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
                if duplicate and name == 'gates.json':
                    archive.addfile(info, io.BytesIO(data))
        return path

    def test_public_proof_binds_actual_inputs_and_verified_archive(self):
        path = self.archive()
        proof = from_archive(path, 123, 'candidate-1', 'd' * 64)
        self.assertEqual(proof['mode'], 'public')
        self.assertEqual(proof['source_lock'], self.lock)
        self.assertEqual(proof['installer'], self.installer)
        self.assertEqual(proof['archive_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(proof['run_id'], 123)

    def test_private_is_not_mislabeled_anonymous(self):
        proof = from_archive(self.archive(private=True), 123, 'candidate-1', 'd' * 64)
        self.assertEqual(proof['mode'], 'private')
        self.assertIsNone(proof['installer'])

    def test_changed_gates_fail_receipt_hash(self):
        path = self.archive(mutate=lambda values: values.update({'gates.json': b'{}'}))
        with self.assertRaisesRegex(ValueError, 'does not match receipt'):
            from_archive(path, 123, 'candidate-1', 'd' * 64)

    def test_missing_input_fails_closed(self):
        path = self.archive(mutate=lambda values: values.pop('delivery-input.json'))
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            from_archive(path, 123, 'candidate-1', 'd' * 64)

    def test_duplicate_member_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            from_archive(self.archive(duplicate=True), 123, 'candidate-1', 'd' * 64)

    def test_public_requires_verified_installer_marker(self):
        (self.root / 'public-installer.json').unlink()
        with self.assertRaises(FileNotFoundError):
            capture_inputs(self.root / 'source-lock.json', self.root, False)

    def test_unexpected_source_lock_fields_are_not_logged(self):
        self.lock['unexpected_private_content'] = 'should not become provenance'
        (self.root / 'source-lock.json').write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(ValueError, 'Unexpected source lock'):
            capture_inputs(self.root / 'source-lock.json', self.root, False)
