"""Independent checks of compact training data and its admission boundary."""
import fcntl
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from conductor.artifacts import ArtifactStore, QUALITY_POLICY_VERSION, quality_report
from conductor.compact import build_compact, compact_quality
from conductor.coverage import CoverageInventory
from conductor.lineage import source_admitted
from conductor.storage import artifact_manifest, atomic_json, digest, verify_manifest
from conductor.training import export_index
from conductor.web import Service


def make_source(parent, name='run', seed=1, lineage=None):
    run = parent / name
    captures = run / 'captures'
    captures.mkdir(parents=True)
    (captures / 'session.bin').write_bytes(b'\x00\x00')
    (captures / 'session.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in [
        {'off': 0, 'len': 1, 'dir': 'C2S', 'state': 'play'},
        {'off': 1, 'len': 1, 'dir': 'S2C', 'state': 'play'}]))
    atomic_json(captures / 'session.status.json', {'complete': True, 'bytes': 2})
    health = {'valid': True, 'samples': 12, 'meanTps': 20, 'degradedWindows': 0}
    result = {'runId': name, 'diagnosticPassed': True, 'taskSuccess': True, 'datasetEligible': True,
              'capture': {'complete': True}, 'serverHealth': health, 'clientHealth': health}
    world = {'id': 'world', 'revision': 'revision', 'sourceSeed': str(seed)}
    if lineage is not None: world['earnedContext'] = lineage
    atomic_json(run / 'metadata.json', {'runId': name, 'assignment': '@get oak_log 1', 'mode': 'collection',
                'versions': {'minecraft': '1.21.1'}, 'build': {'source': 'fixture'}, 'world': world})
    atomic_json(run / 'result.json', result)
    heartbeat = {'actualMode': 'survival', 'permissionSource': 'server_observer', 'permissionLevel': 0,
                 'abilities': {'allowFlying': False}, 'observationSemantics': {'items': 'inventory_present'},
                 'observations': {'items': ['minecraft:oak_log']}, 'irrelevant': 'discard me'}
    (run / 'heartbeats.jsonl').write_text((json.dumps(heartbeat) + '\n') * 3)
    (run / 'logs').mkdir()
    (run / 'logs/client.log').write_text('healthy client\n')
    (run / 'screenshot.png').write_bytes(b'large image')
    (run / 'world-snapshot.tar.gz').write_bytes(b'world')
    atomic_json(run / 'manifest.json', artifact_manifest(run, result))
    return run


def compact_to_central(source, central):
    central.mkdir(parents=True, exist_ok=True)
    admission = central / '.admission'
    admission.mkdir(exist_ok=True)
    run = central / source.name
    proof = build_compact(source, run, admission)
    receipt = {'runId': source.name, 'manifestSha256': proof['compactManifestSha256'],
               'sourceManifestSha256': proof['sourceManifestSha256'], 'evidenceSha256': proof['evidenceSha256'],
               'compactSchemaVersion': 1, 'quality': proof['quality'], 'originalQuality': proof['quality']}
    atomic_json(admission / (source.name + '.json'), receipt)
    return run, admission


