import hashlib
import importlib.util
import sys
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.storage import atomic_json, inspect_world_bank, stage_world, world_revision
from conductor.worker import stage_earned_context

OLD = '7c87873f-cf67-3c06-ab10-91902fd1ffa8'
TARGETS = ['f15bc9cf-04c1-3407-a3cd-4acb8bf76d9a', 'a6952800-5df9-32c7-af77-1c95810be734']


def fixture(root):
    entry = root / 'bank/prepared'; world = entry / 'world'; (world / 'playerdata').mkdir(parents=True)
    (world / 'level.dat').write_bytes(b'level'); (world / 'region').mkdir(); (world / 'region/r.0.0.mca').write_bytes(b'terrain')
    for suffix in ('.dat', '.dat_old'): (world / 'playerdata' / (OLD + suffix)).write_bytes(('player'+suffix).encode())
    (world / 'stats').mkdir(); (world / 'stats' / (OLD+'.json')).write_bytes(b'stats')
    rev = world_revision(world)['revision']
    manifest=b'{"fixture":true}'; (entry/'source-run-manifest.json').write_bytes(manifest)
    source_manifest_hash=hashlib.sha256(manifest).hexdigest()
    receipt=json.dumps({'runId':'earned-run','manifestSha256':source_manifest_hash,'quality':{'policyVersion':4,'eligible':True}}).encode()
    original_meta=b'{"fixture":"source"}'
    (entry/'source-admission-receipt.json').write_bytes(receipt)
    admission=root/'admission'; admission.mkdir(); (admission/'earned-run.json').write_bytes(receipt)
    (entry/'source-original-world.json').write_bytes(original_meta)
    config = {'revision': rev, 'tags': ['inventory-sort-kit'], 'gamemodes': ['survival'], 'allowOperator': False, 'allowedTasks': ['mechanic-organize-inventory'],
        'earnedContextTemplate': {'kind': 'earned_inventory_sort_v1', 'sourceUuid': OLD, 'sourceRun': 'earned-run',
            'sourceManifestSha256': source_manifest_hash, 'sourceAdmissionSha256': hashlib.sha256(receipt).hexdigest(),
            'sourceWorldMetadataSha256': hashlib.sha256(original_meta).hexdigest(), 'sourceWorldRevision': rev,
            'sortProof': {'sourceSlot': 8, 'hotbarSlot': 1, 'item': 'minecraft:stone_pickaxe', 'count': 1, 'hotbarEmpty': True}}}
    atomic_json(entry/'world.json', config)
    return entry, world, config


def fake_node(argv, **kwargs):
    staged, old, target = Path(argv[2]), argv[3], argv[4]
    records = []
    for suffix in ('.dat', '.dat_old'):
        src = staged/'playerdata'/(old+suffix); dst = staged/'playerdata'/(target+suffix)
        original=src.read_bytes(); output=original + target.encode(); dst.write_bytes(output); src.unlink()
        records.append({'source': f'playerdata/{old}{suffix}', 'target': f'playerdata/{target}{suffix}',
                        'sourceSha256': hashlib.sha256(original).hexdigest(), 'targetSha256': hashlib.sha256(output).hexdigest(),
                        'onlyUuidChanged': True})
    src=staged/'stats'/(old+'.json'); dst=staged/'stats'/(target+'.json'); raw=src.read_bytes(); src.rename(dst)
    records.append({'source': f'stats/{old}.json', 'target': f'stats/{target}.json',
                    'sha256': hashlib.sha256(raw).hexdigest(), 'onlyFilenameChanged': True})
    return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'sourceUuid':old, 'targetUuid':target,
        'files':records, 'uuidOnlyGameplayStateUnchanged':True}))


