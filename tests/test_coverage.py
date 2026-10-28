import json
from collections import Counter
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.coverage import CoverageInventory, NoEligibleTask
from conductor.storage import artifact_manifest, atomic_json


def capture_run(root, run_id='run-one', task='@get oak_log 1', success=None, observations=None, semantics=None,
                include_script=False, complete=True):
    run = root / run_id
    captures = run / 'captures'
    captures.mkdir(parents=True)
    # Deliberately misleading packet names: coverage must use wire ID, protocol state and direction.
    (captures / 'session.bin').write_bytes(b'\x00\x00')
    rows = [{'t': 0, 'off': 0, 'len': 1, 'dir': 'C2S', 'state': 'play', 'name': 'not_a_real_name'},
            {'t': 1, 'off': 1, 'len': 1, 'dir': 'S2C', 'state': 'play', 'name': 'also_wrong'}]
    (captures / 'session.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    atomic_json(captures / 'session.status.json', {'complete': complete, 'bytes': 2})
    result = {'runId': run_id, 'diagnosticPassed': True, 'taskSuccess': success, 'capture': {'complete': complete}}
    atomic_json(run / 'metadata.json', {'runId': run_id, 'assignment': task, 'mode': 'diagnostic'})
    atomic_json(run / 'result.json', result)
    if observations is not None:
        heartbeat = {'observations': observations, 'observationSemantics': semantics or {}}
        (run / 'heartbeats.jsonl').write_text(json.dumps(heartbeat)+'\n')
    if include_script:
        atomic_json(run / 'script-result.json', {'status': 'exit', 'commands': [{'action': task, 'status': 'completed'}]})
    atomic_json(run / 'manifest.json', artifact_manifest(run, result))
    return run


def admit_run(inventory, run):
    from conductor.artifacts import QUALITY_POLICY_VERSION
    from conductor.storage import digest
    inventory.central_dir = run.parent
    atomic_json(run.parent / '.admission' / (run.name + '.json'),
                {'runId': run.name, 'manifestSha256': digest(run / 'manifest.json'),
                 'quality': {'policyVersion': QUALITY_POLICY_VERSION, 'eligible': True}})



class CoverageTests(unittest.TestCase):
    def test_requested_inventory_is_complete_and_assignment_is_not_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite', enabled_tasks=['get-oak_log'])
            assignment = inventory.choose('run-one')
            self.assertEqual(assignment, inventory.choose('run-one'))
            self.assertEqual(assignment['task'], '@get oak_log 1')
            before = inventory.snapshot()
            self.assertEqual(before['categories']['packet']['requested'], 224)
            self.assertEqual(before['categories']['item']['observed'], 0)
            self.assertEqual(before['categories']['task']['observed'], 0)
            self.assertIn('task:protocol-fixture-suite', before['unsupported'])
            run = capture_run(root / 'runs', success=True)
            admit_run(inventory, run)
            inventory.ingest(run)
            after = inventory.snapshot()
            self.assertEqual(after['categories']['task']['observed'], 0)
            self.assertEqual(after['categories']['task']['successful'], 0)

    def test_raw_wire_evidence_scopes_ids_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite')
            run = capture_run(root / 'runs')
            admit_run(inventory, run)
            first = inventory.ingest(run)
            self.assertEqual(first, inventory.ingest(run))
            data = inventory.snapshot()
            observed = {t['id'] for t in data['targets'] if t['observedRuns']}
            self.assertIn('packet:play:serverbound:0', observed)
            self.assertIn('packet:play:clientbound:0', observed)
            self.assertNotIn('packet:login:serverbound:0', observed)
            self.assertEqual(data['ingestedRuns'], 1)
            self.assertEqual(data['categories']['packet']['observed'], 2)

    def test_content_requires_actual_telemetry_semantics_and_success_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite')
            observed = {'items': ['minecraft:oak_log'], 'blocks': ['minecraft:stone'], 'mobs': ['minecraft:zombie']}
            run = capture_run(root / 'runs', success=True, observations=observed,
                              semantics={'items': 'inventory_present', 'blocks': 'crosshair_target', 'mobs': 'loaded_nearby'}, include_script=True)
            admit_run(inventory, run)
            inventory.ingest(run)
            data = inventory.snapshot()
            for category in ['item', 'block', 'mob']:
                self.assertEqual(data['categories'][category]['observed'], 1)
                self.assertEqual(data['categories'][category]['successful'], 0)
            self.assertEqual(data['categories']['task']['successful'], 1)
            other = capture_run(root / 'runs', 'run-two', observations={'items': ['minecraft:diamond']}, semantics={'items': 'requested'})
            inventory.ingest(other)
            diamond = next(t for t in inventory.snapshot()['targets'] if t['id'] == 'item:minecraft:diamond')
            self.assertEqual(diamond['observedRuns'], 0)

    def test_incomplete_sessions_do_not_credit_packets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite')
            inventory.ingest(capture_run(root / 'runs', complete=False))
            self.assertEqual(inventory.snapshot()['categories']['packet']['observed'], 0)

    def test_scheduler_balances_underrepresented_assignments_and_rejects_prerequisites(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite', enabled_tasks=['get-dirt', 'get-oak_log'])
            tasks = [inventory.choose('run-'+str(i))['task_id'] for i in range(10)]
            self.assertEqual(tasks.count('get-dirt'), 5)
            self.assertEqual(tasks.count('get-oak_log'), 5)
            restricted = CoverageInventory(root / 'restricted.sqlite', enabled_tasks=['observe-mobs'])
            with self.assertRaisesRegex(ValueError, 'No supported task'): restricted.choose('no-mobs-world')
            value = restricted.choose('mob-world', worlds=[{'id': 'forest', 'tags': ['mobs'], 'supported': True}])
            self.assertEqual(value['world_id'], 'forest')
            unsupported = CoverageInventory(root / 'unsupported.sqlite', enabled_tasks=['protocol-fixture-suite'])
            with self.assertRaises(ValueError): unsupported.choose('fixture', worlds=[{'id': 'fixture', 'tags': ['protocol-fixture']}])




class FailureSchedulingTests(unittest.TestCase):
    def test_actual_repeated_task_failures_cool_down_and_success_resets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite', enabled_tasks=['get-oak_log'], task_failure_limit=2, task_cooldown_seconds=900)
            for index in range(2):
                run_id = 'failure-'+str(index)
                inventory.choose(run_id)
                run = capture_run(root / 'runs', run_id, success=False, include_script=True)
                admit_run(inventory, run)
                inventory.ingest(run)
            task = next(t for t in inventory.snapshot()['targets'] if t['id'] == 'task:get-oak_log')
            self.assertEqual(task['failures'], 2)
            self.assertGreater(task['cooldownUntil'], __import__('time').time())
            with self.assertRaisesRegex(NoEligibleTask, 'No supported task') as pending:
                inventory.choose('during-cooldown')
            self.assertEqual(pending.exception.retry_at, task['cooldownUntil'])
            recovery = capture_run(root / 'runs', 'recovery', success=True, include_script=True)
            inventory.ingest(recovery)
            self.assertEqual(inventory.choose('after-recovery')['task_id'], 'get-oak_log')
            state = inventory.snapshot()['taskHealth'][0]
            self.assertEqual(state['consecutive_failures'], 0)
            self.assertEqual(state['failures'], 2)

    def test_pause_deadline_only_includes_compatible_enabled_tasks_and_expiry_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = CoverageInventory(Path(folder) / 'coverage.sqlite',
                                          enabled_tasks=['get-dirt', 'get-oak_log', 'observe-mobs'])
            with inventory._db() as db:
                for task, deadline in [('get-dirt',800),('get-oak_log',400),('observe-mobs',200),('nether',150)]:
                    db.execute('INSERT INTO task_health(task_id,cooldown_until) VALUES(?,?)', (task,deadline))
            with patch('conductor.coverage.time.time', return_value=100):
                with self.assertRaises(NoEligibleTask) as pending:
                    inventory.choose('waiting')
            self.assertEqual(pending.exception.retry_at,400)  # Mob world prerequisite and disabled Nether excluded.
            self.assertEqual(inventory.snapshot()['assignments'],0)
            with patch('conductor.coverage.time.time', return_value=401):
                self.assertEqual(inventory.choose('recovered')['task_id'],'get-oak_log')

    def test_unavailable_world_bank_has_no_fabricated_retry_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = CoverageInventory(Path(folder) / 'coverage.sqlite', world_bank=Path(folder)/'missing')
            with self.assertRaises(NoEligibleTask) as pending:
                inventory.choose('missing-bank')
            self.assertIsNone(pending.exception.retry_at)


class DiversitySchedulingTests(unittest.TestCase):
    def inventory(self, root, tasks, name='coverage', seed=73, **settings):
        catalog = root / 'catalog'
        catalog.mkdir(exist_ok=True)
        atomic_json(catalog / 'tasks.json', {'tasks': tasks})
        atomic_json(catalog / 'protocol-767.json', {'packets': []})
        atomic_json(catalog / 'registries-1.21.1.json', {'items': [], 'blocks': [], 'mobs': [], 'entities': []})
        (catalog / 'obtainable-items.txt').write_text('')
        return CoverageInventory(root / (name + '.sqlite'), catalog_path=catalog, seed=seed, **settings)

    def task(self, identifier, family):
        return {'id':identifier, 'family':family, 'command':'@get '+identifier+' 1',
                'seconds':60,'targets':[],'supported':True,'requiresTags':[]}

    def test_canary_cap_uses_live_reservations_and_keeps_regular_work_running(self):
        with tempfile.TemporaryDirectory() as folder:
            tasks = [self.task('canary-a','canary'), self.task('canary-b','canary'),
                     self.task('regular-a','gather'), self.task('regular-b','build')]
            inventory = self.inventory(Path(folder), tasks, canary_tasks=['canary-a','canary-b'], max_active_canaries=1)
            inventory.enabled_tasks = {'canary-a'}
            first = inventory.choose('canary-live')
            self.assertEqual(first['task_id'], 'canary-a')
            inventory.enabled_tasks = None
            active = ['canary-live']
            regular = []
            for index in range(6):
                run = f'regular-{index}'
                result = inventory.choose(run, pending_run_ids=active)
                regular.append(result['family'])
                self.assertTrue(result['task_id'].startswith('regular-'))
                active.append(run)
            self.assertEqual(set(regular), {'gather','build'})
            self.assertEqual(first, inventory.choose('canary-live', pending_run_ids=active))
            inventory.enabled_tasks = {'canary-b'}
            with self.assertRaises(NoEligibleTask):
                inventory.choose('all-canaries-busy', pending_run_ids=active)
            # Once the live reservation ends, a canary can be allocated again.
            inventory.enabled_tasks = {'canary-b'}
            self.assertEqual(inventory.choose('canary-next', pending_run_ids=active[1:])['task_id'], 'canary-b')

    def test_canaries_obey_context_enabled_tasks_and_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            tasks = [self.task('canary','canary'), self.task('regular','gather')]
            tasks[0]['requiresTags'] = ['prepared']
            inventory = self.inventory(Path(folder), tasks, canary_tasks=['canary'], max_active_canaries=1)
            plain = [{'id':'plain','tags':[]}]
            ready = [{'id':'ready','tags':['prepared']}]
            self.assertEqual(inventory.choose('plain', worlds=plain)['task_id'], 'regular')
            inventory.enabled_tasks = {'regular'}
            self.assertEqual(inventory.choose('excluded', worlds=ready)['task_id'], 'regular')
            inventory.enabled_tasks = None
            with inventory._db() as db:
                db.execute('INSERT INTO task_health(task_id,cooldown_until) VALUES(?,?)', ('canary',1000))
            with patch('conductor.coverage.time.time', return_value=100):
                self.assertEqual(inventory.choose('cooldown', worlds=ready)['task_id'], 'regular')
            inventory.enabled_tasks = {'canary'}
            with patch('conductor.coverage.time.time', return_value=1001):
                self.assertEqual(inventory.choose('recovered', worlds=ready)['task_id'], 'canary')

    def test_canary_configuration_validation_and_unrestricted_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            tasks=[self.task('canary','canary')]
            for index, options in enumerate(({'canary_tasks':'canary'}, {'canary_tasks':['unknown']},
                    {'max_active_canaries':-1}, {'max_active_canaries':True}, {'max_active_canaries':1.5})):
                with self.assertRaises(ValueError): self.inventory(root,tasks,name='bad'+str(index),**options)
            inventory=self.inventory(root,tasks,name='unlimited',canary_tasks=['canary'])
            first=inventory.choose('first')
            self.assertEqual(inventory.choose('second',pending_run_ids=['first'])['task_id'], first['task_id'])

    def test_earned_context_is_only_assigned_to_its_explicit_task(self):
        with tempfile.TemporaryDirectory() as folder:
            tasks=[self.task('organize','inventory'), self.task('gather','gather')]
            inventory=self.inventory(Path(folder),tasks)
            contexts=[{'id':'earned','tags':[], 'allowedTasks':['organize']},
                      {'id':'normal','tags':[], 'allowedTasks':None}]
            inventory.enabled_tasks={'gather'}
            self.assertEqual(inventory.choose('gather-run',worlds=contexts)['world_id'],'normal')
            with self.assertRaises(NoEligibleTask):
                inventory.choose('wrong-task',worlds=[contexts[0]])
            inventory.enabled_tasks={'organize'}
            self.assertEqual(inventory.choose('organize-run',worlds=[contexts[0]])['world_id'],'earned')

    def test_ordinary_world_bank_without_allowed_tasks_remains_schedulable(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            inventory=self.inventory(root,[self.task('gather','gather')])
            world=root/'bank'/'ordinary'/'world'
            world.mkdir(parents=True)
            (world/'level.dat').write_bytes(b'world')
            inventory.world_bank=root/'bank'
            self.assertEqual(inventory.choose('ordinary-bank')['world_id'], 'ordinary')

    def test_earned_context_stops_scheduling_when_source_admission_changes(self):
        from conductor.artifacts import QUALITY_POLICY_VERSION
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            central=root/'central'
            receipts=central/'.admission'
            receipts.mkdir(parents=True)
            inventory=self.inventory(root,[self.task('organize','inventory')],central_dir=central)
            from conductor.storage import digest
            source='source-run'; source_dir=central/source;source_dir.mkdir()
            atomic_json(source_dir/'manifest.json', {'source':'immutable'})
            manifest=digest(source_dir/'manifest.json')
            world={'id':'earned','tags':[], 'allowedTasks':['organize'],
                   'earnedContext':{'sourceRun':source, 'sourceManifestSha256':manifest}}
            receipt={'runId':source, 'manifestSha256':manifest,
                     'quality':{'policyVersion':QUALITY_POLICY_VERSION, 'eligible':True}}
            atomic_json(receipts/(source+'.json'),receipt)
            self.assertEqual(inventory.choose('accepted',worlds=[world])['world_id'],'earned')
            receipt['quality']['eligible']=False
            atomic_json(receipts/(source+'.json'),receipt)
            with self.assertRaises(NoEligibleTask): inventory.choose('revoked',worlds=[world])
            receipt['quality']={'policyVersion':QUALITY_POLICY_VERSION-1,'eligible':True}
            atomic_json(receipts/(source+'.json'),receipt)
            with self.assertRaises(NoEligibleTask): inventory.choose('stale',worlds=[world])
            receipt['quality']['policyVersion']=QUALITY_POLICY_VERSION
            receipt['manifestSha256']='b' * 64
            atomic_json(receipts/(source+'.json'),receipt)
            with self.assertRaises(NoEligibleTask): inventory.choose('changed',worlds=[world])
            (receipts/(source+'.json')).unlink()
            with self.assertRaises(NoEligibleTask): inventory.choose('missing',worlds=[world])

    def test_reaudit_retracts_effective_coverage_success_and_deficit_but_keeps_raw(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            inventory=self.inventory(root,[self.task('known','known'),self.task('unseen','unseen')])
            run=capture_run(root/'runs','proof',task='@get known 1',success=True,include_script=True)
            admit_run(inventory,run)
            inventory.ingest(run)
            target=lambda: next(t for t in inventory.snapshot()['targets'] if t['id']=='task:known')
            self.assertEqual((target()['observedRuns'],target()['successfulRuns'],target()['rawObservedRuns']), (1,1,1))
            # Pick a fixed tie that favors known when no admitted evidence exists.
            candidate=next('tie-'+str(i) for i in range(1000) if
                hashlib.sha256(f'73:tie-{i}:family:known'.encode()).hexdigest() >
                hashlib.sha256(f'73:tie-{i}:family:unseen'.encode()).hexdigest())
            self.assertEqual(inventory.choose(candidate)['task_id'],'unseen')
            with inventory._db() as db: db.execute('DELETE FROM assignments WHERE run_id=?',(candidate,))
            receipt=run.parent/'.admission'/'proof.json'
            data=json.loads(receipt.read_text());data['quality']['eligible']=False;atomic_json(receipt,data)
            self.assertEqual((target()['observedRuns'],target()['successfulRuns'],target()['rawObservedRuns']), (0,0,1))
            self.assertEqual(inventory.choose(candidate)['task_id'],'known')
            # Idempotent ingest must not resurrect revoked effective evidence.
            inventory.ingest(run)
            self.assertEqual(target()['observedRuns'],0)
            data['quality']['eligible']=True;data['quality']['policyVersion']=0;atomic_json(receipt,data)
            self.assertEqual(target()['observedRuns'],0)
            data['quality']['policyVersion']=4;data['manifestSha256']='0'*64;atomic_json(receipt,data)
            self.assertEqual(target()['observedRuns'],0)
            receipt.unlink()
            self.assertEqual(target()['observedRuns'],0)

    def test_missing_admission_root_is_raw_diagnostics_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inventory=self.inventory(root,[self.task('known','known')])
            inventory.ingest(capture_run(root/'runs','proof',task='@get known 1',success=True,include_script=True))
            snap=inventory.snapshot();task=next(t for t in snap['targets'] if t['id']=='task:known')
            self.assertEqual((task['observedRuns'],task['successfulRuns'],task['rawObservedRuns']),(0,0,1))
            self.assertEqual(snap['coverageScope'],'current_independent_admission')

    def test_revoking_earned_source_retracts_descendant_coverage_immediately(self):
        from conductor.artifacts import QUALITY_POLICY_VERSION
        from conductor.storage import digest
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inventory=self.inventory(root,[self.task('known','known')])
            run=capture_run(root/'runs','descendant',task='@get known 1',success=True,include_script=True)
            source_run=run.parent/'source';source_run.mkdir()
            atomic_json(source_run/'manifest.json', {'source':'immutable'})
            source_sha=digest(source_run/'manifest.json')
            metadata=json.loads((run/'metadata.json').read_text())
            metadata['world']={'earnedContext':{'sourceRun':'source','sourceManifestSha256':source_sha}}
            atomic_json(run/'metadata.json',metadata)
            result=json.loads((run/'result.json').read_text());atomic_json(run/'manifest.json',artifact_manifest(run,result))
            admit_run(inventory,run)
            source=run.parent/'.admission'/'source.json'
            receipt={'runId':'source','manifestSha256':source_sha,
                     'quality':{'policyVersion':QUALITY_POLICY_VERSION,'eligible':True}}
            atomic_json(source,receipt);inventory.ingest(run)
            target=lambda: next(t for t in inventory.snapshot()['targets'] if t['id']=='task:known')
            self.assertEqual(target()['observedRuns'],1)
            receipt['quality']['eligible']=False;atomic_json(source,receipt)
            self.assertEqual((target()['observedRuns'],target()['rawObservedRuns']),(0,1))

    def test_raw_gameplay_health_is_distinct_from_admitted_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inventory=self.inventory(root,[self.task('known','known')])
            with inventory._db() as db:
                db.execute('INSERT INTO task_health(task_id,failures,consecutive_failures,cooldown_until) VALUES(?,?,?,?)',
                           ('known',3,3,1000))
            run=capture_run(root/'runs','excluded',task='@get known 1',success=True,include_script=True)
            inventory.ingest(run) # no independent admission receipt
            snap=inventory.snapshot();target=next(t for t in snap['targets'] if t['id']=='task:known')
            self.assertEqual(target['successfulRuns'],0)
            self.assertEqual(snap['taskHealth'][0]['consecutive_failures'],0)
            self.assertEqual(snap['taskHealthScope'],'raw_worker_gameplay_outcomes_not_training_admission')

    def test_unequal_catalog_sizes_do_not_crowd_smaller_families(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tasks = [self.task('gather-'+str(i),'gather') for i in range(8)]
            tasks += [self.task('creative-'+str(i),'creative') for i in range(2)]
            tasks += [self.task('operator','operator')]
            inventory = self.inventory(root,tasks)
            assignments = [inventory.choose('rotation-'+str(i)) for i in range(30)]
            self.assertEqual(Counter(a['family'] for a in assignments), {'gather':10,'creative':10,'operator':10})
            gather = Counter(a['task_id'] for a in assignments if a['family']=='gather')
            self.assertEqual(len(gather),8)
            self.assertLessEqual(max(gather.values())-min(gather.values()),1)
            self.assertEqual(inventory.snapshot()['categories']['task']['observed'],0)

    def test_explicit_pending_work_spreads_families_and_does_not_infer_old_reservations_active(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = self.inventory(Path(folder),[self.task('old','gather'),self.task('new','creative')])
            inventory.enabled_tasks={'old'}
            for index in range(20): inventory.choose('historical-'+str(index))
            inventory.enabled_tasks=None
            first = inventory.choose('active-one')
            self.assertEqual(first['task_id'],'new')
            second = inventory.choose('active-two',pending_run_ids=['active-one'])
            self.assertEqual(second['task_id'],'old')
            self.assertEqual(second,inventory.choose('active-two',pending_run_ids=[]))
            # Old un-ingested reservations are history, not assumed active work.
            self.assertEqual(inventory.choose('next',pending_run_ids=[])['task_id'],'new')

    def test_pending_task_preference_operates_within_a_shared_family(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = self.inventory(Path(folder),[self.task('old','gather'),self.task('new','gather')])
            inventory.enabled_tasks={'old'}
            for index in range(20): inventory.choose('historical-'+str(index))
            inventory.enabled_tasks=None
            self.assertEqual(inventory.choose('active-one')['task_id'],'new')
            self.assertEqual(inventory.choose('active-two',pending_run_ids=['active-one'])['task_id'],'old')

    def test_world_rotation_is_balanced_deterministic_and_respects_pending_worlds(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            tasks=[self.task('one','gather')]
            left=self.inventory(root,tasks,'left')
            right=self.inventory(root,tasks,'right')
            worlds=[{'id':name,'supported':True} for name in ('forest','plains','desert')]
            a=[left.choose('world-'+str(i),worlds=worlds) for i in range(9)]
            b=[right.choose('world-'+str(i),worlds=list(reversed(worlds))) for i in range(9)]
            self.assertEqual(a,b)
            self.assertEqual(Counter(row['world_id'] for row in a),{'forest':3,'plains':3,'desert':3})
            active=left.choose('world-active',worlds=worlds)
            next_assignment=left.choose('world-next',worlds=worlds,pending_run_ids=['world-active'])
            self.assertNotEqual(active['world_id'],next_assignment['world_id'])

    def test_observed_deficits_still_prioritize_uncovered_family(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory=self.inventory(Path(folder),[self.task('known','gather'),self.task('unseen','creative')])
            run = capture_run(Path(folder) / 'runs', 'proof')
            admit_run(inventory, run)
            inventory.ingest(run)
            with inventory._db() as db:
                for target in ('task:known',inventory._context_id('known','survival',False)):
                    db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)',('proof',target,1,'[]'))
                    db.execute('INSERT OR IGNORE INTO successes VALUES(?,?)',('proof',target))
            self.assertEqual(inventory.choose('deficit')['task_id'],'unseen')

    def test_incompatible_or_cooling_families_never_earn_rotation_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            tasks=[self.task('gather','gather'),self.task('fixture','creative'),self.task('cooling','operator')]
            tasks[1]['requiresTags']=['fixture']
            inventory=self.inventory(Path(folder),tasks)
            with inventory._db() as db:
                db.execute('INSERT INTO task_health(task_id,cooldown_until) VALUES(?,?)',('cooling',1000))
            with patch('conductor.coverage.time.time',return_value=100):
                for index in range(3):
                    self.assertEqual(inventory.choose('safe-'+str(index))['task_id'],'gather')


class InteractionGapTests(unittest.TestCase):
    def test_presence_never_credits_requested_acquisition_use_or_interaction_facets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite')
            run = capture_run(root / 'runs', success=True, include_script=True,
                observations={'items': ['minecraft:oak_log'], 'blocks': ['minecraft:stone'], 'mobs': ['minecraft:zombie']},
                semantics={'items': 'inventory_present', 'blocks': 'crosshair_target', 'mobs': 'loaded_nearby'})
            admit_run(inventory, run)
            inventory.ingest(run)
            targets = {row['id']: row for row in inventory.snapshot()['targets']}
            for identity in ['item-acquire:minecraft:oak_log', 'item-use:minecraft:oak_log',
                             'item-inventory-interact:minecraft:oak_log', 'block-interact:minecraft:stone',
                             'block-statechange:minecraft:stone', 'mob-interact:minecraft:zombie']:
                self.assertIn(identity, targets)
                if identity != 'block-statechange:minecraft:stone':
                    self.assertFalse(targets[identity]['supported'])
                self.assertEqual(targets[identity]['observedRuns'], 0)
                self.assertEqual(targets[identity]['successfulRuns'], 0)
            self.assertEqual(targets['item:minecraft:oak_log']['observedRuns'], 1)
            self.assertEqual(targets['block:minecraft:stone']['observedRuns'], 1)
            self.assertEqual(targets['mob:minecraft:zombie']['observedRuns'], 1)


class ModeCurriculumTests(unittest.TestCase):
    def test_assignments_use_explicit_modes_permissions_and_world_compatibility(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cases = [('director', 'survival', False), ('creative_inventory', 'creative', False),
                     ('creative_build', 'creative', False), ('adventure_inspect', 'adventure', False),
                     ('spectator_explore', 'spectator', False), ('operator_commands', 'survival', True)]
            for task_id, mode, operator in cases:
                inventory = CoverageInventory(root / (task_id + '.sqlite'), enabled_tasks=[task_id])
                assignment = inventory.choose(task_id + '-run')
                self.assertEqual(assignment['gamemode'], mode)
                self.assertEqual(assignment['operator'], operator)
                if mode != 'survival':
                    with self.assertRaisesRegex(ValueError, 'No supported task'):
                        inventory.choose(task_id + '-wrong-world', worlds=[{'id': 'survival-only', 'gamemodes': ['survival']}])
            operator = CoverageInventory(root / 'restricted-op.sqlite', enabled_tasks=['operator_commands'])
            with self.assertRaisesRegex(ValueError, 'No supported task'):
                operator.choose('no-op-world', worlds=[{'id': 'restricted', 'allowOperator': False}])

    def test_actual_mode_and_server_permissions_are_separate_from_assignment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite', enabled_tasks=['creative_inventory'])
            assignment = inventory.choose('creative-run')
            self.assertEqual(inventory.snapshot()['categories']['mode']['observed'], 0)
            run = capture_run(root / 'runs', 'creative-run', task=assignment['task'], success=True, include_script=True)
            heartbeat = {'actualMode': 'survival', 'permissionLevel': 0, 'permissionSource': 'server_observer',
                         'abilities': {'creativeMode': False, 'allowFlying': False}}
            (run / 'heartbeats.jsonl').write_text(json.dumps(heartbeat)+'\n')
            result = json.loads((run / 'result.json').read_text())
            atomic_json(run / 'manifest.json', artifact_manifest(run, result))
            admit_run(inventory, run)
            inventory.ingest(run)
            targets = {t['id']: t for t in inventory.snapshot()['targets']}
            self.assertEqual(targets['mode:survival']['observedRuns'], 1)
            self.assertEqual(targets['mode:creative']['observedRuns'], 0)
            self.assertEqual(targets['permission:non_operator']['observedRuns'], 1)
            self.assertEqual(targets['task-context:creative_inventory:creative:non_operator']['observedRuns'], 0)

    def test_confirmed_scenario_action_events_credit_only_specific_observed_facets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite', enabled_tasks=['creative_inventory'])
            assignment = inventory.choose('creative-good')
            run = capture_run(root / 'runs', 'creative-good', task=assignment['task'], success=True, include_script=True)
            heartbeat = {'actualMode': 'creative', 'permissionLevel': 0, 'permissionSource': 'server_observer',
                         'abilities': {'creativeMode': True, 'allowFlying': True}}
            (run / 'heartbeats.jsonl').write_text(json.dumps(heartbeat)+'\n')
            atomic_json(run / 'scenario-result.json', {'scenario': 'creative_inventory', 'status': 'completed',
                'source': 'client_actions_plus_server_observer', 'evidence': {'serverConfirmed': True, 'events': [
                    {'kind': 'item_acquire', 'id': 'minecraft:diamond', 'serverConfirmed': True, 'success': True},
                    {'kind': 'inventory_interact', 'id': 'minecraft:diamond', 'serverConfirmed': True, 'success': True},
                    {'kind': 'item_use', 'id': 'minecraft:diamond', 'serverConfirmed': False, 'success': True}]}})
            result = json.loads((run / 'result.json').read_text())
            atomic_json(run / 'manifest.json', artifact_manifest(run, result))
            admit_run(inventory, run)
            inventory.ingest(run)
            targets = {t['id']: t for t in inventory.snapshot()['targets']}
            for identity in ['scenario:creative_inventory', 'task-context:creative_inventory:creative:non_operator',
                             'item-acquire:minecraft:diamond', 'item-inventory-interact:minecraft:diamond']:
                self.assertEqual(targets[identity]['observedRuns'], 1)
                self.assertEqual(targets[identity]['successfulRuns'], 1)
            self.assertEqual(targets['item-use:minecraft:diamond']['observedRuns'], 0)
            self.assertFalse(targets['item-use:minecraft:diamond']['supported'])

    def test_unconfirmed_permission_or_scenario_result_never_claims_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite', enabled_tasks=['operator_commands'])
            assignment = inventory.choose('op-unconfirmed')
            run = capture_run(root / 'runs', 'op-unconfirmed', task=assignment['task'], success=True, include_script=True)
            (run / 'heartbeats.jsonl').write_text(json.dumps({'actualMode':'survival','permissionLevel':2,'permissionSource':'client_synced'})+'\n')
            atomic_json(run / 'scenario-result.json', {'scenario':'operator_commands','status':'completed',
                'source':'client_actions_plus_server_observer','evidence':{'serverConfirmed':True,
                    'operatorCommand':{'command':'/time query daytime','serverConfirmed':True,'success':True}}})
            result = json.loads((run / 'result.json').read_text())
            atomic_json(run / 'manifest.json', artifact_manifest(run, result))
            admit_run(inventory, run)
            inventory.ingest(run)
            targets = {t['id']:t for t in inventory.snapshot()['targets']}
            self.assertEqual(targets['permission:operator']['observedRuns'],0)
            self.assertEqual(targets['permission:operator-command-success']['observedRuns'],0)
            self.assertEqual(targets['scenario:operator_commands']['observedRuns'],0)



class LimitedOperatorTests(unittest.TestCase):
    def test_level_one_is_limited_operator_and_not_non_operator(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite')
            run = capture_run(root / 'runs')
            (run / 'heartbeats.jsonl').write_text(json.dumps({'actualMode':'survival','permissionLevel':1,'permissionSource':'server_observer'})+'\n')
            result=json.loads((run / 'result.json').read_text())
            atomic_json(run / 'manifest.json',artifact_manifest(run,result))
            admit_run(inventory, run)
            inventory.ingest(run)
            targets={t['id']:t for t in inventory.snapshot()['targets']}
            self.assertEqual(targets['permission:operator']['observedRuns'],1)
            self.assertEqual(targets['permission:non_operator']['observedRuns'],0)
            self.assertEqual(targets['permission:operator-command-success']['observedRuns'],0)

if __name__ == '__main__': unittest.main()
