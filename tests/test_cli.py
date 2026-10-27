import tempfile
import unittest
from pathlib import Path

from conductor.__main__ import verify
from conductor.storage import artifact_manifest, atomic_json


class CliTests(unittest.TestCase):
    def test_manifest_verification_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'capture.bin').write_bytes(b'first')
            atomic_json(root / 'manifest.json', artifact_manifest(root, {'passed': False}))
            self.assertEqual(verify(root), {'passed': False})
            (root / 'capture.bin').write_bytes(b'other')
            with self.assertRaisesRegex(ValueError, 'verification failed'): verify(root)


if __name__ == '__main__': unittest.main()
