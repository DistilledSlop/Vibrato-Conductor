"""Owned-worker runtimes. All discovery and mutation is scoped to one fleet label."""
from __future__ import annotations
import json
import os
import re
import subprocess
import time
from pathlib import Path

WORKER_LABEL = 'dev.vibrato.conductor.worker'
FLEET_LABEL = 'dev.vibrato.conductor.fleet'
RUN_LABEL = 'dev.vibrato.conductor.run'
SPEC_LABEL = 'dev.vibrato.conductor.assignment'


class BackendError(RuntimeError): pass


def command(argv, *, data=None, timeout=30):
    try:
        result = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackendError(str(error)) from error
    if result.returncode:
        raise BackendError(result.stderr.strip() or result.stdout.strip() or f'{argv[0]} exited {result.returncode}')
    return result.stdout


def worker_env(config, spec):
    gamemode = spec.get('gamemode', 'survival')
    operator = spec.get('operator', False)
    operator_level = spec.get('operatorLevel', 2)
    scenario = spec.get('scenario')
    if gamemode not in ('survival', 'creative', 'adventure', 'spectator'):
        raise ValueError('Assignment gamemode must be a known Minecraft mode')
    if not isinstance(operator, bool): raise ValueError('Assignment operator must be boolean')
    minimum_level = 1 if operator else 0
    if isinstance(operator_level, bool) or not isinstance(operator_level, int) or not minimum_level <= operator_level <= 4:
        raise ValueError(f'Assignment operatorLevel must be an integer between {minimum_level} and 4')
    if scenario is not None and not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', scenario):
        raise ValueError('Assignment scenario must be a safe identifier')
    env = dict(config.worker_env)
    env.update({
        'RUN_ID': spec['runId'], 'SEED': str(spec['seed']), 'TASK': spec['task'],
        'RUN_SECONDS': str(spec['seconds']), 'PERF_MODE': config.perf,
        'STARTUP_SECONDS': str(config.startup_timeout), 'MODE': config.mode,
        'ACCEPT_EULA': 'true' if config.accept_eula else 'false',
        'ACCEPT_OFFLINE': 'true' if config.accept_offline else 'false',
        'MAX_LOCAL_BYTES': str(config.storage_mb * 1024**2),
        'GAMEMODE': gamemode, 'OPERATOR': 'true' if operator else 'false',
        'OP_PERMISSION_LEVEL': str(operator_level),
    })
    if scenario is not None: env['SCENARIO'] = scenario
    if spec.get('worldId'): env['WORLD_ID'] = spec['worldId']
    if config.allow_world_copy: env['ALLOW_WORLD_COPY'] = 'true'
    return env


