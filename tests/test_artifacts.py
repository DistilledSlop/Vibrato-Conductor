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
