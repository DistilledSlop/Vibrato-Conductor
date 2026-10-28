import json
import importlib.util
import sys
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from conductor.artifacts import ArtifactStore
from conductor.storage import artifact_manifest, atomic_json, digest
from conductor.training import export_index, import_run


def make_run(root, name, seed):
    run = root / name
    captures = run / 'captures'
    captures.mkdir(parents=True)
    base = captures / ('session_' + name)
    base.with_suffix('.bin').write_bytes(b'\x00\x00')
    base.with_suffix('.jsonl').write_text('\n'.join(json.dumps(v) for v in [
        {'off': 0, 'len': 1, 'dir': 'C2S', 'state': 'play'},
        {'off': 1, 'len': 1, 'dir': 'S2C', 'state': 'play'}]) + '\n')
    atomic_json(captures / (base.name + '.status.json'), {'complete': True, 'bytes': 2})
    health = {'valid': True, 'samples': 12, 'meanTps': 20, 'degradedWindows': 0}
    result = {'runId': name, 'diagnosticPassed': True, 'taskSuccess': True, 'datasetEligible': True,
              'capture': {'complete': True}, 'serverHealth': health, 'clientHealth': health}
    atomic_json(run / 'metadata.json', {'runId': name, 'assignment': '@get oak_log 1', 'mode': 'collection',
                'versions': {'minecraft': '1.21.1'}, 'build': {'source': 'fixture'},
                'world': {'id': 'world', 'revision': 'fixture', 'sourceSeed': str(seed)}})
    atomic_json(run / 'result.json', result)
    atomic_json(run / 'manifest.json', artifact_manifest(run, result))
    return run


