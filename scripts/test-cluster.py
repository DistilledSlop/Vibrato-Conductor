#!/usr/bin/env python3
"""Manage the repository's isolated kind integration-test cluster (never an existing cluster)."""
import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = 'vibrato-conductor-test'
NODE = 'kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5'
TOOLS = ROOT / '.tools'
STATE = ROOT / '.state/kubernetes'


def install_tool(spec):
    name, url, checksum_url = spec
    path = TOOLS / name
    if path.exists(): return
    data = urllib.request.urlopen(url, timeout=120).read()
    checksum = urllib.request.urlopen(checksum_url, timeout=30).read().decode().split()[0]
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError(f'{name} checksum mismatch')
    temp = path.with_suffix('.download')
    temp.write_bytes(data); temp.chmod(0o755); temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['up', 'load', 'down'])
    parser.add_argument('--image', action='append', default=[])
    args = parser.parse_args()
    TOOLS.mkdir(exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    specs = [
        ('kind', 'https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64',
         'https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64.sha256sum'),
        ('kubectl', 'https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl',
         'https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl.sha256'),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(install_tool, specs))
    kind = str(TOOLS / 'kind')
    if args.operation == 'down':
        subprocess.run([kind, 'delete', 'cluster', '--name', CLUSTER], check=True)
        return
    if args.operation == 'load':
        subprocess.run([kind, 'load', 'docker-image', '--name', CLUSTER,
                        *(args.image or ['vibrato-worker:dev'])], check=True)
        return
    (STATE / 'artifacts').mkdir(exist_ok=True)
    (ROOT / '.state/world-bank').mkdir(exist_ok=True)
    config = {
        'kind': 'Cluster', 'apiVersion': 'kind.x-k8s.io/v1alpha4',
        'nodes': [{'role': 'control-plane', 'image': NODE, 'extraMounts': [
            {'hostPath': str(STATE), 'containerPath': '/conductor-state'},
            {'hostPath': str(ROOT / '.state/world-bank'), 'containerPath': '/conductor-world-bank', 'readOnly': True},
        ]}],
    }
    config_path = STATE / 'kind.json'
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    clusters = subprocess.check_output([kind, 'get', 'clusters'], text=True).splitlines()
    if CLUSTER not in clusters:
        subprocess.run([kind, 'create', 'cluster', '--name', CLUSTER, '--config', str(config_path),
                        '--kubeconfig', str(STATE / 'kubeconfig'), '--wait', '180s'], check=True)
    else:
        subprocess.run([kind, 'export', 'kubeconfig', '--name', CLUSTER,
                        '--kubeconfig', str(STATE / 'kubeconfig')], check=True)
    subprocess.run([str(TOOLS / 'kubectl'), '--kubeconfig', str(STATE / 'kubeconfig'),
                    'get', 'nodes'], check=True)

if __name__ == '__main__': main()
