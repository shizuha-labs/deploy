import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance_gates import Gates


class GateTests(unittest.TestCase):
    def test_real_failed_process_does_not_skip_independent_journey(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'gates.json'
            marker = Path(directory) / 'continued'
            gates = Gates(receipt)
            self.assertFalse(gates.run('browser', lambda: subprocess.run(
                [sys.executable, '-c', 'raise SystemExit(7)'], check=True)))
            self.assertTrue(gates.run('runtime', lambda: marker.write_text('ran')))
            self.assertTrue(marker.exists())
            with self.assertRaisesRegex(RuntimeError, 'browser'):
                gates.finish()
            self.assertEqual(json.loads(receipt.read_text())['runtime']['status'], 'passed')

    def test_blocked_dependency_fails_overall_and_clean_run_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            gates = Gates(Path(directory) / 'gates.json')
            gates.run('mail', lambda: None)
            gates.finish()
            gates.blocked('notes', 'browser-dependencies')
            with self.assertRaisesRegex(RuntimeError, 'notes'):
                gates.finish()

    def test_aggregate_failure_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            code = "from acceptance_gates import Gates; from pathlib import Path; g=Gates(Path('gates.json')); g.blocked('browser','install'); g.finish()"
            import os
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent.resolve()))
            result = subprocess.run([sys.executable, '-c', code], cwd=directory, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Acceptance gates did not pass: browser', result.stderr)


if __name__ == '__main__':
    unittest.main()
