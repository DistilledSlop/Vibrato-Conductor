#!/usr/bin/env python3
"""Validate only the repository's isolated kind cluster through the deployed controller API.

Default checks the target-zero controller and authentication. --exercise performs real
Minecraft worker recovery tests and requires explicit EULA/offline acceptance flags.
"""
from __future__ import annotations
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.state/kubernetes'
sys.path.insert(0, str(ROOT))
from conductor.storage import digest, verify_manifest


class Test:
    def __init__(self, port, image, healthy_pause):
        self.port = port
        self.image = image
        self.healthy_pause = healthy_pause
        self.base = f'http://127.0.0.1:{port}'
        self.token = (STATE / 'dashboard.token').read_text().strip()
        self.forward = None
        self.log = None
        self.checks = []

    def kube(self, *args, data=None, timeout=150):
        argv = [str(ROOT / '.tools/kubectl'), '--kubeconfig', str(STATE / 'kubeconfig'), *args]
        result = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout)
        if result.returncode: raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def json_kube(self, *args): return json.loads(self.kube(*args, '-o', 'json'))

    def api(self, path='/api/state', data=None, auth=True):
        headers = {'Content-Type': 'application/json'}
        if auth: headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.base + path, headers=headers,
                    data=None if data is None else json.dumps(data).encode())
        with urllib.request.urlopen(request, timeout=20) as response: return json.load(response)

    def wait(self, predicate, label, timeout=360):
        deadline, last, next_notice = time.monotonic() + timeout, None, 0
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value: return value
            except (OSError, RuntimeError, ValueError, urllib.error.URLError) as error: last = str(error)
            if time.monotonic() >= next_notice:
                print('Waiting:', label, flush=True)
                next_notice = time.monotonic() + 30
            time.sleep(2)
        raise RuntimeError(f'Timed out: {label}; last error={last}')

    def record(self, name, **details):
        self.checks.append({'check': name, 'passed': True, **details})
        print('Passed:', name, flush=True)

    def forward_start(self):
        self.forward_stop()
        self.log = (STATE / 'live-port-forward.log').open('ab')
        self.forward = subprocess.Popen([str(ROOT / '.tools/kubectl'), '--kubeconfig', str(STATE / 'kubeconfig'),
                    '-n', 'vibrato', 'port-forward', 'service/vibrato-controller', f'{self.port}:8080'],
                    stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda: self.api('/readyz', auth=False).get('ok'), 'controller readiness', timeout=120)

    def forward_stop(self):
        if self.forward is not None:
            self.forward.terminate()
            try: self.forward.wait(timeout=5)
            except subprocess.TimeoutExpired: self.forward.kill(); self.forward.wait()
            self.forward = None
        if self.log: self.log.close(); self.log = None

    def controller_restart(self):
        self.forward_stop()
        self.kube('-n', 'vibrato', 'rollout', 'restart', 'deployment/vibrato-controller')
        self.kube('-n', 'vibrato', 'rollout', 'status', 'deployment/vibrato-controller', '--timeout=120s')
        self.forward_start()

    def active(self):
        return [w for w in self.api()['workers'] if w['state'] in
                ('pending', 'created', 'starting', 'running', 'unhealthy', 'stopping', 'missing')]

    def prepare_exercise(self):
        # A private test catalog keeps this reproducible without changing the shipped task budgets.
        catalog = STATE / 'controller/live-test-catalog'
        shutil.copytree(ROOT / 'catalogs', catalog, dirs_exist_ok=True)
        tasks = json.loads((catalog / 'tasks.json').read_text())
        for task in tasks['tasks']:
            if task['id'] == 'get-dirt': task['seconds'] = 120
        (catalog / 'tasks.json').write_text(json.dumps(tasks, indent=2) + '\n')
        config_map = self.json_kube('-n', 'vibrato', 'get', 'configmap', 'vibrato-controller-config')
        config = json.loads(config_map['data']['fleet.json'])
        config.update({'desired': 0, 'image': self.image, 'max_workers': 1, 'cpus': 8, 'cpu_budget': 8,
            'accept_eula': True, 'accept_offline': True,
            'enabled_tasks': ['get-dirt'], 'mode': 'collection', 'catalog_dir': '/data/live-test-catalog', 'seconds': 75,
            'stagger_seconds': 3, 'reconcile_seconds': 2, 'failure_backoff_seconds': 3,
            'max_failure_backoff_seconds': 15, 'circuit_pause_seconds': 30,
            'worker_env': {**config.get('worker_env', {}), 'LP_NUM_THREADS': '2', 'COLLECTION_SETTLE_SECONDS': '120'},
            'unhealthy_grace_seconds': 5, 'world_bank_pvc': 'vibrato-world-bank',
            'world_bank': '/world-bank', 'world_ids': ['seed-424242'], 'allow_world_copy': True})
        patch = {'data': {'fleet.json': json.dumps(config, indent=2)}}
        self.kube('-n', 'vibrato', 'patch', 'configmap', 'vibrato-controller-config', '--type=merge', '-p', json.dumps(patch))
        patch = {'spec': {'template': {'spec': {
            'volumes': [{'name': 'world-bank', 'persistentVolumeClaim': {'claimName': 'vibrato-world-bank', 'readOnly': True}}],
            'containers': [{'name': 'controller', 'volumeMounts': [{'name': 'world-bank', 'mountPath': '/world-bank', 'readOnly': True}]}],
        }}}}
        self.kube('-n', 'vibrato', 'patch', 'deployment', 'vibrato-controller', '--type=strategic', '-p', json.dumps(patch))
        # ConfigMaps do not restart a Deployment when only their data changes.
        self.kube('-n', 'vibrato', 'rollout', 'restart', 'deployment/vibrato-controller')
        self.kube('-n', 'vibrato', 'rollout', 'status', 'deployment/vibrato-controller', '--timeout=120s')
        self.forward_start()
        return catalog

    def exercise(self):
        bank = ROOT / '.state/world-bank/seed-424242'
        before = {str(p.relative_to(bank)): digest(p) for p in bank.rglob('*') if p.is_file()}
        catalog = self.prepare_exercise()
        if self.api()['desired'] != 0: raise RuntimeError('Live test requires an initially drained fleet')
        self.api('/api/fleet/resume', {})
        self.api('/api/fleet', {'desired': 1})
        first = self.wait(lambda: next((w for w in self.active() if w.get('healthy')), None), 'first Kubernetes worker healthy')
        first_id = first['runId']
        self.record('real_kubernetes_worker_healthy', runId=first_id, health=first.get('health'))
        if self.healthy_pause:
            print(f'Healthy baseline inspection window: {self.healthy_pause}s', flush=True)
            time.sleep(self.healthy_pause)
        # Existing worker keeps its 120s settle window; replacements get 90s after task success.
        tasks = json.loads((catalog / 'tasks.json').read_text())
        for task in tasks['tasks']:
            if task['id'] == 'get-dirt': task['seconds'] = 75
        (catalog / 'tasks.json').write_text(json.dumps(tasks, indent=2) + '\n')
        config_map = self.json_kube('-n', 'vibrato', 'get', 'configmap', 'vibrato-controller-config')
        config = json.loads(config_map['data']['fleet.json'])
        config['worker_env']['COLLECTION_SETTLE_SECONDS'] = '90'
        self.kube('-n', 'vibrato', 'patch', 'configmap', 'vibrato-controller-config', '--type=merge', '-p',
                  json.dumps({'data': {'fleet.json': json.dumps(config, indent=2)}}))
        self.controller_restart()
        self.wait(lambda: len(self.active()) == 1 and self.active()[0]['runId'] == first_id, 'adoption after controller restart', timeout=60)
        owned = self.json_kube('-n', 'vibrato', 'get', 'pods', '-l', 'dev.vibrato.conductor.fleet=cluster')['items']
        assert len(owned) == 1 and owned[0]['metadata']['labels']['dev.vibrato.conductor.run'] == first_id
        self.record('restart_adopts_without_duplicate', runId=first_id)
        self.kube('-n', 'vibrato', 'delete', 'pod', owned[0]['metadata']['name'], '--grace-period=0', '--force', '--wait=false')
        second = self.wait(lambda: next((w for w in self.active() if w['runId'] != first_id and w.get('healthy')), None),
                           'fresh worker after deleted Pod', timeout=420)
        second_id = second['runId']
        assert first_id != second_id
        self.record('deleted_pod_replaced_with_fresh_run', oldRunId=first_id, newRunId=second_id)
        completed = self.wait(lambda: next((w for w in self.api()['workers'] if w['runId'] == second_id and w['state'] == 'completed'), None),
                              'replacement completes a diagnostic', timeout=180)
        assert completed.get('result', {}).get('diagnosticPassed') is True
        assert completed['result'].get('taskSuccess') is True, 'Dirt task must have a confirmed successful outcome'
        self.record('replacement_diagnostic_completed', runId=second_id,
                    capture=completed['result']['capture'], serverHealth=completed['result'].get('serverHealth'))
        self.api('/api/fleet', {'desired': 0})
        self.wait(lambda: self.api()['counts']['active'] == 0, 'scale to zero', timeout=180)
        self.record('scale_zero_drains_workers')
        # Continuous replenishment may schedule the next worker before our completion poll.
        # Every run launched by this exercise must retain evidence, including that drained run.
        run_ids = [w['runId'] for w in self.api()['workers'] if w['createdWall'] >= first['createdWall']]
        self.wait(lambda: all((STATE/'dataset'/rid/'manifest.json').is_file() for rid in run_ids),
                  'interrupted and completed artifacts centrally delivered', timeout=120)
        for run_id in run_ids:
            verify_manifest(STATE / 'dataset' / run_id)
        self.record('interrupted_evidence_and_success_delivered', runIds=run_ids)
        after = {str(p.relative_to(bank)): digest(p) for p in bank.rglob('*') if p.is_file()}
        assert before == after, 'World bank changed during Kubernetes tests'
        self.record('world_bank_immutable', files=len(before))
        return {'firstRunId': first_id, 'replacementRunId': second_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise', action='store_true')
    parser.add_argument('--accept-eula', action='store_true')
    parser.add_argument('--accept-offline', action='store_true')
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--image', default='vibrato-worker:dev')
    parser.add_argument('--healthy-pause', type=float, default=25)
    args = parser.parse_args()
    if args.exercise and not (args.accept_eula and args.accept_offline):
        parser.error('--exercise requires --accept-eula and --accept-offline')
    if not 0 <= args.healthy_pause <= 60: parser.error('--healthy-pause must be 0-60 seconds')
    test = Test(args.port, args.image, args.healthy_pause)
    current = test.kube('config', 'current-context').strip()
    if current != 'kind-vibrato-conductor-test': raise RuntimeError('Refusing to modify a non-test Kubernetes context')
    result = {}
    try:
        test.forward_start()
        try: test.api(auth=False)
        except urllib.error.HTTPError as error:
            if error.code != 401: raise
        else: raise RuntimeError('Unauthenticated API unexpectedly succeeded')
        test.record('api_requires_authentication')
        state = test.api()
        test.record('authenticated_api_ready', fleetId=state['fleetId'], desired=state['desired'])
        if args.exercise: result = test.exercise()
        report = {'passed': True, 'wall': time.time(), 'context': current, 'checks': test.checks, **result}
        destination = STATE / ('live-results.json' if args.exercise else 'controller-results.json')
        destination.write_text(json.dumps(report, indent=2) + '\n')
        print('Results:', destination, flush=True)
    finally:
        if args.exercise:
            try: test.api('/api/fleet', {'desired': 0})
            except Exception:
                # Preserve the drain intent even if port-forward/controller restarted during failure.
                database = STATE / 'controller/fleet/fleet.sqlite3'
                if database.exists():
                    with sqlite3.connect(database, timeout=30) as db:
                        db.execute("UPDATE settings SET value='0' WHERE key='desired'")
        test.forward_stop()

if __name__ == '__main__': main()
