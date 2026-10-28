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

    def test_catalog_revision_cache_invalidates_tree_and_never_caches_staging(self):
        import json
        import conductor.storage as storage
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bank = root / 'bank'; entry = bank / 'test'; world = entry / 'world'
            world.mkdir(parents=True); (world / 'level.dat').write_bytes(b'level')
            (entry / 'world.json').write_text(json.dumps({'tags': ['first']}))
            cache = {}
            with patch('conductor.storage.world_revision', wraps=storage.world_revision) as revision:
                first = storage.inspect_world_bank(bank, cache)
                self.assertEqual(revision.call_count, 1)
                self.assertEqual(storage.inspect_world_bank(bank, cache)[0]['revision'], first[0]['revision'])
                self.assertEqual(revision.call_count, 1)  # no world payload hashes on repeat
                (entry / 'world.json').write_text(json.dumps({'tags': ['second']}))
                self.assertEqual(storage.inspect_world_bank(bank, cache)[0]['tags'], ['second'])
                self.assertEqual(revision.call_count, 1)
                (world / 'extra').write_bytes(b'a')
                changed = storage.inspect_world_bank(bank, cache)[0]['revision']
                self.assertNotEqual(changed, first[0]['revision']); self.assertEqual(revision.call_count, 2)
                (world / 'extra').write_bytes(b'b')
                self.assertNotEqual(storage.inspect_world_bank(bank, cache)[0]['revision'], changed)
                self.assertEqual(revision.call_count, 3)
                (world / 'extra').unlink()
                storage.inspect_world_bank(bank, cache); self.assertEqual(revision.call_count, 4)
                (world / 'link').symlink_to('/etc/passwd')
                self.assertFalse(storage.inspect_world_bank(bank, cache)[0]['supported'])
                (world / 'link').unlink()
                with patch('conductor.storage.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'unsupported')):
                    storage.stage_world(bank, 'test', root / 'staged', True)
                self.assertGreaterEqual(revision.call_count, 7)  # before/after/copy full hashes
                import shutil
                shutil.rmtree(entry)
                self.assertEqual(storage.inspect_world_bank(bank, cache), [])
                self.assertFalse(cache)

    def test_catalog_cache_tracks_new_worlds_and_fresh_metadata_restrictions(self):
        import json
        import conductor.storage as storage
        with tempfile.TemporaryDirectory() as tmp:
            bank = Path(tmp) / 'bank'; first = bank / 'first'; world = first / 'world'
            world.mkdir(parents=True); (world / 'level.dat').write_bytes(b'a')
            (first / 'world.json').write_text(json.dumps({'allowOperator': True, 'gamemodes': ['survival']}))
            cache = {}
            storage.inspect_world_bank(bank, cache)
            (first / 'world.json').write_text(json.dumps({'allowOperator': False, 'gamemodes': ['creative']}))
            second = bank / 'second' / 'world'; second.mkdir(parents=True); (second / 'level.dat').write_bytes(b'b')
            entries = {x['id']: x for x in storage.inspect_world_bank(bank, cache)}
            self.assertEqual(set(entries), {'first', 'second'})
            self.assertFalse(entries['first']['allowOperator'])
            self.assertEqual(entries['first']['gamemodes'], ['creative'])
            self.assertNotEqual(entries['first']['revision'], entries['second']['revision'])
            self.assertEqual(len(cache), 2)

    def test_cached_revision_rehashes_same_size_and_restored_mtime(self):
        import os
        import conductor.storage as storage
        with tempfile.TemporaryDirectory() as tmp:
            bank = Path(tmp) / 'bank'; world = bank / 'a' / 'world'; world.mkdir(parents=True)
            level = world / 'level.dat'; level.write_bytes(b'aaaa'); before = level.stat()
            cache = {}; first = storage.inspect_world_bank(bank, cache)[0]['revision']
            level.write_bytes(b'bbbb'); os.utime(level, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = storage.inspect_world_bank(bank, cache)[0]['revision']
            self.assertNotEqual(first, second)

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




class IntegrityTests(unittest.TestCase):
    def test_world_revision_covers_chunks_and_rejects_copy_mutations(self):
        from conductor.storage import world_revision
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'bank/test/world'
            source.mkdir(parents=True)
            (source / 'level.dat').write_bytes(b'level')
            (source / 'region').mkdir()
            (source / 'region/r.0.0.mca').write_bytes(b'chunk-a')
            original = world_revision(source)['revision']
            (source / 'region/r.0.0.mca').write_bytes(b'chunk-b')
            self.assertNotEqual(original, world_revision(source)['revision'])
            with patch('conductor.storage.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
                with self.assertRaises(ValueError): stage_world(root / 'bank', 'test', root / 'dest')

    def test_manifest_rejects_traversal_extra_files_and_symlinks(self):
        from conductor.storage import verify_manifest
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data.bin').write_bytes(b'valid')
            atomic_json(root / 'manifest.json', artifact_manifest(root, {'ok': True}))
            self.assertEqual(verify_manifest(root), {'ok': True})
            (root / 'extra').write_bytes(b'unlisted')
            with self.assertRaisesRegex(ValueError, 'Unlisted'): verify_manifest(root)
            (root / 'extra').unlink()
            (root / 'link').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError, 'regular files'): verify_manifest(root)
            (root / 'link').unlink()
            atomic_json(root / 'manifest.json', {'schemaVersion': 1, 'result': {}, 'files': [{'path': '../outside', 'bytes': 0, 'sha256': ''}]})
            with self.assertRaisesRegex(ValueError, 'Unsafe'): verify_manifest(root)



class WorldCurriculumTests(unittest.TestCase):
    def test_stage_preserves_source_seed_and_rejects_stale_sidecar_before_copy(self):
        from conductor.storage import world_revision
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'bank/real/world'
            source.mkdir(parents=True)
            (source / 'level.dat').write_bytes(b'pregenerated')
            metadata = {'seed': '-9211111111111111111', 'sourceGameType': 1,
                        'minecraftVersion': '1.21.1', 'revision': world_revision(source)['revision']}
            atomic_json(source.parent / 'world.json', metadata)
            with patch('conductor.storage.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'unsupported')):
                result = stage_world(root / 'bank', 'real', root / 'dest', True)
            self.assertEqual(result['sourceSeed'], metadata['seed'])
            self.assertEqual(result['sourceGameType'], 1)
            self.assertEqual(result['minecraftVersion'], '1.21.1')
            self.assertEqual(len(result['sourceMetadataSha256']), 64)
            (source / 'level.dat').write_bytes(b'modified')
            with patch('conductor.storage.subprocess.run') as copy:
                with self.assertRaisesRegex(ValueError, 'World revision mismatch'):
                    stage_world(root / 'bank', 'real', root / 'stale', True)
                copy.assert_not_called()
            self.assertFalse((root / 'stale').exists())

    def test_bank_mode_and_operator_restrictions_are_explicit_and_strict(self):
        from conductor.storage import inspect_world_bank
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            world = root / 'bank/clean/world'
            world.mkdir(parents=True)
            (world / 'level.dat').write_bytes(b'clean-template')
            atomic_json(world.parent / 'world.json', {'gamemodes':['survival','adventure'],'allowOperator':False,'tags':['safe']})
            metadata = inspect_world_bank(root / 'bank')[0]
            self.assertEqual(metadata['gamemodes'],['survival','adventure'])
            self.assertFalse(metadata['allowOperator'])
            self.assertEqual(metadata['tags'],['safe'])
            atomic_json(world.parent / 'world.json', {'gamemodes':'creative','allowOperator':'false'})
            invalid = inspect_world_bank(root / 'bank')[0]
            self.assertFalse(invalid['supported'])
            self.assertIn('gamemodes',invalid['error'])

if __name__ == '__main__': unittest.main()
