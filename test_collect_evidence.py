import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('collect_evidence', Path(__file__).with_name('collect_evidence.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReviewArtifactTest(unittest.TestCase):
    def test_only_explicit_browser_and_receipt_artifacts_are_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'browser-evidence').mkdir()
            (root / 'receipt.json').write_text('{}')
            (root / 'gates.json').write_text('{}')
            (root / 'secrets.env').write_text('PRIVATE=value')
            (root / 'browser-evidence' / 'app.png').write_bytes(b'image')
            (root / 'browser-evidence' / 'trace.zip').write_bytes(b'private login body')
            (root / 'browser-evidence' / 'linked.png').symlink_to(root / 'secrets.env')
            with tarfile.open(module.collect(root)) as archive:
                self.assertEqual(set(archive.getnames()), {'receipt.json', 'gates.json', 'browser-evidence/app.png'})
            self.assertTrue((root / 'review-evidence-ready').is_file())