class DockerBackend:
    def __init__(self, config): self.config = config

    def name(self, run_id): return f'vibrato-{self.config.fleet_id}-{run_id}'

    def _inspect(self, name):
        values = json.loads(command(['docker', 'inspect', name]))
        if len(values) != 1: raise BackendError('Ambiguous container identity')
        value = values[0]
        labels = value.get('Config', {}).get('Labels', {}) or {}
        if labels.get(FLEET_LABEL) != self.config.fleet_id or labels.get(WORKER_LABEL) != 'true':
            raise BackendError('Refusing to operate on a container outside this fleet')
        return value

    def list_workers(self):
        ids = command(['docker', 'ps', '-aq', '--filter', f'label={WORKER_LABEL}=true',
                       '--filter', f'label={FLEET_LABEL}={self.config.fleet_id}']).split()
        if not ids: return []
        values = json.loads(command(['docker', 'inspect', *ids]))
        workers = []
        for value in values:
            labels = value.get('Config', {}).get('Labels', {}) or {}
            if labels.get(FLEET_LABEL) != self.config.fleet_id: continue
            state = value['State']
            raw_state = state['Status']
            lifecycle = {'created': 'created', 'running': 'running', 'paused': 'unhealthy',
                         'exited': 'completed' if state.get('ExitCode') == 0 else 'failed',
                         'dead': 'failed', 'removing': 'stopping'}.get(raw_state, 'starting')
            try: spec = json.loads(labels.get(SPEC_LABEL, '{}'))
            except ValueError: spec = {}
            workers.append({'runId': labels.get(RUN_LABEL), 'backendId': value['Id'],
                            'name': value['Name'].lstrip('/'), 'state': lifecycle,
                            'runtimeHealth': state.get('Health', {}).get('Status'),
                            'exitCode': state.get('ExitCode') if raw_state in ('exited', 'dead') else None,
                            'reason': 'oom_killed' if state.get('OOMKilled') else state.get('Error') or None,
                            'spec': spec})
        return workers

    def create_worker(self, spec):
        config = self.config
        name = self.name(spec['runId'])
        # Discovery before create makes retries safe after a lost Docker CLI response.
        existing = {w['runId']: w for w in self.list_workers()}.get(spec['runId'])
        if existing:
            if existing['state'] == 'created': self.start_worker(spec['runId'])
            return existing['backendId']
        workspace = config.workspace_dir / spec['runId']
        workspace.mkdir(parents=True, exist_ok=True)
        config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        argv = ['docker', 'create', '--name', name, '--label', f'{WORKER_LABEL}=true',
                '--label', f'{FLEET_LABEL}={config.fleet_id}', '--label', f'{RUN_LABEL}={spec["runId"]}',
                '--label', f'{SPEC_LABEL}={json.dumps(spec, separators=(",", ":"))}',
                '--init', '--hostname', 'vibrato-worker', '--add-host', 'vibrato-worker:127.0.0.1',
                '--network', 'none',
                '--pids-limit', '512', '--shm-size', '256m', '--read-only', '--stop-timeout', '90',
                '--tmpfs', '/tmp:rw,exec,nosuid,size=512m', '--user', f'{config.worker_uid}:{config.worker_gid}',
                '--mount', f'type=bind,src={workspace},dst=/work',
                '--mount', f'type=bind,src={config.artifacts_dir},dst=/artifacts']
        if config.cpus: argv += ['--cpus', str(config.cpus)]
        if config.memory_mb: argv += ['--memory', f'{config.memory_mb}m']
        if config.world_bank:
            argv += ['--mount', f'type=bind,src={config.world_bank},dst=/world-bank,readonly']
        for key, value in worker_env(config, spec).items(): argv += ['--env', f'{key}={value}']
        argv += [config.image]
        backend_id = command(argv).strip()
        # If this call fails, the pending intent and labelled created container survive for adoption.
        command(['docker', 'start', name])
        return backend_id

    def start_worker(self, run_id):
        self._inspect(self.name(run_id))
        command(['docker', 'start', self.name(run_id)])

    def stop_worker(self, run_id):
        self._inspect(self.name(run_id))
        command(['docker', 'stop', '--timeout', '90', self.name(run_id)], timeout=100)

    def remove_worker(self, run_id):
        value = self._inspect(self.name(run_id))
        if value['State']['Running']: raise BackendError('Cannot remove an active worker')
        command(['docker', 'rm', self.name(run_id)])

    def logs(self, run_id, tail=100):
        self._inspect(self.name(run_id))
        return command(['docker', 'logs', '--tail', str(min(max(int(tail), 1), 1000)), self.name(run_id)])


