import hashlib
import io
from pathlib import Path
import tempfile
import unittest

import artifact_transport as transport


class ArtifactTransportTests(unittest.TestCase):
    def frame(self, directory):
        source = Path(directory) / 'source.tar.gz'
        source.write_bytes(b'review artifact\x00bytes')
        stream = io.StringIO()
        transport.emit(source, '42', stream)
        return stream.getvalue()

    def test_exact_bytes_private_mode_and_frame_stripped_from_log(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = self.frame(directory)
            target = Path(directory) / 'receipt.tar.gz'
            log = transport.receive('before\n' + frame + 'after\n', '42', target)
            self.assertEqual(log, 'before\nafter\n')
            self.assertEqual(target.read_bytes(), b'review artifact\x00bytes')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_missing_truncated_duplicate_wrong_run_oversize_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = self.frame(directory)
            header = frame.splitlines()[0]
            corrupt = frame.replace(header.split()[-1], '0' * 64)
            cases = ['', frame.rsplit('SHIZUHA_REVIEW_END', 1)[0], frame + frame,
                     frame.replace('BEGIN 42', 'BEGIN 43'), corrupt,
                     frame.replace(header.split()[2], str(transport.LIMIT + 1), 1),
                     frame.replace('DATA ', 'DATA !', 1)]
            for index, value in enumerate(cases):
                target = Path(directory) / str(index)
                with self.subTest(index=index), self.assertRaises(ValueError):
                    transport.receive(value, '42', target)
                self.assertFalse(target.exists())

    def test_encoder_rejects_oversize_before_emitting(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'oversize'
            source.write_bytes(b'x' * (transport.LIMIT + 1))
            stream = io.StringIO()
            with self.assertRaises(ValueError):
                transport.emit(source, '42', stream)
            self.assertEqual(stream.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
