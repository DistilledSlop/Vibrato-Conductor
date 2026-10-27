import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.artifacts import ArtifactStore, quality_report
from conductor.storage import artifact_manifest, atomic_json


def finalized(root, run_id='run-one', passed=True, mode='diagnostic', success=None):
    run = root / run_id
    (run / 'captures').mkdir(parents=True)
    (run / 'captures/session.bin').write_bytes(b'\x00\x00')
    rows = [{'off': 0, 'len': 1, 'dir': 'C2S', 'state': 'play'}, {'off': 1, 'len': 1, 'dir': 'S2C', 'state': 'play'}]
    (run / 'captures/session.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    atomic_json(run / 'captures/session.status.json', {'complete': True, 'bytes': 2})
    result = {'runId': run_id, 'diagnosticPassed': passed, 'taskSuccess': success,
              'datasetEligible': passed and mode == 'collection' and success is True, 'capture': {'complete': True}, 'serverHealth': {'valid': True, 'samples': 12, 'meanTps': 20}}
    atomic_json(run / 'metadata.json', {'runId': run_id, 'assignment': '@get oak_log 1', 'mode': mode,
                'versions': {'minecraft': '1.21.1'}, 'build': {'source': 'test'}, 'world': {'seed': 1}})
    atomic_json(run / 'result.json', result)
    atomic_json(run / 'manifest.json', artifact_manifest(run, result))
    return run


class ArtifactTests(unittest.TestCase):
    def test_retry_survives_restart_and_publishes_only_verified_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = finalized(root / 'local')
            store = ArtifactStore(root / 'local', root / 'central', root / 'state', retry_seconds=0)
            store.enqueue(run.name)
            with patch.object(store, '_copy', side_effect=OSError('storage unavailable')):
                attempt = store.deliver_pending(limit=1)
            self.assertEqual(attempt[0]['state'], 'retry')
            self.assertFalse((root / 'central' / run.name).exists())
            self.assertEqual(list((root / 'central/.incoming').iterdir()), [])
            reopened = ArtifactStore(root / 'local', root / 'central', root / 'state', retry_seconds=0)
            self.assertEqual(reopened.deliver_pending()[0]['state'], 'delivered')
            self.assertEqual(reopened.snapshot()['delivered'], 1)
            self.assertEqual(reopened.snapshot()['eligible'], 0)
            self.assertEqual(reopened.enqueue(run.name)['state'], 'delivered')
            self.assertEqual(reopened.deliver_pending(), [])

    def test_copy_corruption_never_publishes_or_marks_delivered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = finalized(root / 'local')
            store = ArtifactStore(root / 'local', root / 'central', root / 'state', retry_seconds=0)
            store.enqueue(run.name)
            original = store._copy
            def bad_copy(source, dest):
                original(source, dest)
                (dest / 'metadata.json').write_text('{}')
            with patch.object(store, '_copy', side_effect=bad_copy):
                result = store.deliver_pending(limit=1)
            self.assertNotEqual(result[0]['state'], 'delivered')
            self.assertFalse((root / 'central' / run.name).exists())
            self.assertEqual(store.snapshot()['delivered'], 0)
            self.assertEqual(result[0]['state'], 'retry')
            self.assertEqual(store.deliver_pending(limit=1)[0]['state'], 'delivered')

    def test_retention_protects_active_and_undelivered_runs_and_failed_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, passed in [('active', True), ('pending', True), ('good', True), ('bad', False)]:
                finalized(root / 'local', name, passed)
            store = ArtifactStore(root / 'local', root / 'central', root / 'state')
            for name in ['active', 'good', 'bad']: store.enqueue(name)
            store.deliver_pending()
            future = time.time() + 10 * 86400
            self.assertEqual(store.prune(['active'], now=future), ['good'])
            self.assertTrue((root / 'local/good').exists())
            store.prune(['active'], now=future, dry_run=False)
            self.assertFalse((root / 'local/good').exists())
            for name in ['active', 'pending', 'bad']: self.assertTrue((root / 'local' / name).exists())
            self.assertTrue((root / 'central/good/manifest.json').exists())

    def test_diagnostic_or_unconfirmed_success_cannot_enter_dataset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            diagnostic = finalized(root, 'diagnostic', mode='diagnostic', success=True)
            self.assertIn('diagnostic_mode', quality_report(diagnostic)['reasons'])
            unknown = finalized(root, 'unknown', mode='collection', success=None)
            self.assertIn('task_success_unconfirmed', quality_report(unknown)['reasons'])

    def test_collection_gate_requires_healthy_server_and_real_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = finalized(root, mode='collection', success=True)
            self.assertTrue(quality_report(run)['eligible'])
            (run / 'captures/session.jsonl').write_text('')
            result = json.loads((run / 'result.json').read_text())
            atomic_json(run / 'manifest.json', artifact_manifest(run, result))
            self.assertIn('capture_incomplete', quality_report(run)['reasons'])

    def test_queue_backpressure_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = finalized(root / 'local')
            store = ArtifactStore(root / 'local', root / 'central', root / 'state', max_buffer_bytes=1)
            store.enqueue(run.name)
            self.assertTrue(store.snapshot()['backpressure'])


if __name__ == '__main__': unittest.main()


class ArtifactPolicyTests(unittest.TestCase):
    def test_live_unfinalized_spool_counts_towards_backpressure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = root / 'local/live/captures'
            live.mkdir(parents=True)
            (live / 'open.bin').write_bytes(b'x' * 1024)
            store = ArtifactStore(root / 'local', root / 'central', root / 'state', max_buffer_bytes=512)
            value = store.snapshot()
            self.assertEqual(value['pendingBytes'], 0)
            self.assertEqual(value['liveSpoolBytes'], 1024)
            self.assertTrue(value['backpressure'])
            with patch.object(Path, 'rglob', side_effect=AssertionError('usage should be cached')):
                self.assertEqual(store.snapshot()['liveSpoolBytes'], 1024)

    def test_central_retention_preserves_training_active_pending_failures_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ArtifactStore(root / 'local', root / 'central', root / 'state')
            for name, passed, mode, success in [('diagnostic', True, 'diagnostic', None),
                                              ('active', True, 'diagnostic', None),
                                              ('training', True, 'collection', True),
                                              ('failure', False, 'diagnostic', False)]:
                finalized(root / 'local', name, passed, mode, success)
                store.enqueue(name)
            finalized(root / 'local', 'pending')
            store.deliver_pending(limit=10)
            future = time.time() + 40 * 86400
            self.assertEqual(store.prune_central(['active'], now=future), ['diagnostic'])
            store.prune_central(['active'], now=future, dry_run=False)
            self.assertFalse((root / 'central/diagnostic').exists())
            self.assertFalse((root / 'local/diagnostic').exists())
            for name in ['active', 'training', 'failure']: self.assertTrue((root / 'central' / name).exists())
            self.assertTrue((root / 'local/pending').exists())
            (root / 'central/failure/result.json').write_text('{}')
            with self.assertRaises(ValueError): store.prune_central(['active'], now=time.time()+100*86400, dry_run=False)
            self.assertTrue((root / 'central/failure').exists())
            self.assertEqual(store.snapshot()['pendingBytes'], 0)

    def test_central_quota_keeps_copy_unpublished_and_applies_backpressure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = finalized(root / 'local')
            store = ArtifactStore(root / 'local', root / 'central', root / 'state', max_central_bytes=1, retry_seconds=0)
            store.enqueue(run.name)
            value = store.deliver_pending(limit=1)
            self.assertEqual(value[0]['state'], 'retry')
            self.assertFalse((root / 'central' / run.name).exists())
            self.assertTrue(store.snapshot()['backpressure'])


class OrphanTests(unittest.TestCase):
    def test_terminal_kill_evidence_is_preserved_delivered_and_never_promoted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ArtifactStore(root / 'local', root / 'central', root / 'state')
            orphan = root / 'local/killed-run'
            (orphan / 'captures').mkdir(parents=True)
            (orphan / 'captures/raw.bin').write_bytes(b'partial-packet')
            (orphan / 'captures/raw.jsonl').write_text('{partial')
            (orphan / 'captures/raw.status.json').write_text('{broken')
            (orphan / 'metadata.json').write_text('{interrupted')
            with self.assertRaisesRegex(ValueError, 'terminal'): store.quarantine_orphan('killed-run', 'runtime_killed')
            self.assertFalse((orphan / 'manifest.json').exists())
            result = store.quarantine_orphan('killed-run', 'runtime_killed', terminal=True)
            self.assertFalse(result['datasetEligible'])
            self.assertEqual((orphan / 'metadata.interrupted.json').read_text(), '{interrupted')
            self.assertEqual((orphan / 'captures/raw.bin').read_bytes(), b'partial-packet')
            store.enqueue('killed-run')
            self.assertEqual(store.deliver_pending()[0]['state'], 'delivered')
            self.assertEqual(store.snapshot()['eligible'], 0)


class WorkspaceRetentionTests(unittest.TestCase):
    def test_only_old_terminal_delivered_worlds_are_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ArtifactStore(root / 'local', root / 'central', root / 'state')
            workers = []
            for run_id, passed, state in [('good', True, 'completed'), ('bad', False, 'failed'),
                                          ('active', True, 'running'), ('pending', True, 'pending')]:
                finalized(root / 'local', run_id, passed)
                if run_id != 'pending': store.enqueue(run_id)
                workspace = root / 'workspaces' / run_id
                workspace.mkdir(parents=True)
                (workspace / 'world.mca').write_bytes(b'world')
                workers.append({'runId': run_id, 'state': state, 'updatedWall': time.time()})
            store.deliver_pending(limit=10)
            future = time.time() + 2 * 86400
            self.assertEqual(store.prune_workspaces(root / 'workspaces', workers, now=future), ['good'])
            self.assertTrue((root / 'workspaces/good').exists())
            store.prune_workspaces(root / 'workspaces', workers, now=future, dry_run=False)
            self.assertFalse((root / 'workspaces/good').exists())
            for run_id in ['bad', 'active', 'pending']: self.assertTrue((root / 'workspaces' / run_id).exists())
            self.assertTrue((root / 'central/good/manifest.json').exists())

    def test_workspace_symlink_and_corrupt_central_copy_are_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ArtifactStore(root / 'local', root / 'central', root / 'state')
            finalized(root / 'local', 'test')
            store.enqueue('test'); store.deliver_pending()
            workspaces = root / 'workspaces'
            workspaces.mkdir()
            outside = root / 'precious'
            outside.mkdir(); (outside / 'file').write_text('preserve')
            (workspaces / 'test').symlink_to(outside, target_is_directory=True)
            worker = {'runId': 'test', 'state': 'completed', 'updatedWall': time.time()}
            future = time.time() + 2 * 86400
            self.assertEqual(store.prune_workspaces(workspaces, [worker], now=future, dry_run=False), [])
            self.assertTrue((outside / 'file').exists())
            (workspaces / 'test').unlink(); (workspaces / 'test').mkdir()
            (root / 'central/test/metadata.json').write_text('{}')
            with self.assertRaises(ValueError): store.prune_workspaces(workspaces, [worker], now=future, dry_run=False)
            self.assertTrue((workspaces / 'test').exists())


class DeliveryPerformanceTests(unittest.TestCase):
    def test_duplicate_enqueue_does_not_rehash_old_capture_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = finalized(root / 'local')
            store = ArtifactStore(root / 'local', root / 'central', root / 'state')
            job = store.enqueue(run.name)
            with patch('conductor.artifacts.verify_manifest', side_effect=AssertionError('Payloads should not be rehashed')):
                self.assertEqual(store.enqueue(run.name)['manifest_sha'], job['manifest_sha'])
