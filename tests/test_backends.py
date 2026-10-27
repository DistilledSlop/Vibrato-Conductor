import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.backends import BackendError, DockerBackend, KubernetesBackend, FLEET_LABEL, RUN_LABEL, WORKER_LABEL, worker_env
from conductor.fleet import FleetConfig


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.config = FleetConfig.from_dict({'fleet_id': 'test', 'state_dir': root/'state',
            'artifacts_dir': root/'artifacts', 'workspace_dir': root/'work',
            'accept_eula': True, 'accept_offline': True})
        self.spec = {'runId': 'run-001', 'task': 'director', 'seed': 7, 'seconds': 60, 'worldId': None}

    def test_kubernetes_uses_isolated_never_restart_pods_and_explicit_resources(self):
        config = FleetConfig.from_dict({**self.config.__dict__, 'backend': 'kubernetes', 'world_bank_pvc': 'bank'})
        manifest = KubernetesBackend(config).pod_manifest(self.spec)
        self.assertEqual(manifest['kind'], 'Pod')
        self.assertEqual(manifest['spec']['restartPolicy'], 'Never')
        self.assertFalse(manifest['spec']['automountServiceAccountToken'])
        container = manifest['spec']['containers'][0]
        self.assertEqual(container['resources']['requests'], container['resources']['limits'])
        self.assertEqual(container['resources']['requests']['ephemeral-storage'], '8192Mi')
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertTrue(next(v for v in container['volumeMounts'] if v['name']=='world-bank')['readOnly'])
        self.assertEqual(next(v for v in container['env'] if v['name']=='RUN_ID')['value'], 'run-001')
        self.assertEqual(manifest['metadata']['labels'][FLEET_LABEL], 'test')

    def test_assignment_modes_and_operator_permissions_are_explicit(self):
        baseline = worker_env(self.config, self.spec)
        self.assertEqual(baseline['GAMEMODE'], 'survival')
        self.assertEqual(baseline['OPERATOR'], 'false')
        self.assertEqual(baseline['OP_PERMISSION_LEVEL'], '2')
        self.assertEqual(worker_env(self.config, {**self.spec, 'operator': False, 'operatorLevel': 0})['OP_PERMISSION_LEVEL'], '0')
        creative = worker_env(self.config, {**self.spec, 'gamemode': 'creative', 'operator': True, 'scenario': 'creative-builder'})
        self.assertEqual(creative['GAMEMODE'], 'creative')
        self.assertEqual(creative['OPERATOR'], 'true')
        self.assertEqual(creative['SCENARIO'], 'creative-builder')
        for spec in ({'gamemode': 'invented'}, {'operator': 'false'}, {'operator': True, 'operatorLevel': 0}, {'operatorLevel': True}, {'scenario': '../escape'}):
            with self.subTest(spec=spec), self.assertRaises(ValueError): worker_env(self.config, {**self.spec, **spec})

    def test_docker_create_retry_adopts_created_container(self):
        backend = DockerBackend(self.config)
        existing = {'runId': 'run-001', 'backendId': 'existing', 'state': 'created'}
        with patch.object(backend, 'list_workers', return_value=[existing]), patch.object(backend, 'start_worker') as start:
            self.assertEqual(backend.create_worker(self.spec), 'existing')
            start.assert_called_once_with('run-001')

    def test_docker_stop_refuses_an_unowned_container(self):
        backend = DockerBackend(self.config)
        with patch('conductor.backends.command', return_value=json.dumps([{'Config': {'Labels': {FLEET_LABEL:'other'}}}])) as invoke:
            with self.assertRaises(BackendError): backend.stop_worker('run-001')
            self.assertEqual(invoke.call_count, 1)

    def test_kubernetes_delete_checks_ownership(self):
        backend = KubernetesBackend(self.config)
        with patch.object(backend, '_command', return_value=json.dumps({'metadata': {'labels': {FLEET_LABEL: 'other'}}})) as invoke:
            with self.assertRaises(BackendError): backend.stop_worker('run-001')
            self.assertEqual(invoke.call_count, 1)

    def test_docker_oom_and_exit_failures_are_observable(self):
        backend = DockerBackend(self.config)
        value = {'Id': 'one', 'Name': '/owned', 'Config': {'Labels': {FLEET_LABEL:'test',WORKER_LABEL:'true',RUN_LABEL:'run-001'}},
                 'State': {'Status': 'exited', 'ExitCode': 137, 'OOMKilled': True}}
        with patch('conductor.backends.command', side_effect=['one', json.dumps([value])]):
            workers = backend.list_workers()
        self.assertEqual(workers[0]['reason'], 'oom_killed')
        self.assertEqual(workers[0]['state'], 'failed')

    def test_kubernetes_image_pull_failure_does_not_masquerade_as_startup(self):
        backend = KubernetesBackend(self.config)
        value = {'metadata': {'uid':'one','name':'owned','labels':{RUN_LABEL:'run-001'}},
                 'status':{'phase':'Pending','containerStatuses':[{'state':{'waiting':{'reason':'ImagePullBackOff'}}}]}}
        with patch.object(backend, '_command', return_value=json.dumps({'items':[value]})):
            workers = backend.list_workers()
        self.assertEqual(workers[0]['state'], 'unhealthy')
        self.assertEqual(workers[0]['reason'], 'ImagePullBackOff')


if __name__ == '__main__': unittest.main()
