"""Durable desired-state reconciliation for independently recorded Minecraft workers."""
from __future__ import annotations
import fcntl
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import subprocess
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from .backends import BackendError, DockerBackend, KubernetesBackend, worker_env
from .storage import verify_manifest
from .coverage import NoEligibleTask

ACTIVE = {'pending', 'created', 'starting', 'running', 'unhealthy', 'stopping', 'missing'}
TERMINAL = {'completed', 'failed', 'lost', 'cancelled'}


def _positive(value, name, low=0, high=10**12):
    if isinstance(value, bool): raise ValueError(f'{name} must be numeric')
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high: raise ValueError(f'Invalid {name}')
    return value


@dataclass
class FleetConfig:
    fleet_id: str = 'default'
    backend: str = 'docker'
    state_dir: Path = Path('.state/fleet')
    artifacts_dir: Path = Path('.state/artifacts')
    central_dir: Path | None = None
    workspace_dir: Path = Path('.state/workspaces')
    world_bank: Path | None = None
    world_ids: list[str] = field(default_factory=list)
    image: str = 'vibrato-worker:dev'
    desired: int = 0
    max_workers: int = 1  # zero disables the worker-count ceiling
    cpus: float = 8  # zero disables CPU quota and CPU admission/pressure gates
    memory_mb: int = 6144  # zero disables memory quota and memory admission/pressure gates
    storage_mb: int = 8192
    cpu_budget: float = 0
    memory_budget_mb: int = 0
    storage_budget_mb: int = 0
    free_disk_reserve_mb: int = 1024
    stagger_seconds: float = 8
    startup_timeout: float = 360
    health_timeout: float = 45
    unhealthy_grace_seconds: float = 30
    failure_backoff_seconds: float = 15
    max_failure_backoff_seconds: float = 300
    circuit_failures: int = 5
    circuit_pause_seconds: float = 300
    reconcile_seconds: float = 5
    retained_workers: int = 20
    event_limit: int = 500
    seconds: float = 300
    task: str = 'director'
    seed: int = 424242
    mode: str = 'diagnostic'
    perf: str = 'normal'
    allow_world_copy: bool = False
    accept_eula: bool = False
    accept_offline: bool = False
    worker_uid: int = field(default_factory=os.getuid)
    worker_gid: int = field(default_factory=os.getgid)
    worker_env: dict[str, str] = field(default_factory=dict)
    namespace: str = 'vibrato'
    kubectl: str = 'kubectl'
    kubeconfig: Path | None = None
    kube_context: str | None = None
    cluster_endpoint: str | None = None
    artifacts_pvc: str = 'vibrato-artifacts'
    central_pvc: str | None = None
    world_bank_pvc: str | None = None

    @classmethod
    def from_dict(cls, value):
        aliases = {'fleetId': 'fleet_id', 'state': 'state_dir', 'artifacts': 'artifacts_dir',
                   'worldBank': 'world_bank', 'centralDir': 'central_dir', 'target': 'desired', 'kubectl_path': 'kubectl',
                   'memory': 'memory_mb'}
        names = {v.name for v in fields(cls)}
        data = {aliases.get(key, key): val for key, val in dict(value).items()}
        unknown = set(data) - names
        if unknown: raise ValueError('Unknown fleet settings: ' + ', '.join(sorted(unknown)))
        for name in ('state_dir', 'artifacts_dir', 'central_dir', 'workspace_dir', 'world_bank', 'kubeconfig'):
            if data.get(name) is not None: data[name] = Path(data[name]).expanduser().resolve()
        config = cls(**data)
        for name in ('state_dir', 'artifacts_dir', 'workspace_dir'):
            setattr(config, name, Path(getattr(config, name)).resolve())
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', config.fleet_id): raise ValueError('Invalid fleet_id')
        if config.backend not in ('docker', 'kubernetes'): raise ValueError('Unknown backend')
        desired = config.desired
        if isinstance(desired, bool) or not isinstance(desired, int) or desired < 0:
            raise ValueError('desired must be a nonnegative integer')
        for name in ('max_workers', 'memory_mb', 'storage_mb', 'memory_budget_mb',
                     'storage_budget_mb', 'free_disk_reserve_mb', 'circuit_failures', 'retained_workers',
                     'event_limit', 'worker_uid', 'worker_gid'):
            raw = getattr(config, name)
            if int(_positive(raw, name)) != float(raw): raise ValueError(f'{name} must be an integer')
            setattr(config, name, int(raw))
        for name in ('cpus', 'cpu_budget', 'stagger_seconds', 'startup_timeout', 'health_timeout',
                     'unhealthy_grace_seconds', 'failure_backoff_seconds', 'max_failure_backoff_seconds',
                     'circuit_pause_seconds', 'reconcile_seconds', 'seconds'):
            setattr(config, name, _positive(getattr(config, name), name))
        if min(config.storage_mb, config.startup_timeout,
               config.health_timeout, config.reconcile_seconds, config.circuit_failures) <= 0:
            raise ValueError('Storage size, timeouts, and circuit_failures must be positive')
        if config.max_workers and config.desired > config.max_workers: raise ValueError('desired exceeds max_workers safety ceiling')
        if config.mode not in ('diagnostic', 'collection'): raise ValueError('Unknown mode')
        if config.perf not in ('normal', 'low'): raise ValueError('Unknown rendering profile')
        if not 10 <= config.seconds <= 86400: raise ValueError('seconds must be 10-86400')
        if config.task != 'director' and not re.fullmatch(r'@[a-z][a-z0-9_]*(?: [A-Za-z0-9_:.-]+)*', config.task):
            raise ValueError('Invalid task command')
        if isinstance(config.seed, bool) or not -(2**63) <= int(config.seed) < 2**63: raise ValueError('Invalid seed')
        config.seed = int(config.seed)
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', str(v)) for v in config.world_ids):
            raise ValueError('Invalid world id')
        if not isinstance(config.worker_env, dict) or any(not re.fullmatch(r'[A-Z][A-Z0-9_]*', k) for k in config.worker_env):
            raise ValueError('Invalid worker_env keys')
        config.worker_env = {k: str(v) for k, v in config.worker_env.items()}
        for name in ('accept_eula', 'accept_offline', 'allow_world_copy'):
            if not isinstance(getattr(config, name), bool): raise ValueError(f'{name} must be boolean')
        if config.world_ids and not (config.world_bank or config.world_bank_pvc):
            raise ValueError('world_ids require a world bank')
        if config.backend == 'kubernetes':
            for name in ('namespace', 'artifacts_pvc', 'central_pvc', 'world_bank_pvc'):
                if getattr(config, name) is None and name in ('central_pvc', 'world_bank_pvc'): continue
                if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', getattr(config, name)):
                    raise ValueError(f'Invalid Kubernetes {name}')
        return config

    def runtime_scope(self):
        scope = {'version': 1, 'backend': self.backend, 'artifactsRoot': str(self.artifacts_dir)}
        if self.backend == 'docker':
            scope['workspaceRoot'] = str(self.workspace_dir)
            scope['dockerHost'] = os.environ.get('DOCKER_HOST')
            scope['dockerContext'] = os.environ.get('DOCKER_CONTEXT')
            return scope
        endpoint = self.cluster_endpoint
        host = os.environ.get('KUBERNETES_SERVICE_HOST')
        if endpoint is None and host and self.kubeconfig is None and self.kube_context is None:
            port = os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', os.environ.get('KUBERNETES_SERVICE_PORT', '443'))
            endpoint = f'https://{host if ":" not in host else "[" + host + "]"}:{port}'
        if endpoint is None:
            argv = [self.kubectl]
            if self.kubeconfig: argv += ['--kubeconfig', str(self.kubeconfig)]
            if self.kube_context: argv += ['--context', self.kube_context]
            # JSONPath prints only the server URL; never extract credentials or certificate data.
            argv += ['config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}']
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                if result.returncode == 0: endpoint = result.stdout.strip() or None
            except (OSError, subprocess.TimeoutExpired): pass
        if endpoint is not None:
            parsed = urlsplit(endpoint)
            if parsed.scheme not in ('https', 'http') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Invalid Kubernetes cluster_endpoint')
            endpoint = endpoint.rstrip('/')
        scope.update(namespace=self.namespace, kubeContext=self.kube_context, clusterEndpoint=endpoint)
        if endpoint is None: scope['kubeconfigSource'] = str(self.kubeconfig) if self.kubeconfig else None
        return scope

    def public(self):
        return {name: (str(getattr(self, name)) if isinstance(getattr(self, name), Path) else getattr(self, name))
                for name in ('fleet_id', 'backend', 'image', 'max_workers', 'cpus', 'memory_mb', 'storage_mb',
                             'stagger_seconds', 'startup_timeout', 'health_timeout', 'seconds', 'mode', 'perf',
                             'namespace', 'artifacts_dir', 'central_dir', 'world_ids')}


