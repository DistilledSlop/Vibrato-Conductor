import json
import tempfile
import unittest
from pathlib import Path

from conductor.coverage import CoverageInventory
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
            inventory.ingest(run)
            after = inventory.snapshot()
            self.assertEqual(after['categories']['task']['observed'], 0)
            self.assertEqual(after['categories']['task']['successful'], 0)

    def test_raw_wire_evidence_scopes_ids_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite')
            run = capture_run(root / 'runs')
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
                inventory.ingest(run)
            task = next(t for t in inventory.snapshot()['targets'] if t['id'] == 'task:get-oak_log')
            self.assertEqual(task['failures'], 2)
            self.assertGreater(task['cooldownUntil'], __import__('time').time())
            with self.assertRaisesRegex(ValueError, 'No supported task'): inventory.choose('during-cooldown')
            recovery = capture_run(root / 'runs', 'recovery', success=True, include_script=True)
            inventory.ingest(recovery)
            self.assertEqual(inventory.choose('after-recovery')['task_id'], 'get-oak_log')
            state = inventory.snapshot()['taskHealth'][0]
            self.assertEqual(state['consecutive_failures'], 0)
            self.assertEqual(state['failures'], 2)


class InteractionGapTests(unittest.TestCase):
    def test_presence_never_credits_requested_acquisition_use_or_interaction_facets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = CoverageInventory(root / 'coverage.sqlite')
            run = capture_run(root / 'runs', success=True, include_script=True,
                observations={'items': ['minecraft:oak_log'], 'blocks': ['minecraft:stone'], 'mobs': ['minecraft:zombie']},
                semantics={'items': 'inventory_present', 'blocks': 'crosshair_target', 'mobs': 'loaded_nearby'})
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
            inventory.ingest(run)
            targets={t['id']:t for t in inventory.snapshot()['targets']}
            self.assertEqual(targets['permission:operator']['observedRuns'],1)
            self.assertEqual(targets['permission:non_operator']['observedRuns'],0)
            self.assertEqual(targets['permission:operator-command-success']['observedRuns'],0)

if __name__ == '__main__': unittest.main()
