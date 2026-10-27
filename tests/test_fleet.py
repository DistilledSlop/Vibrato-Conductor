import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.fleet import Fleet, FleetConfig
from conductor.storage import atomic_json, artifact_manifest


class Clock:
    def __init__(self): self.value = 1800000000
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Runtime:
    def __init__(self): self.workers = {}; self.creates = []; self.stops = []; self.removes = []; self.fail = False; self.ambiguous = False
    def list_workers(self): return list(self.workers.values())
    def create_worker(self, spec):
        self.creates.append(dict(spec))
        if self.fail: raise RuntimeError('create failed')
        row = {'runId': spec['runId'], 'backendId': 'runtime-' + spec['runId'], 'state': 'running', 'spec': dict(spec)}
        self.workers[spec['runId']] = row
        if self.ambiguous: self.ambiguous = False; raise RuntimeError('lost response after create')
        return row['backendId']
    def start_worker(self, run_id): self.workers[run_id]['state'] = 'running'
    def stop_worker(self, run_id): self.stops.append(run_id); self.workers[run_id]['state'] = 'failed'
    def remove_worker(self, run_id): self.removes.append(run_id); del self.workers[run_id]


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.clock = Clock(); self.runtime = Runtime()
        self.config = FleetConfig.from_dict({
            'fleet_id': 'test', 'state_dir': self.root / 'state', 'artifacts_dir': self.root / 'artifacts',
            'workspace_dir': self.root / 'work', 'accept_eula': True, 'accept_offline': True,
            'max_workers': 3, 'stagger_seconds': 5, 'cpu_budget': 12, 'memory_budget_mb': 18432,
            'storage_budget_mb': 24576, 'startup_timeout': 30, 'health_timeout': 10,
            'unhealthy_grace_seconds': 3, 'failure_backoff_seconds': 5, 'circuit_failures': 2,
            'circuit_pause_seconds': 20,
        })
        self.fleet = Fleet(self.config, self.runtime, clock=self.clock)
        self.addCleanup(lambda: self.fleet.close() if self.fleet else None)
        self.capacity = patch.object(self.fleet, '_capacity', return_value={'limit': 3, 'pressure': None})
        self.capacity.start(); self.addCleanup(self.capacity.stop)

    def health(self, run_id, **extra):
        root = self.config.artifacts_dir / run_id
        root.mkdir(parents=True, exist_ok=True)
        data = {'runId': run_id, 'phase': 'running', 'wall': self.clock(), 'heartbeatAge': 0,
                'captureAge': 0, 'serverHeartbeatAge': 0, 'clientTps': 20, 'serverTps': 20,
                'worldReady': True, 'warmingUp': False, 'capacityReady': True}
        data.update(extra)
        atomic_json(root / 'health.json', data)

    def finalized(self, run_id, passed=True):
        root = self.config.artifacts_dir / run_id
        root.mkdir(parents=True, exist_ok=True)
        result = {'runId': run_id, 'diagnosticPassed': passed, 'reason': 'client_finished' if passed else 'crash'}
        atomic_json(root / 'result.json', result)
        atomic_json(root / 'manifest.json', artifact_manifest(root, result))
        self.runtime.workers[run_id]['state'] = 'completed' if passed else 'failed'

    def test_startup_stagger_and_healthy_requires_telemetry(self):
        self.fleet.set_target(2)
        first = self.fleet.reconcile()
        self.assertEqual(first['counts']['starting'], 1)
        self.assertEqual(first['counts']['healthy'], 0)
        self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 1)
        self.health(first['workers'][0]['runId'])
        self.clock.advance(5)
        second = self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 2)
        self.assertEqual(second['counts']['healthy'], 1)
        self.assertNotEqual(self.runtime.creates[0]['runId'], self.runtime.creates[1]['runId'])

    def test_recovery_adopts_lost_create_response_without_duplicate(self):
        self.runtime.ambiguous = True
        self.fleet.set_target(1)
        self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 1)
        self.fleet.close(); self.fleet = None
        restarted = Fleet(self.config, self.runtime, clock=self.clock)
        self.fleet = restarted
        with patch.object(restarted, '_capacity', return_value={'limit': 3, 'pressure': None}):
            result = restarted.reconcile()
        self.assertEqual(len(self.runtime.creates), 1)
        self.assertEqual(result['counts']['active'], 1)
        self.assertEqual(result['desired'], 1)

    def test_loading_and_warmup_never_count_as_healthy_or_trigger_timing_failure(self):
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.health(run_id, worldReady=False, warmingUp=True, capacityReady=False, clientTps=2, serverTps=10)
        result = self.fleet.reconcile()
        self.assertEqual(result['counts']['starting'], 1)
        self.assertEqual(result['counts']['healthy'], 0)
        self.assertEqual(self.runtime.stops, [])
        self.health(run_id, worldReady=True, warmingUp=False, capacityReady=True)
        self.assertEqual(self.fleet.reconcile()['counts']['healthy'], 1)

    def test_adopted_terminal_runtime_is_not_completed_without_evidence(self):
        spec = {'runId': 'adopted-001', 'task': 'director', 'seed': 1, 'seconds': 30}
        self.runtime.workers['adopted-001'] = {'runId': 'adopted-001', 'backendId': 'existing',
            'state': 'completed', 'spec': spec}
        result = self.fleet.reconcile()
        self.assertEqual(result['counts']['completed'], 0)
        self.assertEqual(result['workers'][0]['state'], 'failed')

    def test_terminal_failure_replaced_with_fresh_id_and_backoff(self):
        self.fleet.set_target(1)
        old = self.fleet.reconcile()['workers'][0]['runId']
        self.runtime.workers[old]['state'] = 'failed'
        result = self.fleet.reconcile()
        self.assertEqual(result['pausedReason'], 'failure_backoff')
        self.assertEqual(len(self.runtime.creates), 1)
        self.clock.advance(5)
        self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 2)
        self.assertNotEqual(self.runtime.creates[-1]['runId'], old)
        self.assertTrue((self.config.workspace_dir).exists())

    def test_stale_health_stops_only_after_grace(self):
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.health(run_id)
        self.fleet.reconcile()
        self.clock.advance(11)
        self.fleet.reconcile()
        self.assertEqual(self.runtime.stops, [])
        self.clock.advance(3)
        self.fleet.reconcile()
        self.assertEqual(self.runtime.stops, [run_id])

    def test_missing_or_slow_game_ticks_never_count_as_healthy(self):
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.health(run_id, serverTps=None)
        self.assertEqual(self.fleet.reconcile()['counts']['healthy'], 0)
        self.health(run_id, serverTps=10)
        self.assertEqual(self.fleet.reconcile()['counts']['healthy'], 0)
        self.health(run_id)
        self.assertEqual(self.fleet.reconcile()['counts']['healthy'], 1)
        self.clock.advance(11)
        self.assertEqual(self.fleet.snapshot()['counts']['healthy'], 0)

    def test_target_zero_stops_existing_workers_without_replacement(self):
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.fleet.set_target(0)
        self.fleet.reconcile(); self.fleet.reconcile()
        self.assertEqual(self.runtime.stops, [run_id])
        self.assertEqual(len(self.runtime.creates), 1)
        self.assertEqual(self.fleet.snapshot()['workers'][0]['state'], 'cancelled')

    def test_operator_stop_is_idempotent_and_does_not_stop_other_workers(self):
        self.fleet.set_target(2)
        first = self.fleet.reconcile()['workers'][0]['runId']
        self.clock.advance(5)
        self.fleet.reconcile()
        self.fleet.stop_worker(first)
        self.fleet.stop_worker(first)
        self.assertEqual(self.fleet.snapshot()['desired'], 1)
        self.fleet.reconcile()
        self.assertEqual(self.runtime.stops, [first])

    def test_negative_seed_is_preserved_for_first_assignment(self):
        self.fleet.config.seed = -42
        self.fleet.set_target(1)
        self.fleet.reconcile()
        self.assertEqual(self.runtime.creates[0]['seed'], -42)

    def test_external_storage_backpressure_blocks_start_but_not_monitoring(self):
        self.fleet.set_target(1)
        self.fleet.set_inhibited('storage_backpressure')
        self.assertEqual(self.fleet.reconcile()['pausedReason'], 'storage_backpressure')
        self.assertEqual(self.runtime.creates, [])
        self.fleet.set_inhibited(None)
        self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 1)

    def test_failed_creates_open_circuit_without_reusing_dirty_ids(self):
        self.runtime.fail = True
        self.fleet.set_target(1)
        first = self.fleet.reconcile()['workers'][0]['runId']
        self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 1)
        self.clock.advance(5)
        result = self.fleet.reconcile()
        self.assertEqual(result['pausedReason'], None)
        self.clock.advance(5)
        result = self.fleet.reconcile()
        self.assertEqual(result['pausedReason'], 'failure_circuit_open')
        self.assertEqual(len(self.runtime.creates), 2)
        self.assertNotEqual(self.runtime.creates[-1]['runId'], first)

    def test_completed_runtime_cleanup_requires_verified_artifacts(self):
        self.config.retained_workers = 0
        self.fleet.config.retained_workers = 0
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.runtime.workers[run_id]['state'] = 'failed'
        self.fleet.set_target(0)
        self.fleet.reconcile()
        self.assertEqual(self.runtime.removes, [])
        self.finalized(run_id)
        self.fleet.reconcile()
        self.assertEqual(self.runtime.removes, [run_id])

    def test_second_process_cannot_reconcile_while_lease_held(self):
        other = Fleet(self.config, self.runtime, clock=self.clock)
        self.addCleanup(other.close)
        import fcntl
        fcntl.flock(self.fleet._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: result = other.reconcile()
        finally: fcntl.flock(self.fleet._lock_file.fileno(), fcntl.LOCK_UN)
        self.assertEqual(result['pausedReason'], 'another_reconciler_active')
        self.assertEqual(self.runtime.creates, [])

    def test_configuration_rejects_unsafe_identity_and_bad_limits(self):
        for data in ({'fleet_id': '../bad'}, {'cpus': float('nan')}, {'desired': 2, 'max_workers': 1},
                     {'accept_eula': 'false'}, {'world_ids': ['../bad'], 'world_bank': '/bank'}):
            with self.subTest(data=data), self.assertRaises(ValueError): FleetConfig.from_dict(data)


if __name__ == '__main__': unittest.main()