class Fleet:
    def __init__(self, config, backend=None, scheduler=None, clock=time.time):
        self.config = config if isinstance(config, FleetConfig) else FleetConfig.from_dict(config)
        self.config = FleetConfig.from_dict({v.name: getattr(self.config, v.name) for v in fields(FleetConfig)})
        self.clock = clock
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._lock_file = (self.config.state_dir / 'reconciler.lock').open('a+')
        self.db = sqlite3.connect(self.config.state_dir / 'fleet.sqlite3', check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workers(
                run_id TEXT PRIMARY KEY, backend_id TEXT, state TEXT NOT NULL, spec TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, reason TEXT, unhealthy_since REAL,
                finalized INTEGER NOT NULL DEFAULT 0, ingested INTEGER NOT NULL DEFAULT 0,
                removed INTEGER NOT NULL DEFAULT 0, health TEXT, result TEXT, failure_counted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, wall REAL NOT NULL,
                kind TEXT NOT NULL, run_id TEXT, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS startup_milestones(
                run_id TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(run_id, kind));
        ''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('desired', str(self.config.desired)))
            for key in ('last_start', 'next_start', 'consecutive_failures', 'circuit_until'):
                self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, '0'))
            self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('fleet_id', self.config.fleet_id))
        if self._setting('fleet_id') != self.config.fleet_id:
            self.db.close(); self._lock_file.close()
            raise ValueError('State directory belongs to a different fleet')
        scope = self.config.runtime_scope()
        with self.db:
            created_scope = self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',
                ('runtime_scope', json.dumps(scope, sort_keys=True))).rowcount
            if created_scope:
                self._event('scope_bound', 'Bound state to backend and artifact storage identity; earlier legacy settings cannot be inferred')
            prior_scope = self.db.execute("SELECT value FROM settings WHERE key='runtime_scope'").fetchone()[0]
        old = json.loads(prior_scope)
        if old != scope:
            changed = ', '.join(sorted(k for k in set(old) | set(scope) if old.get(k) != scope.get(k)))
            self.db.close(); self._lock_file.close()
            raise ValueError('State runtime scope changed (' + changed + '); drain the old fleet and use a new state directory or an explicit migration')
        self.config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.backend = backend or (DockerBackend(self.config) if self.config.backend == 'docker' else KubernetesBackend(self.config))
        self.scheduler = scheduler
        self._paused_reason = None
        self._next_eligible_wall = None
        self._last_capacity = None
        self._external_inhibition = None
        self._startup_milestones = set(map(tuple, self.db.execute('SELECT run_id,kind FROM startup_milestones')))
        self._last_reported_pause = None

    def _milestone(self, run_id, kind, details):
        key = (run_id, kind)
        if key in self._startup_milestones: return
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO startup_milestones VALUES (?,?)', key).rowcount
            if inserted: self._event(kind, json.dumps(details, sort_keys=True), run_id)
        self._startup_milestones.add(key)

    def _report_pause(self):
        if self._paused_reason != self._last_reported_pause:
            with self.db: self._event('admission_pause_changed', json.dumps({'reason': self._paused_reason}))
            self._last_reported_pause = self._paused_reason

    def _setting(self, key): return self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]
    def _set(self, key, value): self.db.execute('UPDATE settings SET value=? WHERE key=?', (str(value), key))
    def _event(self, kind, message, run_id=None):
        self.db.execute('INSERT INTO events(wall,kind,run_id,message) VALUES (?,?,?,?)', (self.clock(), kind, run_id, str(message)))
        self.db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)', (self.config.event_limit,))

    def close(self):
        with self._thread_lock:
            self.db.close()
            self._lock_file.close()

    def set_inhibited(self, reason=None):
        with self._thread_lock:
            self._external_inhibition = str(reason)[:500] if reason else None

    def set_target(self, n):
        if isinstance(n, bool) or not isinstance(n, int) or n < 0 or (self.config.max_workers and n > self.config.max_workers):
            ceiling = f' between 0 and {self.config.max_workers}' if self.config.max_workers else ' greater than or equal to zero'
            raise ValueError('Target must be an integer' + ceiling)
        if n and not (self.config.accept_eula and self.config.accept_offline):
            raise ValueError('Fleet launch requires accept_eula and accept_offline')
        with self._thread_lock, self.db:
            self._set('desired', int(n))
            self._event('target', f'Desired worker count set to {n}')
        return self.snapshot()

    def resume(self):
        """Audited operator retry after a diagnosis; never changes desired count or resource gates."""
        with self._thread_lock, self.db:
            self._set('circuit_until', 0)
            self._set('next_start', 0)
            self._set('consecutive_failures', 0)
            if self._paused_reason in ('failure_backoff', 'failure_circuit_open'):
                self._paused_reason = None
            self._event('backoff_reset', 'Operator reset the failure circuit and retry backoff')
        return self.snapshot()

    def reset_backoff(self):
        return self.resume()

    def stop_worker(self, run_id, replace=False):
        # Persist intent; the reconciler serializes potentially slow backend operations.
        with self._thread_lock, self.db:
            row = self.db.execute('SELECT * FROM workers WHERE run_id=?', (run_id,)).fetchone()
            if row is None: raise KeyError(run_id)
            if row['state'] not in ACTIVE or row['state'] == 'stopping': return self.snapshot()
            if not replace: self._set('desired', max(0, int(self._setting('desired')) - 1))
            reason = 'operator_replace' if replace else 'operator_stop'
            self.db.execute('UPDATE workers SET state=?,reason=?,updated=? WHERE run_id=?',
                            ('stopping', reason, self.clock(), run_id))
            self._event(reason, 'Worker replacement requested' if replace else 'Worker stop requested', run_id)
        return self.snapshot()

    def _capacity(self):
        config = self.config
        cpus = (config.cpu_budget or max(1, (os.cpu_count() or 1) - 2)) if config.cpus else 0
        memory = config.memory_budget_mb if config.memory_mb else 0
        available = None
        try:
            values = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
            available = values.get('MemAvailable', 0) / 1024
            if config.memory_mb and not memory: memory = max(0, values.get('MemTotal', 0) / 1024 - 2048)
        except (OSError, ValueError):
            if config.memory_mb and not memory: memory = config.memory_mb * config.max_workers
        disk_root = config.artifacts_dir if config.backend == 'kubernetes' else config.workspace_dir
        disk_free = shutil.disk_usage(disk_root).free // 1024**2
        # Kubernetes temporary storage is admitted by the scheduler, not the controller's state PVC.
        storage = config.storage_budget_mb or (config.storage_mb * config.max_workers if config.backend == 'kubernetes' and config.max_workers
                                               else max(0, disk_free - config.free_disk_reserve_mb))
        slots = [int(storage // config.storage_mb)]
        if config.max_workers: slots.append(config.max_workers)
        if config.cpus: slots.append(int(cpus // config.cpus))
        if config.memory_mb: slots.append(int(memory // config.memory_mb))
        count = max(0, min(slots))
        pressure = None
        if config.memory_mb and available is not None and available < config.memory_mb + 1024: pressure = 'host_memory_pressure'
        disk_needed = min(config.storage_mb, 1024) if config.backend == 'kubernetes' else config.storage_mb
        if disk_free < disk_needed + config.free_disk_reserve_mb: pressure = 'host_storage_pressure'
        # CPU pressure requires sustained scheduling stalls, not a load-average guess.
        try:
            cpu_pressure = float(Path('/proc/pressure/cpu').read_text().split('avg10=')[1].split()[0])
            if config.cpus and cpu_pressure > 50: pressure = 'host_cpu_pressure'
        except (OSError, ValueError, IndexError): cpu_pressure = None
        return {'limit': count, 'cpuBudget': cpus, 'memoryBudgetMb': memory, 'storageBudgetMb': storage,
                'freeDiskMb': disk_free, 'availableMemoryMb': available, 'cpuPressure': cpu_pressure,
                'pressure': pressure, 'ceiling': config.max_workers}

    def _read_health(self, run_id):
        try:
            path = self.config.artifacts_dir / run_id / 'health.json'
            data = json.loads(path.read_text())
            if data.get('runId') != run_id: return None
            if (not isinstance(data.get('wall'), (float, int)) or not math.isfinite(data['wall'])
                    or not 0 <= data['wall'] <= self.clock() + self.config.health_timeout): return None
            return data
        except (OSError, ValueError, TypeError): return None

    @staticmethod
    def _warming_up(health):
        if not health: return True
        sample_keys = ('clientMeanTpsSamples', 'serverMeanTpsSamples')
        sampling = any(key in health for key in sample_keys) and any(
            not isinstance(health.get(key), (int, float)) or not math.isfinite(health[key]) or health[key] < 10
            for key in sample_keys)
        return health.get('warmingUp') is True or health.get('worldReady') is not True or sampling

    def _health_problem(self, row, backend, health, now):
        age = now - row['created']
        spec = json.loads(row['spec'])
        if age > spec.get('seconds', self.config.seconds) + self.config.startup_timeout * 4 + 120:
            return 'worker_absolute_lifetime_limit'
        if backend['state'] == 'unhealthy': return backend.get('reason') or 'runtime_unhealthy'
        if health is None:
            return 'missing_worker_health' if age > self.config.startup_timeout else None
        if now - health['wall'] > self.config.health_timeout: return 'stale_worker_health'
        phase = health.get('phase')
        classification = health.get('classification') or {}
        warming = self._warming_up(health)
        if phase == 'unhealthy':
            if not (warming and classification.get('kind') == 'overload' and classification.get('confidence') == 'inferred'):
                return classification.get('reason') or 'worker_unhealthy'
        if phase == 'failed': return classification.get('reason') or 'worker_failed'
        if phase == 'starting': return 'worker_startup_timeout' if age > self.config.startup_timeout else None
        if phase in ('stopping', 'completed'): return None
        if warming and age > self.config.startup_timeout: return 'worker_capacity_warmup_timeout'
        for key in ('heartbeatAge', 'captureAge', 'serverHeartbeatAge'):
            if (not isinstance(health.get(key), (int, float)) or not math.isfinite(health[key])
                    or health[key] < 0 or health[key] > self.config.health_timeout):
                return key + '_timeout' if age > self.config.startup_timeout else None
        if age > self.config.startup_timeout and any(
                not isinstance(health.get(key), (int, float)) or not math.isfinite(health[key])
                for key in ('clientTps', 'serverTps')):
            return 'missing_game_tick_health'
        if classification.get('kind') == 'overload' and classification.get('confidence') == 'confirmed':
            return classification.get('reason') or 'worker_overload'
        return None

    def _healthy(self, health, state):
        if state != 'running' or not health or health.get('phase') != 'running': return False
        if health.get('capacityReady') is not True or health.get('worldReady') is not True or health.get('warmingUp') is not False: return False
        wall = health.get('wall')
        if not isinstance(wall, (int, float)) or not math.isfinite(wall) or not 0 <= self.clock() - wall <= self.config.health_timeout:
            return False
        for key in ('heartbeatAge', 'captureAge', 'serverHeartbeatAge'):
            value = health.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= self.config.health_timeout: return False
        for raw, rolling in (('clientTps', 'clientMeanTps'), ('serverTps', 'serverMeanTps')):
            key = rolling if rolling in health else raw
            value = health.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 18: return False
            if key == rolling and health.get(rolling + 'Samples', 0) < 10: return False
        return not health.get('classification')

    def _cadence_pressure(self, row, now):
        # Only fresh, fully sampled rolling rates of running workers gate new starts.
        # Unhealthy/stopping workers retain their normal watchdog and replacement handling.
        if row['state'] != 'running': return False
        health = self._read_health(row['run_id'])
        if not health or now - health['wall'] > self.config.health_timeout: return False
        for side in ('client', 'server'):
            value = health.get(side + 'MeanTps')
            samples = health.get(side + 'MeanTpsSamples')
            if (isinstance(samples, (int, float)) and samples >= 10
                    and isinstance(value, (int, float)) and math.isfinite(value) and value < 18):
                return True
        return False

    def _artifact_roots(self, run_id):
        roots = [self.config.artifacts_dir / run_id]
        if self.config.central_dir is not None:
            roots.append(self.config.central_dir / run_id)
        return roots

    def _verify_result(self, row):
        roots = self._artifact_roots(row['run_id'])
        # Terminal evidence may already have been safely delivered and pruned from the spool.
        # Live telemetry continues to read only the worker's spool, never stale central files.
        for root in roots:
            try:
                result = verify_manifest(root)
                if result.get('runId') != row['run_id']: raise ValueError('Artifact run identity differs')
                return result
            except (OSError, ValueError, KeyError, TypeError): continue
        return None

    def _record_failure(self, row, reason, now):
        if row['failure_counted']: return
        if reason in ('operator_stop', 'operator_replace', 'scale_down'): return
        failures = int(self._setting('consecutive_failures')) + 1
        self._set('consecutive_failures', failures)
        delay = min(self.config.max_failure_backoff_seconds, self.config.failure_backoff_seconds * 2**min(failures - 1, 16))
        self._set('next_start', now + delay)
        if failures >= self.config.circuit_failures:
            self._set('circuit_until', now + self.config.circuit_pause_seconds)
            self._event('circuit_open', f'{failures} consecutive failures; pausing replacements for {self.config.circuit_pause_seconds:g}s')
        self.db.execute('UPDATE workers SET failure_counted=1 WHERE run_id=?', (row['run_id'],))

    def _adopt(self, value, now):
        run_id = value.get('runId')
        if not run_id or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', run_id):
            self._event('adoption_rejected', 'Owned backend worker lacks a valid run label')
            return
        spec = dict(value.get('spec') or {})
        spec.setdefault('runId', run_id)
        spec.setdefault('task', self.config.task)
        spec.setdefault('seed', self.config.seed)
        spec.setdefault('seconds', self.config.seconds)
        spec.setdefault('worldId', None)
        spec.setdefault('gamemode', 'survival')
        spec.setdefault('operator', False)
        spec.setdefault('operatorLevel', 2)
        self.db.execute('INSERT OR IGNORE INTO workers(run_id,backend_id,state,spec,created,updated) VALUES (?,?,?,?,?,?)',
                        (run_id, value['backendId'], 'pending' if value['state'] in ('completed', 'failed') else value['state'], json.dumps(spec), now, now))
        self._event('adopted', 'Recovered an owned worker from the runtime', run_id)

    def _new_spec(self, run_id):
        if self.scheduler:
            states = sorted(ACTIVE)
            placeholders = ','.join('?' for _ in states)
            pending = [row['run_id'] for row in self.db.execute(
                f'SELECT run_id FROM workers WHERE state IN ({placeholders}) ORDER BY created,run_id', states)]
            spec = dict(self.scheduler.choose(run_id, worlds=self.config.world_ids or None, pending_run_ids=pending))
            spec['worldId'] = spec.pop('world_id', spec.get('worldId'))
        else:
            number = self.db.execute('SELECT COUNT(*) FROM workers').fetchone()[0]
            spec = {'task': self.config.task, 'seconds': self.config.seconds,
                    'seed': ((self.config.seed + number + 2**63) % (2**64)) - 2**63,
                    'worldId': self.config.world_ids[number % len(self.config.world_ids)] if self.config.world_ids else None}
        spec.update(runId=run_id, mode=self.config.mode)
        spec.setdefault('seconds', self.config.seconds)
        spec.setdefault('seed', self.config.seed)
        spec.setdefault('task', self.config.task)
        spec.setdefault('gamemode', 'survival')
        spec.setdefault('operator', False)
        spec.setdefault('operatorLevel', 2)
        worker_env(self.config, spec)  # validate permissions before persisting or creating anything
        return spec

    def reconcile(self):
        with self._thread_lock:
            try: fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._paused_reason = 'another_reconciler_active'
                return self.snapshot()
            try: return self._reconcile_locked()
            finally: fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)

    def _reconcile_locked(self):
        now = self.clock()
        self._paused_reason = None
        self._next_eligible_wall = None
        try: discovered = self.backend.list_workers()
        except Exception as error:
            with self.db: self._event('backend_error', str(error))
            self._paused_reason = 'backend_unavailable'
            self._report_pause()
            return self.snapshot()
        backend = {v['runId']: v for v in discovered if v.get('runId')}
        with self.db:
            known = {r['run_id'] for r in self.db.execute('SELECT run_id FROM workers')}
            for value in discovered:
                if value.get('runId') not in known: self._adopt(value, now)
        rows = list(self.db.execute('SELECT * FROM workers ORDER BY created,run_id'))
        for row in rows:
            run_id, value = row['run_id'], backend.get(row['run_id'])
            if row['state'] in TERMINAL:
                if not row['finalized']:
                    result = self._verify_result(row)
                    if result is not None:
                        with self.db:
                            self.db.execute('UPDATE workers SET finalized=1,result=? WHERE run_id=?', (json.dumps(result), run_id))
                if self.scheduler and not row['ingested']:
                    errors = []
                    for root in self._artifact_roots(run_id):
                        if not (root / 'manifest.json').is_file(): continue
                        try:
                            self.scheduler.ingest(root)
                            with self.db: self.db.execute('UPDATE workers SET ingested=1 WHERE run_id=?', (run_id,))
                            break
                        except Exception as error: errors.append(str(error))
                    else:
                        if errors:
                            with self.db: self._event('coverage_error', '; '.join(errors), run_id)

                continue
            if value is None:
                if row['state'] == 'pending':
                    if self._external_inhibition: continue
                    if row['failure_counted']:
                        if now < max(float(self._setting('next_start')), float(self._setting('circuit_until'))): continue
                        with self.db:
                            self.db.execute('UPDATE workers SET state=?,reason=?,updated=? WHERE run_id=?',
                                            ('failed', 'create_failed', now, run_id))
                            self._event('failed', 'Create was not accepted by the runtime; retiring its identity', run_id)
                        continue
                    try:
                        backend_id = self.backend.create_worker(json.loads(row['spec']))
                        with self.db:
                            self.db.execute('UPDATE workers SET backend_id=?,state=?,updated=? WHERE run_id=?', (backend_id, 'starting', now, run_id))
                            self._event('started', 'Worker started from durable intent', run_id)
                    except Exception as error:
                        with self.db:
                            self._event('create_error', str(error), run_id)
                            self._record_failure(row, 'create_failed', now)
                            # Keep pending: the runtime may have accepted an ambiguous create request.
                    continue
                if self.config.backend == 'kubernetes':
                    # API absence after force deletion is not proof that the node already stopped a Pod.
                    # Reserve its capacity until durable heartbeats have gone stale plus a quiescence grace.
                    health = self._read_health(run_id)
                    missing_since = row['unhealthy_since'] if row['unhealthy_since'] is not None else now
                    last_health = health['wall'] if health else missing_since
                    quiet_after = max(missing_since + self.config.health_timeout,
                                      last_health + self.config.health_timeout + self.config.unhealthy_grace_seconds)
                    if now < quiet_after:
                        with self.db:
                            reason = row['reason'] if row['reason'] in ('operator_stop', 'operator_replace', 'scale_down') else 'runtime_worker_unconfirmed_missing'
                            self.db.execute('UPDATE workers SET state=?,reason=?,unhealthy_since=?,health=? WHERE run_id=?',
                                ('missing', reason, missing_since, json.dumps(health) if health else None, run_id))
                            if row['state'] != 'missing': self._event('runtime_missing', 'Runtime absent; reserving capacity until worker evidence is quiescent', run_id)
                        continue
                with self.db:
                    cancelled = row['reason'] in ('operator_stop', 'operator_replace', 'scale_down')
                    reason = row['reason'] if cancelled else 'runtime_worker_missing'
                    state = 'cancelled' if cancelled else 'lost'
                    self.db.execute('UPDATE workers SET state=?,updated=?,reason=? WHERE run_id=?', (state, now, reason, run_id))
                    self._event(state, reason, run_id)
                    self._record_failure(row, reason, now)

                continue
            health = self._read_health(run_id)
            if value['state'] == 'running':
                self._milestone(run_id, 'runtime_running_observed', {'backendState': value['state']})
            if health and 0 <= now - health['wall'] <= self.config.health_timeout:
                for side, age_key in (('client', 'heartbeatAge'), ('server', 'serverHeartbeatAge')):
                    age = health.get(age_key)
                    rate = health.get(side + 'MeanTps')
                    samples = health.get(side + 'MeanTpsSamples')
                    if (isinstance(age, (int, float)) and math.isfinite(age) and 0 <= age and age + now - health['wall'] <= self.config.health_timeout
                            and isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 18
                            and isinstance(samples, (int, float)) and math.isfinite(samples) and samples >= 10):
                        self._milestone(run_id, side + '_healthy_ticks_observed',
                                        {'healthWall': health['wall'], 'heartbeatAge': age,
                                         'meanTps': rate, 'samples': samples})
            with self.db:
                self.db.execute('UPDATE workers SET backend_id=?,health=?,updated=? WHERE run_id=?',
                                (value['backendId'], json.dumps(health) if health else None, now, run_id))
            if value['state'] in ('completed', 'failed'):
                result = self._verify_result(row)
                success = value['state'] == 'completed' and result is not None and result.get('diagnosticPassed') is True
                state = 'cancelled' if row['reason'] in ('operator_stop', 'operator_replace', 'scale_down') else 'completed' if success else 'failed'
                reason = row['reason'] or (result or {}).get('reason') or value.get('reason') or ('missing_final_manifest' if result is None else 'worker_failed')
                with self.db:
                    self.db.execute('UPDATE workers SET state=?,reason=?,result=?,finalized=?,updated=? WHERE run_id=?',
                                    (state, reason, json.dumps(result) if result is not None else None, int(result is not None), now, run_id))
                    self._event(state, reason, run_id)
                    if success:
                        self._set('consecutive_failures', 0)
                        self._set('next_start', 0)
                    else: self._record_failure(row, reason, now)
                continue
            if row['state'] == 'stopping':
                if value['state'] != 'stopping':
                    try: self.backend.stop_worker(run_id)
                    except Exception as error:
                        with self.db: self._event('stop_error', str(error), run_id)
                continue
            if value['state'] == 'created':
                try: self.backend.start_worker(run_id)
                except Exception as error:
                    with self.db: self._event('start_error', str(error), run_id)
            problem = self._health_problem(row, value, health, now)
            if problem:
                since = row['unhealthy_since'] if row['unhealthy_since'] is not None else now
                with self.db:
                    self.db.execute('UPDATE workers SET state=?,reason=?,unhealthy_since=? WHERE run_id=?', ('unhealthy', problem, since, run_id))
                # Confirmed worker watchdogs already made a contextual decision; stale health gets a short grace.
                confirmed = health and health.get('phase') in ('unhealthy', 'failed')
                if confirmed or now - since >= self.config.unhealthy_grace_seconds:
                    with self.db:
                        self.db.execute('UPDATE workers SET state=? WHERE run_id=?', ('stopping', run_id))
                        self._event('unhealthy', problem, run_id)
                    try: self.backend.stop_worker(run_id)
                    except Exception as error:
                        with self.db: self._event('stop_error', str(error), run_id)
            else:
                state = ('running' if health and health.get('phase') == 'running'
                         and not self._warming_up(health) else 'starting')
                if state == 'running' and row['state'] != 'running':
                    self._milestone(run_id, 'startup_ready_observed', {'healthWall': health.get('wall')})
                with self.db:
                    self.db.execute('UPDATE workers SET state=?,reason=NULL,unhealthy_since=NULL WHERE run_id=?', (state, run_id))
        self._last_capacity = self._capacity()
        desired = int(self._setting('desired'))
        active = list(self.db.execute("SELECT * FROM workers WHERE state IN ('pending','created','starting','running','unhealthy','stopping','missing') ORDER BY created DESC"))
        # Scale down first; workers requested to stop do not allow an immediate replacement until absent/terminal.
        remaining = [r for r in active if r['state'] not in ('stopping', 'missing')]
        excess = max(0, len(remaining) - min(desired, self._last_capacity['limit']))
        for row in remaining[:excess]:
            with self.db:
                self.db.execute('UPDATE workers SET state=?,reason=? WHERE run_id=?', ('stopping', 'scale_down', row['run_id']))
                self._event('scale_down', 'Stopping worker to meet desired count or resource budget', row['run_id'])
            try: self.backend.stop_worker(row['run_id'])
            except Exception as error:
                with self.db: self._event('stop_error', str(error), row['run_id'])
        if self._external_inhibition: self._paused_reason = self._external_inhibition
        elif not (self.config.accept_eula and self.config.accept_offline): self._paused_reason = 'acceptance_required'
        elif now < float(self._setting('circuit_until')): self._paused_reason = 'failure_circuit_open'
        elif self._last_capacity['pressure']: self._paused_reason = self._last_capacity['pressure']
        elif now < float(self._setting('next_start')): self._paused_reason = 'failure_backoff'
        elif now - float(self._setting('last_start')) < self.config.stagger_seconds: self._paused_reason = 'startup_stagger'
        elif any(row['state'] in ('pending', 'created', 'starting') for row in active):
            # Loading a world and starting both JVMs is the most expensive part of a run.
            # Wait for readiness or the existing startup timeout before starting another.
            self._paused_reason = 'startup_warmup'
        elif any(self._cadence_pressure(row, now) for row in active):
            self._paused_reason = 'worker_tick_pressure'
        elif desired > self._last_capacity['limit']: self._paused_reason = 'resource_capacity_limit'
        if self._paused_reason in (None, 'resource_capacity_limit') and len(active) < min(desired, self._last_capacity['limit']):
            run_id = time.strftime('%Y%m%d%H%M%S', time.gmtime(now)) + '-' + uuid.uuid4().hex[:10]
            try:
                spec = self._new_spec(run_id)
            except NoEligibleTask as error:
                # An intentional scheduler cooldown is a healthy admission pause.
                # Existing workers were already monitored and desired remains persistent.
                self._paused_reason = 'no_eligible_task'
                self._next_eligible_wall = error.retry_at
                self._cleanup()
                self._report_pause()
                return self.snapshot()
            with self.db:
                self.db.execute('INSERT INTO workers(run_id,state,spec,created,updated) VALUES (?,?,?,?,?)',
                                (run_id, 'pending', json.dumps(spec), now, now))
                self._set('last_start', now)
                self._event('assigned', f'Assigned {spec["task"]}', run_id)
            try:
                backend_id = self.backend.create_worker(spec)
                with self.db:
                    self.db.execute('UPDATE workers SET backend_id=?,state=? WHERE run_id=?', (backend_id, 'starting', run_id))
                    self._event('started', 'Worker started', run_id)
            except Exception as error:
                row = self.db.execute('SELECT * FROM workers WHERE run_id=?', (run_id,)).fetchone()
                with self.db:
                    self._event('create_error', str(error), run_id)
                    self._record_failure(row, 'create_failed', now)
        self._cleanup()
        self._report_pause()
        return self.snapshot()

    def _cleanup(self):
        rows = list(self.db.execute('SELECT * FROM workers WHERE finalized=1 AND removed=0 ORDER BY created DESC'))
        for row in rows[self.config.retained_workers:]:
            if row['state'] not in TERMINAL: continue
            # Only runtime objects are removed. Artifacts and worlds belong to explicit retention policy.
            try: self.backend.remove_worker(row['run_id'])
            except Exception: continue
            with self.db:
                self.db.execute('UPDATE workers SET removed=1 WHERE run_id=?', (row['run_id'],))
                self._event('runtime_removed', 'Removed terminal runtime after artifact verification', row['run_id'])

    def snapshot(self):
        with self._thread_lock:
            workers = []
            for row in self.db.execute('SELECT * FROM workers ORDER BY created DESC,run_id DESC'):
                spec = json.loads(row['spec'])
                health = json.loads(row['health']) if row['health'] else None
                result = json.loads(row['result']) if row['result'] else None
                workers.append({'runId': row['run_id'], 'backendId': row['backend_id'], 'state': row['state'],
                                'task': spec['task'], 'worldId': spec.get('worldId'), 'seed': spec['seed'],
                                'gamemode': spec.get('gamemode', 'survival'), 'operator': spec.get('operator', False),
                                'scenario': spec.get('scenario'), 'operatorLevel': spec.get('operatorLevel', 2),
                                'assignment': spec, 'createdWall': row['created'], 'updatedWall': row['updated'],
                                'reason': row['reason'], 'health': health, 'healthy': self._healthy(health, row['state']), 'result': result,
                                'artifacts': str(self.config.artifacts_dir / row['run_id']),
                                'artifactsVerified': bool(row['finalized']), 'coverageIngested': bool(row['ingested']), 'removed': bool(row['removed'])})
            counts = {name: sum(w['state'] == name for w in workers) for name in sorted(ACTIVE | TERMINAL)}
            counts['healthy'] = sum(w['healthy'] for w in workers)
            counts['active'] = sum(counts.get(name, 0) for name in ACTIVE)
            events = [{'id': r['id'], 'wall': r['wall'], 'kind': r['kind'], 'runId': r['run_id'], 'message': r['message']}
                      for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (self.config.event_limit,))]
            coverage = self.scheduler.snapshot() if self.scheduler else None
            return {'fleetId': self.config.fleet_id, 'backend': self.config.backend, 'desired': int(self._setting('desired')),
                    'config': self.config.public(), 'capacity': self._last_capacity, 'counts': counts,
                    'workers': workers, 'events': events, 'pausedReason': self._paused_reason,
                    'nextEligibleWall': self._next_eligible_wall,
                    'circuitUntil': float(self._setting('circuit_until')), 'coverage': coverage, 'wall': self.clock()}