class CompactRetentionTests(unittest.TestCase):
    def test_success_is_verifiable_exportable_and_omits_diagnostic_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root / 'full')
            source_hash = digest(source / 'manifest.json')
            run, admission = compact_to_central(source, root / 'central')
            self.assertEqual(verify_manifest(run)['runId'], 'run')
            self.assertTrue(compact_quality(run, admission)['eligible'])
            self.assertEqual(digest(run / 'source-manifest.json'), source_hash)
            self.assertFalse((run / 'logs').exists())
            self.assertFalse((run / 'screenshot.png').exists())
            self.assertFalse((run / 'world-snapshot.tar.gz').exists())
            self.assertTrue((run / 'captures/session.bin').is_file())
            self.assertTrue((run / 'captures/session.jsonl').is_file())
            self.assertTrue((run / 'captures/session.status.json').is_file())
            index = export_index(root / 'central', root / 'index.json')
            self.assertEqual([r['runId'] for r in index['accepted']], ['run'])
            self.assertTrue(quality_report(source)['eligible'])
            self.assertEqual(digest(source / 'manifest.json'), source_hash)

    def test_missing_external_receipt_and_tampering_are_rejected(self):
        for relative in ('captures/session.bin', 'captures/session.jsonl', 'captures/session.status.json',
                         'validation-evidence.json', 'source-manifest.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = make_source(root / 'full')
                run, admission = compact_to_central(source, root / 'central')
                with self.assertRaises((ValueError, OSError)):
                    compact_quality(run, None)
                (run / relative).write_bytes(b'tampered')
                with self.assertRaises((ValueError, OSError)):
                    compact_quality(run, admission)
                self.assertEqual(export_index(root / 'central', root / 'index.json')['accepted'], [])

    def test_receipt_revocation_and_policy_changes_fail_closed(self):
        for change in ('revoked', 'stale', 'source_hash', 'evidence_hash'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = make_source(root / 'full')
                run, admission = compact_to_central(source, root / 'central')
                receipt = admission / 'run.json'; data = json.loads(receipt.read_text())
                if change == 'revoked': data['quality']['eligible'] = False
                elif change == 'stale': data['quality']['policyVersion'] = QUALITY_POLICY_VERSION + 1
                elif change == 'source_hash': data['sourceManifestSha256'] = '0' * 64
                else: data['evidenceSha256'] = '0' * 64
                atomic_json(receipt, data)
                self.assertEqual(export_index(root / 'central', root / 'index.json')['accepted'], [])

    def test_lineage_revocation_is_checked_after_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); central = root / 'central'; admission = central / '.admission'
            admission.mkdir(parents=True)
            parent_hash = 'a' * 64
            receipt = admission / 'source.json'
            atomic_json(receipt, {'runId': 'source', 'manifestSha256': parent_hash,
                                  'quality': {'policyVersion': QUALITY_POLICY_VERSION, 'eligible': True}})
            source = make_source(root / 'full', lineage={'sourceRun': 'source', 'sourceManifestSha256': parent_hash})
            run, admission = compact_to_central(source, central)
            self.assertTrue(compact_quality(run, admission)['eligible'])
            data = json.loads(receipt.read_text()); data['quality']['eligible'] = False; atomic_json(receipt, data)
            self.assertFalse(compact_quality(run, admission)['eligible'])
            self.assertEqual(export_index(central, root / 'index.json')['accepted'], [])

    def test_controller_and_history_migration_share_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            class Fleet:
                config = SimpleNamespace(artifacts_dir=root / 'spool')
                def __init__(self): self.reconciles = 0
                def snapshot(self): return {'workers': []}
                def reconcile(self): self.reconciles += 1; return self.snapshot()
                def set_inhibited(self, value): pass
            fleet = Fleet(); service = Service(fleet, artifacts=store)
            with (store.state_dir / 'history.lock').open('a') as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                service.tick()
                self.assertEqual(fleet.reconciles, 0)
                operations = [lambda: store.migrate_history([], root / 'workspace', apply=True),
                              lambda: store.deliver_pending(), lambda: store.enqueue('run'),
                              lambda: store.reaudit('run'), lambda: store.prune(dry_run=False),
                              lambda: store.prune_central(dry_run=False),
                              lambda: store.prune_compact(root / 'workspace', [])]
                for operation in operations:
                    with self.assertRaisesRegex(ValueError, 'busy'):
                        operation()
            service.tick()
            self.assertEqual(fleet.reconciles, 1)
            self.assertEqual(store.migrate_history([], root / 'workspace', apply=True)['migrated'], [])

    def test_managed_import_selection_requires_coverage_delivery_and_fresh_inactive_runtime(self):
        from conductor.history import migration_workers
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = root / 'control'; state.mkdir()
            with sqlite3.connect(state / 'fleet.sqlite3') as db:
                db.execute('CREATE TABLE workers(run_id TEXT, state TEXT, ingested INTEGER)')
            store = ArtifactStore(root / 'spool', root / 'central', root / 'delivery')
            inventory = CoverageInventory(state / 'coverage.sqlite3')
            names = ('eligible', 'missing', 'mismatch', 'active', 'badreceipt')
            for name in names:
                make_source(root / 'spool', name); store.enqueue(name)
            store.deliver_pending(limit=10)
            for name in ('eligible', 'mismatch', 'active', 'badreceipt'):
                inventory.ingest(root / 'central' / name)
            with inventory._db() as db:
                db.execute("UPDATE runs SET manifest_sha=? WHERE run_id='mismatch'", ('0' * 64,))
            bad_receipt = root / 'central/.admission/badreceipt.json'
            data = json.loads(bad_receipt.read_text()); data['quality']['eligible'] = False
            atomic_json(bad_receipt, data)
            # A directory with no delivery row must never become a migration candidate.
            make_source(root / 'central', 'arbitrary')
            config = SimpleNamespace(state_dir=state, backend='docker')
            backend = SimpleNamespace(list_workers=lambda: [{'runId': 'active', 'state': 'running'}])
            old_hash = digest(root / 'central/eligible/manifest.json')
            choose = lambda: migration_workers(config, store, include_imports=True, backend=backend)
            selected = {w['runId'] for w in choose()}
            self.assertEqual(selected, {'eligible', 'badreceipt'})
            result = store.migrate_history(choose, root / 'workspace', apply=True)
            self.assertEqual(result['migrated'], ['eligible'])
            self.assertIn('badreceipt', {row['runId'] for row in result['blocked']})
            self.assertTrue(source_admitted(root / 'central/.admission', 'eligible', old_hash))
            self.assertEqual(len(export_index(root / 'central', root / 'index.json', ['eligible'])['accepted']), 1)
            for name in ('missing', 'mismatch', 'active', 'badreceipt', 'arbitrary'):
                self.assertTrue((root / 'central' / name / 'screenshot.png').exists())
            unknown_backend = SimpleNamespace(list_workers=lambda: [{'runId': None, 'state': 'running'}])
            with self.assertRaises(ValueError):
                migration_workers(config, store, include_imports=True, backend=unknown_backend)
            self.assertEqual(migration_workers(config, store, include_imports=False, backend=backend), [])

    def test_historical_migration_preserves_admission_and_skips_active_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            for name in ('good', 'failed', 'active', 'unknown'):
                run = make_source(root / 'spool', name)
                if name == 'failed':
                    result = json.loads((run / 'result.json').read_text())
                    result.update(diagnosticPassed=False, datasetEligible=False, taskSuccess=False)
                    atomic_json(run / 'result.json', result); atomic_json(run / 'manifest.json', artifact_manifest(run, result))
                store.enqueue(name)
            store.deliver_pending()
            old_hash = digest(root / 'central/good/manifest.json')
            workers = [{'runId': name, 'state': state, 'coverageIngested': True}
                       for name, state in [('good', 'completed'), ('failed', 'failed'), ('active', 'running')]]
            plan = store.migrate_history(workers, root / 'workspace')
            self.assertEqual({row['runId'] for row in plan['candidates']}, {'good', 'failed'})
            self.assertEqual({row['runId'] for row in plan['blocked']}, {'active', 'unknown'})
            self.assertEqual(digest(root / 'central/good/manifest.json'), old_hash)
            result = store.migrate_history(workers, root / 'workspace', apply=True)
            self.assertEqual(set(result['migrated']), {'good', 'failed'})
            self.assertTrue(source_admitted(root / 'central/.admission', 'good', old_hash))
            self.assertEqual(len(export_index(root / 'central', root / 'index.json', ['good'])['accepted']), 1)
            self.assertEqual(export_index(root / 'central', root / 'index.json', ['failed'])['accepted'], [])
            self.assertFalse((root / 'central/failed/captures').exists())
            self.assertTrue((root / 'central/active/screenshot.png').exists())
            self.assertTrue((root / 'central/unknown/screenshot.png').exists())

    def test_historical_earned_source_preserves_descendant_and_coverage_until_revoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            parent = make_source(root / 'spool', 'parent'); parent_hash = digest(parent / 'manifest.json')
            store.enqueue('parent'); store.deliver_pending()
            child = make_source(root / 'spool', 'child', lineage={'sourceRun': 'parent', 'sourceManifestSha256': parent_hash})
            store.enqueue('child'); store.deliver_pending()
            inventory = CoverageInventory(root / 'coverage.sqlite', central_dir=root / 'central')
            inventory.ingest(root / 'central/parent')
            with inventory._db() as db:
                before = list(map(tuple, db.execute('SELECT target_id,count,evidence FROM observations ORDER BY target_id')))
            self.assertEqual(len(export_index(root / 'central', root / 'index.json', ['child'])['accepted']), 1)
            worker = [{'runId': 'parent', 'state': 'completed', 'coverageIngested': True}]
            self.assertEqual(store.migrate_history(worker, root / 'workspace', apply=True)['migrated'], ['parent'])
            inventory.ingest(root / 'central/parent')
            with inventory._db() as db:
                after = list(map(tuple, db.execute('SELECT target_id,count,evidence FROM observations ORDER BY target_id')))
            self.assertEqual(before, after)
            self.assertEqual(len(export_index(root / 'central', root / 'index.json', ['child'])['accepted']), 1)
            receipt = root / 'central/.admission/parent.json'; data = json.loads(receipt.read_text())
            data['quality']['eligible'] = False; data['quality']['reasons'] = ['revoked']; atomic_json(receipt, data)
            self.assertEqual(export_index(root / 'central', root / 'index.json', ['child'])['accepted'], [])

    def test_history_recovery_never_overwrites_intervening_revocation(self):
        for phase in ('target', 'receipt'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); make_source(root / 'spool')
                store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
                store.enqueue('run'); store.deliver_pending()
                receipt = root / 'central/.admission/run.json'
                prior = json.loads(receipt.read_text())
                workers = [{'runId': 'run', 'state': 'completed', 'coverageIngested': True}]
                replace, old_atomic = Path.replace, atomic_json
                def interrupt_target(path, target):
                    result = replace(path, target)
                    if phase == 'target' and Path(target) == root / 'central/run':
                        raise KeyboardInterrupt('after target')
                    return result
                def interrupt_receipt(path, value):
                    result = old_atomic(path, value)
                    if phase == 'receipt' and Path(path) == receipt:
                        raise KeyboardInterrupt('after receipt')
                    return result
                with patch.object(Path, 'replace', interrupt_target), \
                     patch('conductor.artifacts.atomic_json', side_effect=interrupt_receipt):
                    with self.assertRaises(KeyboardInterrupt):
                        store.migrate_history(workers, root / 'workspace', apply=True)
                current = json.loads(receipt.read_text()) if receipt.exists() else prior
                current['quality']['eligible'] = False
                current['quality']['reasons'] = ['intervening_revocation']
                atomic_json(receipt, current); preserved = receipt.read_bytes()
                reopened = ArtifactStore(root / 'spool', root / 'central', root / 'state')
                outcome = reopened.migrate_history(workers, root / 'workspace', apply=True)
                self.assertEqual(outcome['migrated'], [])
                self.assertTrue(outcome['blocked'])
                self.assertEqual(receipt.read_bytes(), preserved)
                self.assertTrue((root / 'central/.history-backup/run').is_dir())
                self.assertTrue((root / 'state/history-migrations/run.json').is_file())

    def test_converted_coverage_remains_effective_for_snapshot_and_scheduling(self):
        for change in ('none', 'revoked', 'corrupt'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
                make_source(root / 'spool', 'run'); store.enqueue('run'); store.deliver_pending()
                inventory = CoverageInventory(root / 'coverage.sqlite', central_dir=root / 'central', enabled_tasks=['get-oak_log'])
                inventory.ingest(root / 'central/run'); before = inventory.snapshot()
                self.assertEqual(before['eligibleRuns'], 1)
                workers = [{'runId': 'run', 'state': 'completed', 'coverageIngested': True}]
                self.assertEqual(store.migrate_history(workers, root / 'workspace', apply=True)['migrated'], ['run'])
                if change == 'revoked':
                    receipt = root / 'central/.admission/run.json'; data = json.loads(receipt.read_text())
                    data['quality']['eligible'] = False; data['quality']['reasons'] = ['revoked']; atomic_json(receipt, data)
                if change == 'corrupt': (root / 'central/run/captures/session.bin').write_bytes(b'xx')
                after = inventory.snapshot()
                self.assertEqual(after['eligibleRuns'], 1 if change == 'none' else 0)
                if change == 'none':
                    self.assertEqual(before['categories'], after['categories'])
                    self.assertEqual(before['modelPacketCoverage']['observed'], after['modelPacketCoverage']['observed'])
                counts = []
                effective = inventory._effective_runs
                def observe(db):
                    value = effective(db); counts.append(value); return value
                with patch.object(inventory, '_effective_runs', side_effect=observe):
                    inventory.choose('new-assignment')
                self.assertEqual(counts, [1 if change == 'none' else 0])

    def test_historical_migration_recovers_at_transaction_boundaries(self):
        for phase in ('backup', 'target', 'receipt', 'database', 'cleanup'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = make_source(root / 'spool')
                store = ArtifactStore(root / 'spool', root / 'central', root / 'state', retry_seconds=0)
                store.enqueue('run'); store.deliver_pending()
                original_hash = digest(source / 'manifest.json')
                workers = [{'runId': 'run', 'state': 'completed', 'coverageIngested': True}]
                rename, replace = Path.rename, Path.replace
                import shutil
                remove = shutil.rmtree
                old_atomic = atomic_json
                def interrupt_rename(path, target):
                    result = rename(path, target)
                    if phase == 'backup' and Path(target) == root / 'central/.history-backup/run':
                        raise KeyboardInterrupt('after backup rename')
                    return result
                def interrupt_replace(path, target):
                    result = replace(path, target)
                    if phase == 'target' and Path(target) == root / 'central/run':
                        raise KeyboardInterrupt('after target publication')
                    return result
                def interrupt_receipt(path, value):
                    result = old_atomic(path, value)
                    if phase == 'receipt' and Path(path) == root / 'central/.admission/run.json':
                        raise KeyboardInterrupt('after receipt')
                    return result
                def interrupt_cleanup(path, *args, **kwargs):
                    result = remove(path, *args, **kwargs)
                    if phase == 'cleanup' and Path(path) == root / 'central/.history-backup/run':
                        raise KeyboardInterrupt('after backup deletion')
                    return result
                with patch.object(Path, 'rename', interrupt_rename), patch.object(Path, 'replace', interrupt_replace), \
                     patch('conductor.artifacts.atomic_json', side_effect=interrupt_receipt), \
                     patch('conductor.artifacts.shutil.rmtree', side_effect=interrupt_cleanup), \
                     patch.object(store, 'prune_compact', side_effect=KeyboardInterrupt('after database') if phase == 'database' else store.prune_compact):
                    with self.assertRaises(KeyboardInterrupt):
                        store.migrate_history(workers, root / 'workspace', apply=True)
                reopened = ArtifactStore(root / 'spool', root / 'central', root / 'state', retry_seconds=0)
                result = reopened.migrate_history(workers, root / 'workspace', apply=True)
                self.assertEqual(result['blocked'], [])
                self.assertIn('run', result['migrated'])
                self.assertTrue(source_admitted(root / 'central/.admission', 'run', original_hash))
                self.assertEqual(len(export_index(root / 'central', root / 'index.json')['accepted']), 1)
                self.assertEqual(reopened.migrate_history(workers, root / 'workspace', apply=True)['migrated'], [])

    def test_cutoff_is_persisted_across_controller_restart(self):
        from conductor.web import serve
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); config = root / 'config.json'
            atomic_json(config, {'state_dir': str(root / 'state'), 'artifacts_dir': str(root / 'spool'),
                                 'workspace_dir': str(root / 'workspace'), 'central_dir': str(root / 'central'),
                                 'accept_eula': True, 'accept_offline': True})
            cutoffs = []
            class FakeService:
                def __init__(self, fleet, **kwargs): cutoffs.append(kwargs['compact_after_wall'])
                def start(self): pass
                def close(self): pass
            class FakeServer:
                server_port = 1234
                def __init__(self, *args): pass
                def serve_forever(self, **kwargs): pass
                def server_close(self): pass
                def shutdown(self): pass
            with patch('conductor.fleet.Fleet'), patch('conductor.web.scheduler_from_config'), \
                 patch('conductor.web.Service', FakeService), patch('conductor.web.Server', FakeServer), \
                 patch('conductor.web.signal.signal'), patch('builtins.print'):
                serve(config); serve(config)
            saved = json.loads((root / 'state/compact-retention.json').read_text())['afterWall']
            self.assertEqual(cutoffs, [saved, saved])

    def test_service_cutoff_keeps_earlier_and_unknown_runs_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('new', 'old', 'unknown'):
                make_source(root / 'spool', name)
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            workers = [{'runId': 'new', 'state': 'completed', 'createdWall': 101},
                       {'runId': 'old', 'state': 'completed', 'createdWall': 99}]
            class Fleet:
                config = SimpleNamespace(artifacts_dir=root / 'spool')
                def snapshot(self): return {'workers': workers}
                def set_inhibited(self, value): pass
            Service(Fleet(), artifacts=store, compact_after_wall=100)._deliver()
            rows = {row['run_id']: row for row in store.snapshot()['runs']}
            self.assertEqual(rows['new']['compact_requested'], 1)
            self.assertEqual(rows['old']['compact_requested'], 0)
            self.assertEqual(rows['unknown']['compact_requested'], 0)
            self.assertTrue((root / 'central/old/screenshot.png').exists())
            self.assertTrue((root / 'central/unknown/screenshot.png').exists())
            self.assertFalse((root / 'central/new/screenshot.png').exists())

    def test_capacity_is_based_on_compact_size_not_source_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = make_source(root / 'spool')
            (source / 'screenshot.png').write_bytes(b'x' * 1000000)
            atomic_json(source / 'manifest.json', artifact_manifest(source, json.loads((source / 'result.json').read_text())))
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state', max_central_bytes=200000)
            store.enqueue('run', compact=True)
            self.assertEqual(store.deliver_pending()[0]['state'], 'delivered')
            self.assertTrue((root / 'central/run/captures/session.bin').exists())
            self.assertFalse((root / 'central/run/screenshot.png').exists())

    def test_compacted_source_original_manifest_alias_tracks_integrity_and_revocation(self):
        for change in ('none', 'wrong_hash', 'capture', 'status', 'revoked'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = make_source(root / 'full', 'parent')
                original = digest(source / 'manifest.json')
                run, admission = compact_to_central(source, root / 'central')
                self.assertTrue(source_admitted(admission, 'parent', original))
                if change == 'wrong_hash': original = '0' * 64
                elif change == 'capture': (run / 'captures/session.bin').write_bytes(b'bad')
                elif change == 'status': (run / 'captures/session.status.json').write_text('{}')
                elif change == 'revoked':
                    receipt = admission / 'parent.json'; data = json.loads(receipt.read_text())
                    data['quality']['eligible'] = False; data['quality']['reasons'] = ['revoked']
                    atomic_json(receipt, data)
                self.assertEqual(source_admitted(admission, 'parent', original), change == 'none')

    def test_compact_alias_checks_transitive_lineage_each_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); central = root / 'central'; admission = central / '.admission'
            admission.mkdir(parents=True)
            grand = make_source(root / 'full', 'grand')
            grand_hash = digest(grand / 'manifest.json')
            compact_to_central(grand, central)
            parent = make_source(root / 'full', 'parent', lineage={'sourceRun': 'grand', 'sourceManifestSha256': grand_hash})
            parent_hash = digest(parent / 'manifest.json')
            compact_to_central(parent, central)
            self.assertTrue(source_admitted(admission, 'parent', parent_hash))
            receipt = admission / 'grand.json'; data = json.loads(receipt.read_text())
            data['quality']['eligible'] = False; data['quality']['reasons'] = ['revoked']
            atomic_json(receipt, data)
            self.assertFalse(source_admitted(admission, 'parent', parent_hash))

    def test_coverage_observations_match_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = make_source(root / 'full')
            run, _ = compact_to_central(source, root / 'central')
            full = CoverageInventory(root / 'full.sqlite'); compact = CoverageInventory(root / 'compact.sqlite')
            full.ingest(source); compact.ingest(run)
            def rows(inventory, table, fields):
                with inventory._db() as db:
                    return sorted(tuple(row) for row in db.execute('SELECT ' + fields + ' FROM ' + table))
            for table, fields in [('observations', 'target_id,count,evidence'),
                                  ('successes', 'target_id'), ('unknown_evidence', 'kind,identity')]:
                self.assertEqual(rows(full, table, fields), rows(compact, table, fields))

    def test_failed_payload_removed_only_for_terminal_compact_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failed = make_source(root / 'spool', 'failed')
            result = json.loads((failed / 'result.json').read_text())
            result.update(diagnosticPassed=False, taskSuccess=False, datasetEligible=False)
            atomic_json(failed / 'result.json', result)
            atomic_json(failed / 'manifest.json', artifact_manifest(failed, result))
            active = make_source(root / 'spool', 'active')
            historical = make_source(root / 'spool', 'historical')
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state', retry_seconds=0)
            store.enqueue('failed', compact=True); store.enqueue('active', compact=True)
            store.enqueue('historical'); store.deliver_pending()
            workspace = root / 'workspace'
            for name in ('failed', 'active', 'historical'):
                (workspace / name).mkdir(parents=True)
                (workspace / name / 'world').write_bytes(b'world')
            workers = [{'runId': 'failed', 'state': 'failed', 'coverageIngested': True}, {'runId': 'active', 'state': 'running', 'coverageIngested': True},
                       {'runId': 'historical', 'state': 'completed', 'coverageIngested': True}]
            store.prune_compact(workspace, workers)
            self.assertFalse(failed.exists())
            self.assertFalse((workspace / 'failed').exists())
            self.assertTrue(active.exists()); self.assertTrue((workspace / 'active').exists())
            self.assertTrue(historical.exists()); self.assertTrue((workspace / 'historical').exists())
            self.assertFalse((root / 'central/failed/captures').exists())
            self.assertFalse((root / 'central/failed/world-snapshot.tar.gz').exists())
            self.assertFalse(json.loads((root / 'central/.admission/failed.json').read_text())['quality']['eligible'])
            self.assertEqual(export_index(root / 'central', root / 'index.json', ['failed'])['rejected'][0]['runId'], 'failed')

    def test_recovery_after_published_run_before_database_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_source(root / 'spool')
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state', retry_seconds=0)
            store.enqueue('run', compact=True)
            publish = store._deliver_compact
            def interrupt(*args):
                publish(*args)
                raise KeyboardInterrupt('simulated process loss after publication')
            with patch.object(store, '_deliver_compact', side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt): store.deliver_pending()
            reopened = ArtifactStore(root / 'spool', root / 'central', root / 'state', retry_seconds=0)
            self.assertEqual(reopened.deliver_pending()[0]['state'], 'delivered')
            self.assertEqual(reopened.deliver_pending(), [])
            self.assertEqual(len(export_index(root / 'central', root / 'index.json')['accepted']), 1)

    def test_recovery_after_publication_before_receipt(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); make_source(root / 'spool')
                store = ArtifactStore(root / 'spool', root / 'central', root / 'state', retry_seconds=0)
                store.enqueue('run', compact=True)
                original_atomic = atomic_json
                def interrupt(path, value):
                    if Path(path) == root / 'central/.admission/run.json':
                        raise KeyboardInterrupt('simulated process loss before receipt')
                    return original_atomic(path, value)
                with patch('conductor.artifacts.atomic_json', side_effect=interrupt):
                    with self.assertRaises(KeyboardInterrupt): store.deliver_pending()
                self.assertFalse((root / 'central/.admission/run.json').exists())
                self.assertTrue((root / 'central/run').exists())
                self.assertEqual(export_index(root / 'central', root / 'index.json')['accepted'], [])
                if corrupt: (root / 'central/run/captures/session.bin').write_bytes(b'tampered')
                reopened = ArtifactStore(root / 'spool', root / 'central', root / 'state', retry_seconds=0)
                outcome = reopened.deliver_pending()[0]
                if corrupt:
                    self.assertNotEqual(outcome['state'], 'delivered')
                    self.assertFalse((root / 'central/.admission/run.json').exists())
                else:
                    self.assertEqual(outcome['state'], 'delivered')
                    self.assertEqual(len(export_index(root / 'central', root / 'index.json')['accepted']), 1)

    def test_cached_verification_invalidates_on_same_size_tamper_or_tree_change(self):
        for change in ('same_size', 'replace', 'symlink', 'extra'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = make_source(root / 'full')
                run, admission = compact_to_central(source, root / 'central')
                self.assertTrue(compact_quality(run, admission)['eligible'])
                with patch('conductor.compact.verify_manifest', side_effect=AssertionError('cache missed')):
                    self.assertTrue(compact_quality(run, admission)['eligible'])
                binary = run / 'captures/session.bin'
                if change == 'same_size':
                    st = binary.stat(); binary.write_bytes(b'xx'); os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns))
                elif change == 'replace':
                    other = binary.with_name('replacement'); other.write_bytes(b'xx'); other.replace(binary)
                elif change == 'symlink':
                    binary.unlink(); binary.symlink_to(source / 'captures/session.bin')
                else: (run / 'unexpected').write_text('unlisted')
                with self.assertRaises((ValueError, OSError)):
                    compact_quality(run, admission)

    def test_manual_receipt_revocation_survives_reaudit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_source(root / 'spool')
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            store.enqueue('run', compact=True); store.deliver_pending()
            receipt = root / 'central/.admission/run.json'
            data = json.loads(receipt.read_text())
            data['quality']['eligible'] = False; data['quality']['reasons'] = ['manual_revocation']
            atomic_json(receipt, data)
            self.assertFalse(compact_quality(root / 'central/run', receipt.parent)['eligible'])
            self.assertEqual(export_index(root / 'central', root / 'index.json')['accepted'], [])
            self.assertFalse(store.reaudit('run')['quality']['eligible'])
            self.assertFalse(json.loads(receipt.read_text())['quality']['eligible'])

    def test_new_policy_cannot_reassess_discarded_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = make_source(root / 'full')
            run, admission = compact_to_central(source, root / 'central')
            with patch('conductor.artifacts.QUALITY_POLICY_VERSION', QUALITY_POLICY_VERSION + 1):
                with self.assertRaises(ValueError): compact_quality(run, admission)
                self.assertEqual(export_index(root / 'central', root / 'index.json')['accepted'], [])

    def test_cleanup_retries_after_tombstone_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_source(root / 'spool')
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            store.enqueue('run', compact=True); store.deliver_pending()
            workspace = root / 'workspace'; (workspace / 'run').mkdir(parents=True)
            workers = [{'runId': 'run', 'state': 'completed', 'coverageIngested': True}]
            with patch('conductor.artifacts.shutil.rmtree', side_effect=OSError('simulated interruption')):
                with self.assertRaises(OSError): store.prune_compact(workspace, workers)
            self.assertTrue((root / 'spool/.compact-delete-run').exists())
            store.prune_compact(workspace, workers)
            self.assertFalse((root / 'spool/.compact-delete-run').exists())
            self.assertFalse((workspace / 'run').exists())
            self.assertTrue((root / 'central/run/captures/session.bin').exists())


if __name__ == '__main__': unittest.main()
