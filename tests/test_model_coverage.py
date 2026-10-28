import json
import tempfile
import unittest
from pathlib import Path

from conductor.coverage import CoverageInventory
from conductor.storage import artifact_manifest, atomic_json, digest
from conductor.artifacts import QUALITY_POLICY_VERSION


OUTPUT = 'packet:play:clientbound:9'
INPUT = 'packet:play:serverbound:36'


class ModelCoverageTests(unittest.TestCase):
    def task(self, identifier='mining', packet_targets=None, mechanics=None):
        return {'id':identifier,'family':'mechanics','command':'@activity '+identifier,'seconds':60,
                'supported':True,'targets':[],'packet_targets':packet_targets if packet_targets is not None else [OUTPUT,INPUT],
                'mechanics':mechanics if mechanics is not None else ['block_break','item_use']}

    def inventory(self, root, tasks=None):
        catalog=root/'catalog'; catalog.mkdir()
        atomic_json(catalog/'tasks.json',{'tasks':tasks or [self.task()]})
        atomic_json(catalog/'protocol-767.json',{'packets':[
            {'state':'play','direction':'clientbound','id':0,'name':'Bundle delimiter'},
            {'state':'play','direction':'clientbound','id':9,'name':'Block change'},
            {'state':'play','direction':'serverbound','id':36,'name':'Player action'}]})
        atomic_json(catalog/'model-packets.json',{'packets':[
            {'id':OUTPUT,'state':'play','direction':'clientbound','wireId':9,'name':'block_change','tier':'MODEL','role':'output'},
            {'id':INPUT,'state':'play','direction':'serverbound','wireId':36,'name':'block_dig','tier':'INPUT_ACK','role':'input'}]})
        atomic_json(catalog/'registries-1.21.1.json',{'items':['stone'],'blocks':[],'mobs':[],'entities':[]})
        (catalog/'obtainable-items.txt').write_text('stone\n')
        return CoverageInventory(root/'coverage.sqlite',catalog_path=catalog,central_dir=root/'runs')

    def captured_run(self, root, run_id, *, success=True, dispatched=True, proof=None):
        run=root/run_id; captures=run/'captures'; captures.mkdir(parents=True)
        # Deliberately misleading names. Both classified targets must come from wire IDs.
        (captures/'session.bin').write_bytes(bytes([9,36]))
        rows=[{'off':0,'len':1,'state':'play','dir':'S2C','name':'unrelated_name'},
              {'off':1,'len':1,'state':'play','dir':'C2S','name':'also_unrelated'}]
        (captures/'session.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        atomic_json(captures/'session.status.json',{'complete':True,'bytes':2})
        result={'runId':run_id,'taskSuccess':success,'diagnosticPassed':True,'capture':{'complete':True}}
        atomic_json(run/'metadata.json',{'runId':run_id,'assignment':'@activity mining','mode':'diagnostic'})
        atomic_json(run/'result.json',result)
        (run/'heartbeats.jsonl').write_text(json.dumps({'observations':{'items':['minecraft:stone']},
            'observationSemantics':{'items':'inventory_present'}})+'\n')
        if dispatched:
            atomic_json(run/'script-result.json',{'commands':[{'action':'@activity mining','status':'completed'}]})
        if proof is not None: atomic_json(run/'activity-result.json',proof)
        atomic_json(run/'manifest.json',artifact_manifest(run,result))
        atomic_json(root/'.admission'/(run_id+'.json'), {'runId':run_id,'manifestSha256':digest(run/'manifest.json'),
                    'quality':{'policyVersion':QUALITY_POLICY_VERSION,'eligible':True}})
        return run

    def proof(self, *, event=True, completed=True, goal=True):
        value={'activity':'mining','status':'completed' if completed else 'failed','source':'focused_activity_task',
               'evidence':{'actionObserved':True,'goalTaskFinished':goal}}
        if event:
            value['evidence']['mechanics']=[{'id':'block_break','success':True,'actionObserved':True,
                'goalTaskFinished':goal,'source':'client_action_and_task_outcome'}]
        return value

    def test_canonical_subset_keeps_model_outputs_distinct_from_input_context_and_all_raw_types(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory=CoverageInventory(Path(folder)/'coverage.sqlite')
            snapshot=inventory.snapshot(); model=snapshot['modelPacketCoverage']
            self.assertEqual(snapshot['categories']['packet']['requested'],224)
            self.assertEqual((model['requested'],model['output']['requested'],model['input']['requested']),(87,57,30))
            self.assertEqual(model['observed'],0)
            self.assertTrue(all(row['readerTier'] not in ('ENGINE','BAKED','DERIVED','DROP') for row in model['targets']))
            for digest in model['source']['files'].values(): self.assertEqual(len(digest),64)

    def test_declarations_and_item_presence_do_not_credit_packets_or_mechanic_outcomes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); inventory=self.inventory(root)
            assignment=inventory.choose('declared')
            self.assertIn(OUTPUT,assignment['target_ids'])
            self.assertIn('mechanic:block_break',assignment['target_ids'])
            before=inventory.snapshot()
            self.assertEqual(before['modelPacketCoverage']['observed'],0)
            self.assertEqual(before['categories']['mechanic']['observed'],0)
            run=self.captured_run(root/'runs','declared')
            inventory.ingest(run); inventory.ingest(run)
            after=inventory.snapshot()
            self.assertEqual(after['modelPacketCoverage']['observed'],2)
            self.assertEqual(after['modelPacketCoverage']['output']['observed'],1)
            self.assertEqual(after['modelPacketCoverage']['input']['observed'],1)
            self.assertEqual(after['categories']['mechanic']['observed'],0)
            self.assertEqual(after['categories']['item']['observed'],1)
            packets=after['modelPacketCoverage']['byTask'][0]['packets']
            self.assertEqual({row['id']:row['observedRuns'] for row in packets},{OUTPUT:1,INPUT:1})
            self.assertTrue(all(row['successfulRuns']==0 for row in after['modelPacketCoverage']['targets']))

    def test_generic_completed_wrapper_or_undispatched_task_is_not_mechanic_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); inventory=self.inventory(root)
            inventory.ingest(self.captured_run(root/'runs','generic',proof=self.proof(event=False)))
            inventory.ingest(self.captured_run(root/'runs','not-dispatched',dispatched=False,proof=self.proof()))
            snapshot=inventory.snapshot()
            self.assertEqual(snapshot['categories']['mechanic']['observed'],0)
            self.assertEqual(snapshot['modelPacketCoverage']['targets'][0]['observedRuns'],2)
            self.assertTrue(all(row['observedRuns']==1 for row in snapshot['modelPacketCoverage']['byTask'][0]['packets']))

    def test_finite_action_proof_credits_only_specific_mechanic_and_success_requires_task_outcome(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); inventory=self.inventory(root)
            inventory.ingest(self.captured_run(root/'runs','failed-goal',success=False,proof=self.proof(completed=False,goal=False)))
            before={row['id']:row for row in inventory.snapshot()['targets']}
            self.assertEqual(before['mechanic:block_break']['observedRuns'],1)
            self.assertEqual(before['mechanic:block_break']['successfulRuns'],0)
            inventory.ingest(self.captured_run(root/'runs','proved',proof=self.proof()))
            targets={row['id']:row for row in inventory.snapshot()['targets']}
            self.assertEqual(targets['mechanic:block_break']['observedRuns'],2)
            self.assertEqual(targets['mechanic:block_break']['successfulRuns'],1)
            self.assertEqual(targets['mechanic:item_use']['observedRuns'],0)

    def test_engine_targets_are_rejected_and_packet_deficits_change_scheduling(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            tasks=[self.task('covered',[OUTPUT],[]),self.task('missing',[INPUT],[])]
            inventory=self.inventory(root,tasks)
            run=self.captured_run(root/'runs','wire-evidence')
            with inventory._db() as db:
                db.execute('INSERT INTO runs VALUES(?,?,?,?)',('wire-evidence',digest(run/'manifest.json'),0,'{}'))
                db.execute('INSERT INTO observations VALUES(?,?,?,?)',('wire-evidence',OUTPUT,1,'[]'))
            self.assertEqual(inventory.choose('packet-deficit')['task_id'],'missing')
            tasks[0]['packet_targets']=['packet:play:clientbound:0']
            atomic_json(root/'catalog/tasks.json',{'tasks':tasks})
            with self.assertRaisesRegex(ValueError,'canonical model-relevant'):
                CoverageInventory(root/'invalid.sqlite',catalog_path=root/'catalog')


if __name__=='__main__': unittest.main()
