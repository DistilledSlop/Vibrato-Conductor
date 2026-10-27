import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.storage import artifact_manifest, atomic_json, stage_world


class StorageTests(unittest.TestCase):
    def test_world_copy_requires_explicit_fallback_and_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'bank/forest/world'
            source.mkdir(parents=True)
            (source / 'level.dat').write_bytes(b'original')
            source.chmod(0o555)
            (source / 'level.dat').chmod(0o444)
            dest = root / 'worker-world'
            with patch('conductor.storage.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'unsupported')):
                with self.assertRaisesRegex(RuntimeError, 'ALLOW_WORLD_COPY'): stage_world(root / 'bank', 'forest', dest)
                result = stage_world(root / 'bank', 'forest', dest, True)
            self.assertEqual(result['mode'], 'full-copy')
            (dest / 'level.dat').write_bytes(b'modified')
            self.assertEqual((source / 'level.dat').read_bytes(), b'original')
            self.assertTrue(dest.stat().st_mode & 0o200)
            source.chmod(0o755)
            (source / 'link').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError): stage_world(root / 'bank', 'forest', root / 'other', True)

    def test_manifest_detects_content_changes_and_excludes_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'capture.bin').write_bytes(b'first')
            atomic_json(root / 'manifest.json', {})
            first = artifact_manifest(root, {'passed': False})
            (root / 'capture.bin').write_bytes(b'other')
            second = artifact_manifest(root, {})
            atomic_json(root / 'manifest.json', first)
            self.assertEqual(len(first['files']), 1)
            self.assertNotEqual(first['files'][0]['sha256'], second['files'][0]['sha256'])


if __name__ == '__main__': unittest.main()
