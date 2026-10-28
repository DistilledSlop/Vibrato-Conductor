"""Worker commands and the persistent fleet control service."""
import argparse
import json
import os
import subprocess
from pathlib import Path

from .storage import digest
from .worker import Config

LABEL = 'dev.vibrato.conductor.worker=true'


def owned(name):
    info = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True))[0]
    if info.get('Config', {}).get('Labels', {}).get('dev.vibrato.conductor.worker') != 'true':
        raise ValueError('Refusing to control a container without the conductor worker label')
    return info


def verify(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    for record in manifest['files']:
        path = root / record['path']
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe artifact manifest path')
        if path.stat().st_size != record['bytes'] or digest(path) != record['sha256']:
            raise ValueError(f'Artifact verification failed: {record["path"]}')
    return manifest['result']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Start one diagnostic worker')
    run.add_argument('--id', required=True)
    run.add_argument('--image', default='vibrato-worker:dev')
    run.add_argument('--seed', type=int, default=424242)
    run.add_argument('--task', default='director')
    run.add_argument('--seconds', type=float, default=300)
    run.add_argument('--state', type=Path, default=Path('.state'))
    run.add_argument('--artifacts', type=Path, help='Durable filesystem storage, defaults to STATE/artifacts')
    run.add_argument('--world-bank', type=Path)
    run.add_argument('--world-id')
    run.add_argument('--allow-world-copy', action='store_true')
    run.add_argument('--accept-eula', action='store_true')
    run.add_argument('--accept-offline', action='store_true')
    run.add_argument('--cpus', type=float, default=8)
    run.add_argument('--memory', default='6g')
    run.add_argument('--perf', choices=['normal', 'low'], default='normal')
    run.add_argument('--mode', choices=['diagnostic', 'collection'], default='diagnostic')
    run.add_argument('--gamemode', choices=['survival', 'creative', 'adventure', 'spectator'], default='survival')
    run.add_argument('--operator', action='store_true', help='Grant only this isolated bot operator permissions')
    run.add_argument('--operator-level', type=int, choices=[1, 2, 3, 4], default=2)
    run.add_argument('--render-threads', type=int, choices=range(1,33), help='Explicit llvmpipe rendering thread budget')
    run.add_argument('--wait', action='store_true')
    commands.add_parser('status', help='List conductor worker containers')
    stop = commands.add_parser('stop', help='Stop an owned worker, preserving artifacts')
    stop.add_argument('container')
    check = commands.add_parser('verify', help='Verify a finalized run manifest')
    check.add_argument('run', type=Path)
    web = commands.add_parser('serve', help='Run the fleet reconciler, authenticated API, and dashboard')
    web.add_argument('--config', type=Path, required=True)
    web.add_argument('--host', default='0.0.0.0')
    web.add_argument('--port', type=int, default=8080)
    web.add_argument('--token-file', type=Path)
    scale = commands.add_parser('scale', help='Change a running conductor fleet target')
    scale.add_argument('target', type=int)
    scale.add_argument('--url', default='http://127.0.0.1:8080')
    scale.add_argument('--token-file', type=Path, default=Path('.state/fleet/dashboard.token'))
    fleet_status = commands.add_parser('fleet-status', help='Inspect the running conductor API')
    fleet_status.add_argument('--url', default='http://127.0.0.1:8080')
    fleet_status.add_argument('--token-file', type=Path, default=Path('.state/fleet/dashboard.token'))
    enroll = commands.add_parser('import-training-run', help='Verify and deliver one eligible isolated run')
    enroll.add_argument('--config', type=Path, required=True)
    enroll.add_argument('source', type=Path)
    coverage = commands.add_parser('coverage-report', help='Report current admitted coverage and raw diagnostics')
    coverage.add_argument('--db', type=Path, required=True, help='Existing coverage SQLite database')
    coverage.add_argument('--central', type=Path, help='Central dataset root with current .admission receipts')
    coverage.add_argument('--catalog-dir', type=Path, help='Catalog directory, defaults to bundled catalogs')
    export = commands.add_parser('export-training-index', help='Index currently admitted capture paths for the reader')
    export.add_argument('--central', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--run-id', action='append', help='Restrict to these explicitly selected central runs; repeat for each')
    args = parser.parse_args()
    if args.command == 'coverage-report':
        import sqlite3
        import tempfile
        from .coverage import CoverageInventory
        source_path = args.db.resolve(strict=True)
        if not source_path.is_file(): raise ValueError('Coverage database must be an existing file')
        with tempfile.TemporaryDirectory(prefix='vibrato-coverage-report-') as folder:
            copied = Path(folder) / 'coverage.sqlite3'
            source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
            try:
                dest = sqlite3.connect(copied)
                try: source.backup(dest)
                finally: dest.close()
            finally: source.close()
            inventory = CoverageInventory(copied, catalog_path=args.catalog_dir, central_dir=args.central)
            print(json.dumps(inventory.snapshot(), indent=2))
        return 0
    if args.command == 'import-training-run':
        from .training import import_run
        print(json.dumps(import_run(args.source, args.config), indent=2))
        return 0
    if args.command == 'export-training-index':
        from .training import export_index
        index = export_index(args.central, args.output, args.run_id)
        print(json.dumps({'acceptedRuns': len(index['accepted']), 'rejectedRuns': len(index['rejected']),
                          'trainSessions': len(index['trainBases']), 'validationSessions': len(index['validationBases']),
                          'splitReady': index['split']['ready'], 'output': str(args.output)}, indent=2))
        return 0
    if args.command == 'serve':
        from .web import serve
        return serve(args.config, args.host, args.port, args.token_file)
    if args.command in ('scale', 'fleet-status'):
        import urllib.request
        token = args.token_file.read_text().strip()
        endpoint = '/api/fleet' if args.command == 'scale' else '/api/state'
        body = json.dumps({'desired': args.target}).encode() if args.command == 'scale' else None
        request = urllib.request.Request(args.url.rstrip('/') + endpoint, data=body,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=120) as response: print(response.read().decode())
        return 0
    if args.command == 'status':
        return subprocess.call(['docker', 'ps', '-a', '--filter', 'label=' + LABEL,
                                '--format', 'table {{.Names}}\t{{.Status}}\t{{.Image}}'])
    if args.command == 'stop':
        owned(args.container)
        return subprocess.call(['docker', 'stop', '--timeout', '90', args.container])
    if args.command == 'verify':
        print(json.dumps(verify(args.run), indent=2))
        return 0
    if not args.accept_eula or not args.accept_offline:
        parser.error('run requires --accept-eula and --accept-offline')
    values = {'RUN_ID': args.id, 'SEED': str(args.seed), 'TASK': args.task,
              'RUN_SECONDS': str(args.seconds), 'PERF_MODE': args.perf,
              'ACCEPT_EULA': 'true', 'ACCEPT_OFFLINE': 'true',
              'MODE': args.mode, 'GAMEMODE': args.gamemode,
              'OPERATOR': str(args.operator).lower(), 'OP_PERMISSION_LEVEL': str(args.operator_level)}
    if args.render_threads is not None: values['LP_NUM_THREADS'] = str(args.render_threads)
    if args.world_id:
        if args.world_bank is None: parser.error('--world-id requires --world-bank')
        values['WORLD_ID'] = args.world_id
    if args.allow_world_copy: values['ALLOW_WORLD_COPY'] = 'true'
    Config.read(values)
    if not 0 < args.cpus <= 1024: parser.error('--cpus must be positive and finite')
    artifacts = (args.artifacts or args.state / 'artifacts').resolve()
    workspace = (args.state / 'workspaces' / args.id).resolve()
    if (artifacts / args.id).exists() or workspace.exists():
        parser.error('Run already exists; choose a fresh --id')
    artifacts.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True)
    argv = ['docker', 'run', '-d', '--name', 'vibrato-' + args.id, '--label', LABEL,
            '--init', '--hostname', 'vibrato-worker', '--add-host', 'vibrato-worker:127.0.0.1',
            '--network', 'none', '--cpus', str(args.cpus), '--memory', args.memory,
            '--pids-limit', '512', '--shm-size', '256m', '--read-only',
            '--tmpfs', '/tmp:rw,exec,nosuid,size=512m', '--stop-timeout', '90',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '--mount', f'type=bind,src={workspace},dst=/work',
            '--mount', f'type=bind,src={artifacts},dst=/artifacts']
    if args.world_bank:
        bank = args.world_bank.resolve(strict=True)
        argv += ['--mount', f'type=bind,src={bank},dst=/world-bank,readonly']
    for key, value in values.items(): argv += ['--env', f'{key}={value}']
    argv += [args.image]
    subprocess.run(argv, check=True)
    print(f'Artifacts: {artifacts / args.id}', flush=True)
    if args.wait:
        container = 'vibrato-' + args.id
        code = subprocess.check_output(['docker', 'wait', container], text=True).strip()
        subprocess.run(['docker', 'logs', container], check=True)
        return int(code)
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