class EarnedContextTests(unittest.TestCase):
    def test_two_run_ids_keep_source_unchanged_and_write_bound_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); entry, source, _=fixture(root)
            source_before=world_revision(source)
            for i, target in enumerate(TARGETS):
                dest=root/f'staged{i}'; artifacts=root/f'artifacts{i}'; artifacts.mkdir()
                world=stage_world(root/'bank','prepared',dest,allow_copy=True)
                with patch.dict(os.environ, {'SOURCE_ADMISSIONS_DIR':str(root/'admission')}), patch('conductor.worker.subprocess.run', side_effect=fake_node):
                    self.assertTrue(stage_earned_context(root/'bank','prepared',world,dest,artifacts,f'run{i}',target,'survival',False,'@activity organize_inventory'))
                proof=json.loads((artifacts/'earned-context-migration.json').read_text())
                self.assertEqual(proof['targetUuid'],target); self.assertEqual(proof['runId'],f'run{i}')
                self.assertEqual(proof['sourceWorldRevision'],source_before['revision'])
                self.assertFalse((dest/'playerdata'/(OLD+'.dat')).exists())
                self.assertEqual(world_revision(source),source_before)
            self.assertTrue(inspect_world_bank(root/'bank')[0]['supported'])

    def test_template_must_have_truthful_sort_gate_and_nonoperator_survival(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); entry, source, config=fixture(root)
            config['tags']=[]; atomic_json(entry/'world.json',config)
            self.assertFalse(inspect_world_bank(root/'bank')[0]['supported'])
            config['tags']=['inventory-sort-kit']; atomic_json(entry/'world.json',config)
            dest=root/'staged'; artifacts=root/'artifacts'; artifacts.mkdir()
            world=stage_world(root/'bank','prepared',dest,allow_copy=True)
            with self.assertRaisesRegex(ValueError,'non-operator survival'):
                stage_earned_context(root/'bank','prepared',world,dest,artifacts,'run',TARGETS[0],'creative',False,'@activity organize_inventory')

    def test_template_creator_checks_admission_server_inventory_and_copies_original(self):
        from test_training import make_run
        from conductor.artifacts import ArtifactStore
        from conductor.storage import artifact_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); entry, source, _=fixture(root)
            run=make_run(root/'spool','earned-run',1)
            metadata=json.loads((run/'metadata.json').read_text())
            metadata.update(assignedGamemode='survival',operator=False,identity={'uuid':OLD},assignment='@activity organize_inventory')
            metadata['world'].update(sourceSeed='123',revision=world_revision(source)['revision'],
                                     sourceMetadataSha256=hashlib.sha256((entry/'world.json').read_bytes()).hexdigest())
            atomic_json(run/'metadata.json',metadata)
            atomic_json(run/'activity-result.json',{'goalStartedWall':101})
            result=json.loads((run/'result.json').read_text()); result['exitCodes']={'server':0}
            atomic_json(run/'result.json',result)
            # Omitting assignedGamemode keeps this minimal fixture from requiring mode heartbeats;
            # the creation check itself still requires survival/non-operator metadata.
            snapshot={'source':'fabric_end_server_tick','wall':100,'playerSnapshots':[{'uuid':OLD,
                'mode':'survival','permissionLevel':0,'inventory':{'minecraft:stone_pickaxe':1},'position':[1,2,3]}]}
            (run/'server-heartbeats.jsonl').write_text(json.dumps(snapshot)+'\n')
            atomic_json(run/'manifest.json',artifact_manifest(run,result))
            receipt=root/'receipt.json'; atomic_json(receipt,{'runId':run.name,'manifestSha256':hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest(), 'quality':{'policyVersion':4,'eligible':True}})
            script=Path(__file__).resolve().parents[1]/'scripts/create-earned-inventory-template.py'
            spec=importlib.util.spec_from_file_location('template_creator',script); module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            observed={'sourcePlayer':{'position':[1,2,3],'inventory':{'minecraft:stone_pickaxe':1}}}
            target=root/'newbank'; argv=['create','--source-run',str(run),'--source-world',str(source),'--admission-receipt',str(receipt),'--bank',str(target),'--world-id','earned','--runtime-image','image:test']
            # The integration of admission itself is tested elsewhere; isolate creator checks here.
            with patch.object(sys,'argv',argv), patch.object(module,'quality_report',return_value={'eligible':True}), patch.object(module.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(observed))):
                module.main()
            self.assertEqual(world_revision(source),world_revision(target/'earned/world'))
            self.assertEqual(json.loads((target/'earned/world.json').read_text())['allowedTasks'],['mechanic-organize-inventory'])
            self.assertEqual((target/'earned/source-admission-receipt.json').read_bytes(),receipt.read_bytes())
            bad=json.loads(receipt.read_text());bad['quality']['eligible']=False;atomic_json(receipt,bad)
            argv[-3]='other'
            with patch.object(sys,'argv',argv), patch.object(module,'quality_report',return_value={'eligible':True}):
                with self.assertRaisesRegex(ValueError,'receipt'):
                    module.main()

    def test_revoked_source_is_blocked_before_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); entry, source, _=fixture(root)
            dest=root/'staged'; artifacts=root/'artifacts'; artifacts.mkdir()
            world=stage_world(root/'bank','prepared',dest,allow_copy=True)
            receipt=root/'admission/earned-run.json'; data=json.loads(receipt.read_text())
            data['quality']['eligible']=False; atomic_json(receipt,data)
            with patch.dict(os.environ, {'SOURCE_ADMISSIONS_DIR':str(root/'admission')}), patch('conductor.worker.subprocess.run') as node:
                with self.assertRaisesRegex(ValueError,'revoked'):
                    stage_earned_context(root/'bank','prepared',world,dest,artifacts,'run',TARGETS[0],'survival',False,'@activity organize_inventory')
                node.assert_not_called()
            self.assertTrue((dest/'playerdata'/(OLD+'.dat')).exists())

    def test_nonidentity_change_or_wrong_proof_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); entry, source, _=fixture(root)
            dest=root/'staged'; artifacts=root/'artifacts'; artifacts.mkdir()
            world=stage_world(root/'bank','prepared',dest,allow_copy=True)
            def corrupt(argv, **kwargs):
                result=fake_node(argv,**kwargs); (dest/'region/r.0.0.mca').write_bytes(b'changed'); return result
            with patch.dict(os.environ, {'SOURCE_ADMISSIONS_DIR':str(root/'admission')}), patch('conductor.worker.subprocess.run', side_effect=corrupt):
                with self.assertRaisesRegex(ValueError,'nonidentity'):
                    stage_earned_context(root/'bank','prepared',world,dest,artifacts,'run',TARGETS[0],'survival',False,'@activity organize_inventory')
            self.assertFalse((artifacts/'earned-context-migration.json').exists())
            self.assertEqual((source/'region/r.0.0.mca').read_bytes(),b'terrain')

if __name__ == '__main__': unittest.main()