class KubernetesBackend:
    """Bare Pods deliberately avoid Job retries reusing a run's mutable artifact identity."""
    def __init__(self, config): self.config = config

    def _command(self, *args, data=None):
        argv = [self.config.kubectl]
        if self.config.kubeconfig: argv += ['--kubeconfig', str(self.config.kubeconfig)]
        if self.config.kube_context: argv += ['--context', self.config.kube_context]
        argv += ['--namespace', self.config.namespace, *args]
        return command(argv, data=data, timeout=45)

    def name(self, run_id): return f'vibrato-{self.config.fleet_id}-{run_id}'

    def pod_manifest(self, spec):
        config = self.config
        labels = {WORKER_LABEL: 'true', FLEET_LABEL: config.fleet_id, RUN_LABEL: spec['runId']}
        volumes = [
            {'name': 'work', 'emptyDir': {'sizeLimit': f'{config.storage_mb}Mi'}},
            {'name': 'tmp', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '512Mi'}},
            {'name': 'shm', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '256Mi'}},
            {'name': 'artifacts', 'persistentVolumeClaim': {'claimName': config.artifacts_pvc}},
        ]
        mounts = [{'name': 'work', 'mountPath': '/work'}, {'name': 'tmp', 'mountPath': '/tmp'},
                  {'name': 'shm', 'mountPath': '/dev/shm'}, {'name': 'artifacts', 'mountPath': '/artifacts'}]
        if config.world_bank_pvc:
            volumes.append({'name': 'world-bank', 'persistentVolumeClaim': {'claimName': config.world_bank_pvc, 'readOnly': True}})
            mounts.append({'name': 'world-bank', 'mountPath': '/world-bank', 'readOnly': True})
        resources = {'ephemeral-storage': f'{config.storage_mb}Mi'}
        if config.cpus: resources['cpu'] = str(config.cpus)
        if config.memory_mb: resources['memory'] = f'{config.memory_mb}Mi'
        return {
            'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': self.name(spec['runId']), 'namespace': config.namespace, 'labels': labels,
                         'annotations': {SPEC_LABEL: json.dumps(spec, separators=(',', ':'))}},
            'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                     'terminationGracePeriodSeconds': 90,
                     'activeDeadlineSeconds': int(spec['seconds'] + config.startup_timeout * 4 + 120),
                     'securityContext': {'runAsUser': config.worker_uid, 'runAsGroup': config.worker_gid,
                                         'fsGroup': config.worker_gid, 'runAsNonRoot': True,
                                         'seccompProfile': {'type': 'RuntimeDefault'}},
                     'hostAliases': [{'ip': '127.0.0.1', 'hostnames': ['vibrato-worker']}],
                     'containers': [{
                         'name': 'worker', 'image': config.image, 'imagePullPolicy': 'IfNotPresent',
                         'env': [{'name': key, 'value': value} for key, value in worker_env(config, spec).items()],
                         'resources': {
                             'requests': dict(resources),
                             'limits': dict(resources),
                         },
                         'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                             'capabilities': {'drop': ['ALL']}},
                         'volumeMounts': mounts,
                         'startupProbe': {'exec': {'command': ['python3', '-m', 'conductor.worker', '--healthcheck']},
                                          'periodSeconds': 10, 'timeoutSeconds': 5,
                                          'failureThreshold': max(1, int(config.startup_timeout / 10))},
                         'readinessProbe': {'exec': {'command': ['python3', '-m', 'conductor.worker', '--healthcheck']},
                                            'periodSeconds': 10, 'timeoutSeconds': 5, 'failureThreshold': 3},
                     }], 'volumes': volumes},
        }

    def list_workers(self):
        selector = f'{WORKER_LABEL}=true,{FLEET_LABEL}={self.config.fleet_id}'
        values = json.loads(self._command('get', 'pods', '-l', selector, '-o', 'json'))['items']
        workers = []
        for value in values:
            meta, status = value['metadata'], value.get('status', {})
            raw_state = status.get('phase', 'Pending')
            lifecycle = {'Pending': 'starting', 'Running': 'running', 'Succeeded': 'completed',
                         'Failed': 'failed', 'Unknown': 'unhealthy'}.get(raw_state, 'starting')
            if meta.get('deletionTimestamp'): lifecycle = 'stopping'
            statuses = status.get('containerStatuses', [])
            termination = next((v.get('state', {}).get('terminated') for v in statuses if v.get('state', {}).get('terminated')), {})
            waiting = next((v.get('state', {}).get('waiting') for v in statuses if v.get('state', {}).get('waiting')), {})
            reason = termination.get('reason') or waiting.get('reason') or status.get('reason')
            if reason in ('ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError', 'InvalidImageName'):
                lifecycle = 'unhealthy'
            try: spec = json.loads(meta.get('annotations', {}).get(SPEC_LABEL, '{}'))
            except ValueError: spec = {}
            workers.append({'runId': meta.get('labels', {}).get(RUN_LABEL), 'backendId': meta['uid'],
                            'name': meta['name'], 'state': lifecycle, 'exitCode': termination.get('exitCode'),
                            'reason': reason, 'runtimeHealth': 'healthy' if statuses and all(v.get('ready') for v in statuses) else 'starting',
                            'spec': spec})
        return workers

    def create_worker(self, spec):
        existing = {w['runId']: w for w in self.list_workers()}.get(spec['runId'])
        if existing: return existing['backendId']
        # create (not apply) avoids modifying an existing object that changed ownership.
        value = json.loads(self._command('create', '-f', '-', '-o', 'json', data=json.dumps(self.pod_manifest(spec))))
        return value['metadata']['uid']

    def start_worker(self, run_id):
        # Kubernetes starts Pending Pods through its scheduler; there is no host-side start operation.
        return None

    def _owned(self, run_id):
        value = json.loads(self._command('get', 'pod', self.name(run_id), '-o', 'json'))
        if value['metadata'].get('labels', {}).get(FLEET_LABEL) != self.config.fleet_id:
            raise BackendError('Refusing to operate on a pod outside this fleet')
        return value

    def stop_worker(self, run_id):
        self._owned(run_id)
        self._command('delete', 'pod', self.name(run_id), '--wait=false', '--grace-period=90')

    def remove_worker(self, run_id):
        value = self._owned(run_id)
        if value.get('status', {}).get('phase') not in ('Succeeded', 'Failed'):
            raise BackendError('Cannot remove an active worker')
        self._command('delete', 'pod', self.name(run_id), '--wait=false')

    def logs(self, run_id, tail=100):
        self._owned(run_id)
        return self._command('logs', self.name(run_id), '--tail', str(min(max(int(tail), 1), 1000)))
