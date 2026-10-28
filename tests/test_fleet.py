import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from conductor.fleet import Fleet, FleetConfig
from conductor.coverage import NoEligibleTask
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

    def test_scheduler_cooldown_pauses_admission_but_monitors_existing_workers(self):
        self.fleet.set_target(2)
        first = self.fleet.reconcile()['workers'][0]['runId']
        self.health(first)
        deadline = self.clock() + 90
        scheduler = SimpleNamespace(choose=lambda *args, **kwargs: None, snapshot=lambda: {})
        self.fleet.scheduler = scheduler
        self.clock.advance(5)
        self.health(first)
        with patch.object(scheduler, 'choose', side_effect=NoEligibleTask('cooldown', retry_at=deadline)):
            state = self.fleet.reconcile()
            self.assertEqual(state['desired'], 2)
            self.assertEqual(state['counts']['healthy'], 1)
            self.assertEqual(state['pausedReason'], 'no_eligible_task')
            self.assertEqual(state['nextEligibleWall'], deadline)
            self.assertEqual(len(self.runtime.creates), 1)
            self.assertEqual(int(self.fleet._setting('consecutive_failures')), 0)
            self.fleet.resume()
            self.assertEqual(self.fleet.reconcile()['pausedReason'], 'no_eligible_task')
        self.clock.advance(90)
        self.health(first)
        with patch.object(scheduler, 'choose', return_value={'task': '@get dirt 1', 'seed': 1, 'seconds': 60}):
            state = self.fleet.reconcile()
        self.assertIsNone(state['pausedReason'])
        self.assertIsNone(state['nextEligibleWall'])
        self.assertEqual(len(self.runtime.creates), 2)

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

    def test_rolling_game_cadence_prevents_raw_sample_flapping(self):
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.health(run_id, clientTps=16, serverTps=16, clientMeanTps=20, serverMeanTps=20,
                    clientMeanTpsSamples=10, serverMeanTpsSamples=10)
        self.assertEqual(self.fleet.reconcile()['counts']['healthy'], 1)
        self.health(run_id, clientTps=20, serverTps=20, clientMeanTps=16, serverMeanTps=20,
                    clientMeanTpsSamples=10, serverMeanTpsSamples=10)
        self.assertEqual(self.fleet.reconcile()['counts']['healthy'], 0)
        self.health(run_id, clientMeanTps=None, serverMeanTps=20,
                    clientMeanTpsSamples=9, serverMeanTpsSamples=10)
        self.assertEqual(self.fleet.reconcile()['counts']['healthy'], 0)

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

    def test_delivered_central_evidence_survives_spool_retention_and_allows_cleanup(self):
        self.fleet.config.central_dir = self.root / 'central'
        self.fleet.config.central_dir.mkdir()
        self.fleet.config.retained_workers = 0
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.finalized(run_id)
        shutil.move(str(self.config.artifacts_dir / run_id), str(self.fleet.config.central_dir / run_id))
        self.fleet.set_target(0)
        result = self.fleet.reconcile()
        self.assertEqual(result['workers'][0]['state'], 'completed')
        self.assertTrue(result['workers'][0]['artifactsVerified'])
        self.assertEqual(self.runtime.removes, [run_id])
        self.assertTrue((self.fleet.config.central_dir / run_id / 'manifest.json').is_file())
        ingested = []
        self.fleet.scheduler = SimpleNamespace(ingest=lambda path: ingested.append(path), snapshot=lambda: {})
        self.fleet.reconcile()
        self.assertEqual(ingested, [self.fleet.config.central_dir / run_id])

    def test_second_process_cannot_reconcile_while_lease_held(self):
        other = Fleet(self.config, self.runtime, clock=self.clock)
        self.addCleanup(other.close)
        import fcntl
        fcntl.flock(self.fleet._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: result = other.reconcile()
        finally: fcntl.flock(self.fleet._lock_file.fileno(), fcntl.LOCK_UN)
        self.assertEqual(result['pausedReason'], 'another_reconciler_active')
        self.assertEqual(self.runtime.creates, [])

    def test_missing_kubernetes_pod_reserves_capacity_until_evidence_goes_quiet(self):
        self.fleet.config.backend = 'kubernetes'
        self.fleet.set_target(1)
        run_id = self.fleet.reconcile()['workers'][0]['runId']
        self.health(run_id)
        del self.runtime.workers[run_id]
        result = self.fleet.reconcile()
        self.assertEqual(result['workers'][0]['state'], 'missing')
        self.assertEqual(result['counts']['active'], 1)
        self.assertEqual(len(self.runtime.creates), 1)
        self.clock.advance(10)
        self.health(run_id)  # a still-alive orphan extends the reservation
        self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 1)
        self.clock.advance(12)
        self.fleet.reconcile()
        self.assertEqual(self.fleet.snapshot()['workers'][0]['state'], 'missing')
        self.clock.advance(1)
        result = self.fleet.reconcile()
        self.assertEqual(result['workers'][0]['state'], 'lost')
        self.assertEqual(len(self.runtime.creates), 1)
        self.clock.advance(5)
        self.fleet.reconcile()
        self.assertEqual(len(self.runtime.creates), 2)

    def test_scheduler_cannot_silently_grant_invalid_operator_permissions(self):
        self.fleet.scheduler = SimpleNamespace(
            choose=lambda *a, **k: {'task': 'director', 'seed': 1, 'seconds': 30, 'operator': 'false'},
            snapshot=lambda: {})
        self.fleet.set_target(1)
        with self.assertRaisesRegex(ValueError, 'operator must be boolean'): self.fleet.reconcile()
        self.assertEqual(self.runtime.creates, [])
        self.assertEqual(self.fleet.snapshot()['workers'], [])

    def test_requested_fleet_larger_than_budget_starts_only_what_fits(self):
        self.fleet.set_target(3)
        with patch.object(self.fleet, '_capacity', return_value={'limit': 1, 'pressure': None}):
            first = self.fleet.reconcile()
            self.assertEqual(first['pausedReason'], 'resource_capacity_limit')
            self.assertEqual(len(self.runtime.creates), 1)
            self.clock.advance(5)
            self.fleet.reconcile()
            self.assertEqual(len(self.runtime.creates), 1)
            self.runtime.workers[first['workers'][0]['runId']]['state'] = 'failed'
            self.assertEqual(self.fleet.reconcile()['pausedReason'], 'failure_backoff')
            self.assertEqual(len(self.runtime.creates), 1)

    def test_operator_resume_clears_failures_without_bypassing_other_gates(self):
        with self.fleet.db:
            self.fleet._set('circuit_until', self.clock() + 100)
            self.fleet._set('next_start', self.clock() + 100)
            self.fleet._set('consecutive_failures', 5)
        self.fleet.resume()
        self.assertEqual(self.fleet.snapshot()['desired'], 0)
        self.assertEqual(self.fleet.snapshot()['circuitUntil'], 0)
        self.assertEqual(self.fleet._setting('consecutive_failures'), '0')
        self.fleet.reconcile()
        self.assertEqual(self.runtime.creates, [])
        self.fleet.set_target(1)
        self.fleet.set_inhibited('storage_backpressure')
        self.fleet.reset_backoff()
        self.assertEqual(self.fleet.reconcile()['pausedReason'], 'storage_backpressure')
        self.assertEqual(self.runtime.creates, [])
        self.fleet.set_inhibited(None)
        with patch.object(self.fleet, '_capacity', return_value={'limit': 0, 'pressure': 'host_memory_pressure'}):
            self.fleet.resume()
            self.assertEqual(self.fleet.reconcile()['pausedReason'], 'host_memory_pressure')
        self.assertEqual(self.runtime.creates, [])
        self.assertTrue(any(e['kind'] == 'backoff_reset' for e in self.fleet.snapshot()['events']))

    def test_persisted_scope_rejects_backend_and_storage_changes_but_allows_profiles(self):
        for change in ({'backend': 'kubernetes'}, {'artifacts_dir': self.root / 'different-artifacts'},
                       {'workspace_dir': self.root / 'different-workspaces'}):
            config = FleetConfig.from_dict({**self.config.__dict__, **change})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'runtime scope changed'):
                Fleet(config, self.runtime, clock=self.clock)
        config = FleetConfig.from_dict({**self.config.__dict__, 'image': 'different:tag', 'cpus': 4, 'seed': 99})
        other = Fleet(config, self.runtime, clock=self.clock)
        other.close()

    def test_kubernetes_scope_records_namespace_context_and_known_cluster_endpoint(self):
        state = self.root / 'kube-state'
        base = {**self.config.__dict__, 'backend': 'kubernetes', 'state_dir': state,
                'cluster_endpoint': 'https://example.invalid:6443', 'kube_context': 'test-cluster'}
        first = Fleet(FleetConfig.from_dict(base), self.runtime, clock=self.clock)
        first.close()
        for change in ({'namespace': 'other'}, {'kube_context': 'another'}, {'cluster_endpoint': 'https://different.invalid:6443'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'runtime scope changed'):
                Fleet(FleetConfig.from_dict({**base, **change}), self.runtime, clock=self.clock)

    def test_configuration_rejects_unsafe_identity_and_bad_limits(self):
        for data in ({'fleet_id': '../bad'}, {'cpus': float('nan')}, {'desired': 2, 'max_workers': 1},
                     {'accept_eula': 'false'}, {'world_ids': ['../bad'], 'world_bank': '/bank'}):
            with self.subTest(data=data), self.assertRaises(ValueError): FleetConfig.from_dict(data)


if __name__ == '__main__': unittest.main()