class TrainingIntegrationTest(unittest.TestCase):
    def test_import_is_idempotent_and_indexes_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_run(root / 'isolated', 'isolated-one', 1)
            original = (source / 'manifest.json').read_bytes()
            config = root / 'config.json'
            atomic_json(config, {'fleet_id': 'test', 'state_dir': str(root / 'control'),
                'artifacts_dir': str(root / 'spool'), 'workspace_dir': str(root / 'workspaces'),
                'central_dir': str(root / 'central'), 'accept_eula': True, 'accept_offline': True})
            one = import_run(source, config)
            two = import_run(source, config)
            self.assertEqual(one, two)
            self.assertEqual(original, (source / 'manifest.json').read_bytes())
            self.assertEqual(digest(source / 'manifest.json'), digest(root / 'central/isolated-one/manifest.json'))
            receipt = root / 'central/.admission/isolated-one.json'
            self.assertTrue(json.loads(receipt.read_text())['quality']['eligible'])
            revoked = json.loads(receipt.read_text()); revoked['quality']['eligible'] = False
            revoked['quality']['reasons'] = ['revoked']; atomic_json(receipt, revoked)
            saved = receipt.read_bytes()
            with self.assertRaisesRegex(ValueError, 'revoked'):
                import_run(source, config)
            self.assertEqual(receipt.read_bytes(), saved)

    def test_export_obeys_current_receipts_rechecks_integrity_and_never_splits_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            for name, seed in [('a', 1), ('b', 1), ('c', 2), ('bad', 3)]:
                make_run(root / 'spool', name, seed)
                store.enqueue(name)
            store.deliver_pending()
            receipt = root / 'central/.admission/bad.json'
            revoked = json.loads(receipt.read_text()); revoked['quality']['eligible'] = False
            revoked['quality']['reasons'] = ['revoked']; atomic_json(receipt, revoked)
            index = export_index(root / 'central', root / 'index.json')
            self.assertEqual(len(index['accepted']), 3)
            self.assertEqual(index['rejected'][0]['runId'], 'bad')
            self.assertTrue(index['split']['ready'])
            train, val = set(index['trainBases']), set(index['validationBases'])
            self.assertTrue(train and val)
            def side(name):
                return 'train' if any('/' + name + '/captures/' in p for p in train) else 'val'
            self.assertEqual(side('a'), side('b'))
            self.assertNotEqual(side('a'), side('c'))
            # A receipt revocation after a previous export removes the source on the next export.
            receipt = root / 'central/.admission/a.json'
            revoked = json.loads(receipt.read_text()); revoked['quality']['eligible'] = False; atomic_json(receipt, revoked)
            newer = export_index(root / 'central', root / 'index.json')
            self.assertNotIn('a', [r['runId'] for r in newer['accepted']])
            self.assertEqual(newer, json.loads((root / 'index.json').read_text()))

    def test_opt_in_builder_uses_admitted_split_and_publishes_trainer_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            for name, seed in [('one', 1), ('two', 2)]:
                make_run(root / 'spool', name, seed); store.enqueue(name)
            store.deliver_pending()
            builder = root / 'transformer/reader/dataset/dataset.js'
            builder.parent.mkdir(parents=True); builder.write_text('// fixture')
            script = Path(__file__).resolve().parents[1] / 'scripts/build-admitted-dataset.py'
            spec = importlib.util.spec_from_file_location('admitted_builder', script)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            calls = []
            def fake_run(argv, check):
                calls.append(argv)
                stage = Path(argv[argv.index('--out') + 1]); stage.mkdir()
                (stage / 'vocab.json').write_text('{}')
                atomic_json(stage / 'manifest.json', {'vocab': str(stage / 'vocab.json'), 'sessions': []})
            output = root / 'dataset'
            with patch.object(sys, 'argv', ['build', '--central', str(root / 'central'), '--output', str(output), '--transformer', str(root / 'transformer')]), patch.object(module.subprocess, 'run', side_effect=fake_run):
                module.main()
            self.assertEqual(len(calls), 1)
            self.assertIn('--val', calls[0])
            index = json.loads((output / 'source-index.json').read_text())
            self.assertEqual((output / 'val.txt').read_text().splitlines(), index['validationSessionNames'])
            self.assertEqual(json.loads((output / 'manifest.json').read_text())['vocab'], str(output / 'vocab.json'))

    def test_builder_uses_image_runtime_with_read_only_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            for name, seed in [('one', 1), ('two', 2)]:
                make_run(root / 'spool', name, seed); store.enqueue(name)
            store.deliver_pending()
            builder = root / 'transformer/reader/dataset/dataset.js'
            builder.parent.mkdir(parents=True); builder.write_text('// fixture')
            script = Path(__file__).resolve().parents[1] / 'scripts/build-admitted-dataset.py'
            spec = importlib.util.spec_from_file_location('admitted_image_builder', script)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            calls = []
            def fake_run(argv, check):
                calls.append(argv)
                stage = Path(argv[argv.index('--out') + 1]); stage.mkdir()
                atomic_json(stage / 'manifest.json', {'vocab': str(stage / 'vocab.json'), 'sessions': []})
            with patch.object(sys, 'argv', ['build', '--central', str(root / 'central'), '--output', str(root / 'dataset'), '--transformer', str(root / 'transformer'), '--runtime-image', 'worker:test']), patch.object(module.subprocess, 'run', side_effect=fake_run):
                module.main()
            argv = calls[0]
            self.assertEqual(argv[0], 'docker')
            self.assertEqual(argv[argv.index('--network') + 1], 'none')
            self.assertIn('worker:test', argv)
            self.assertTrue(any('dst=/source-reader,readonly' in a for a in argv))
            self.assertTrue(any('dst=' + str(root / 'central') + ',readonly' in a for a in argv))

    def test_explicit_selection_rejects_missing_ids_and_ignores_unselected_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            for name, seed in [('one', 1), ('two', 2), ('unselected', 3)]:
                make_run(root / 'spool', name, seed); store.enqueue(name)
            store.deliver_pending()
            selected = export_index(root / 'central', root / 'index.json', ['one', 'two'])
            self.assertEqual(selected['selectedRunIds'], ['one', 'two'])
            self.assertEqual({r['runId'] for r in selected['accepted']}, {'one', 'two'})
            self.assertTrue(selected['split']['ready'])
            with self.assertRaisesRegex(ValueError, 'absent'):
                export_index(root / 'central', root / 'index.json', ['missing'])

    def test_source_revocation_excludes_derived_run_and_reaudits_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=ArtifactStore(root/'spool',root/'central',root/'state')
            source=make_run(root/'spool','source',1); store.enqueue('source'); store.deliver_pending()
            child=make_run(root/'spool','derived',2)
            metadata=json.loads((child/'metadata.json').read_text())
            metadata['world']['earnedContext']={'sourceRun':'source','sourceManifestSha256':digest(source/'manifest.json')}
            atomic_json(child/'metadata.json',metadata)
            atomic_json(child/'manifest.json',artifact_manifest(child,json.loads((child/'result.json').read_text())))
            store.enqueue('derived'); store.deliver_pending()
            self.assertTrue(json.loads((root/'central/.admission/derived.json').read_text())['quality']['eligible'])
            receipt=root/'central/.admission/source.json'; data=json.loads(receipt.read_text())
            data['quality']['eligible']=False; data['quality']['reasons']=['revoked']; atomic_json(receipt,data)
            output=export_index(root/'central',root/'index.json')
            self.assertNotIn('derived',[r['runId'] for r in output['accepted']])
            self.assertIn('earned_context_source_unconfirmed',next(r['reason'] for r in output['rejected'] if r['runId']=='derived'))
            audits=store.reaudit_outdated(limit=10)
            self.assertEqual([a['runId'] for a in audits],['derived'])
            self.assertFalse(json.loads((root/'central/.admission/derived.json').read_text())['quality']['eligible'])

    def test_banked_world_without_source_seed_never_uses_task_rng(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            for name, task_seed in [('first', 101), ('second', 202)]:
                run = make_run(root / 'spool', name, 1)
                metadata = json.loads((run / 'metadata.json').read_text())
                metadata['seed'] = task_seed
                metadata['world'].pop('sourceSeed')
                atomic_json(run / 'metadata.json', metadata)
                atomic_json(run / 'manifest.json', artifact_manifest(run, json.loads((run / 'result.json').read_text())))
                store.enqueue(name)
            store.deliver_pending()
            index = export_index(root / 'central', root / 'index.json')
            self.assertEqual(index['accepted'], [])
            self.assertEqual({r['reason'] for r in index['rejected']}, {'banked_world_source_seed_missing'})

    def test_old_policy_and_tampered_capture_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / 'spool', root / 'central', root / 'state')
            for name in ('old', 'tampered'):
                make_run(root / 'spool', name, 1); store.enqueue(name)
            store.deliver_pending()
            receipt = root / 'central/.admission/old.json'
            data = json.loads(receipt.read_text()); data['quality']['policyVersion'] = 2; atomic_json(receipt, data)
            (root / 'central/tampered/captures/session_tampered.bin').write_bytes(b'changed')
            index = export_index(root / 'central', root / 'index.json')
            self.assertEqual(index['accepted'], [])
            self.assertFalse(index['split']['ready'])
            self.assertEqual({r['runId'] for r in index['rejected']}, {'old', 'tampered'})


if __name__ == '__main__': unittest.main()
