#!/usr/bin/env python3
"""Stage opt-in, pinned optimization profiles and compare immutable worker evidence."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
from statistics import mean
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from conductor.storage import atomic_json, digest, verify_manifest
from conductor.artifacts import quality_report
from conductor.coverage import packet_id

DEFAULT_MANIFEST = ROOT / 'configs/optimization-profiles.json'
IMAGE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}')


def load_profiles(path=DEFAULT_MANIFEST):
    data = json.loads(Path(path).read_text())
    if data.get('schemaVersion') != 1 or data.get('minecraftVersion') != '1.21.1' or data.get('loader') != 'fabric':
        raise ValueError('Optimization catalogue must target Fabric Minecraft 1.21.1')
    for mod in data['mods'].values():
        if (Path(mod['filename']).name != mod['filename'] or not mod['filename'].endswith('.jar')
                or not re.fullmatch('[0-9a-f]{64}', mod['sha256']) or not re.fullmatch('[0-9a-f]{128}', mod['sha512'])
                or urllib.parse.urlparse(mod['url']).scheme != 'https'
                or urllib.parse.urlparse(mod['url']).hostname != 'cdn.modrinth.com'
                or '1.21.1' not in mod['minecraftVersions']):
            raise ValueError('Invalid pinned optimization artifact')
    return data


def verified_mod(mod, cache_dir):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / mod['filename']
    if path.exists(): data = path.read_bytes()
    else:
        request = urllib.request.Request(mod['url'], headers={'User-Agent': 'VibratoConductor/optimization-evaluation'})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(16 * 1024 * 1024 + 1)
    if (len(data) != mod['size'] or len(data) > 16 * 1024 * 1024
            or hashlib.sha512(data).hexdigest() != mod['sha512']
            or hashlib.sha256(data).hexdigest() != mod['sha256']):
        raise ValueError('Optimization artifact integrity failure: ' + mod['filename'])
    descriptor = json.loads(zipfile.ZipFile(io.BytesIO(data)).read('fabric.mod.json'))
    if descriptor['id'] != mod['modId'] or descriptor['version'] != mod['version']:
        raise ValueError('Optimization jar descriptor does not match pin')
    if not path.exists():
        temp = path.with_name(path.name + '.part')
        temp.write_bytes(data); temp.replace(path)
    return path


def stage_profile(profile_name, output, cache_dir, manifest_path=DEFAULT_MANIFEST):
    manifest = load_profiles(manifest_path)
    if profile_name not in manifest['profiles']: raise ValueError('Unknown optimization profile')
    profile = manifest['profiles'][profile_name]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'mods').mkdir()
    mods = [manifest['mods'][key] for key in profile['mods']]
    for mod in mods: shutil.copyfile(verified_mod(mod, cache_dir), output / 'mods' / mod['filename'])
    record = {'schemaVersion': 1, 'id': profile_name, 'minecraftVersion': manifest['minecraftVersion'],
              'loader': manifest['loader'], 'experimental': bool(mods), 'behaviorValidated': False if mods else True,
              'behaviorValidation': 'unvalidated' if mods else 'baseline',
              'trainingEligible': not bool(mods), 'manifestSha256': digest(Path(manifest_path)),
              'mods': [{**mod, 'sides': ['client', 'server']} for mod in mods],
              'requiredBehaviorScenarios': manifest['requiredBehaviorScenarios']}
    atomic_json(output / 'optimization-profile.json', record)
    return record


FINALIZE = '''import hashlib,json
from pathlib import Path
profile=json.loads(Path('/opt/optimization-profile.json').read_text())
for mod in profile['mods']:
 for base in ['/opt/game/mods','/opt/server/mods']:
  data=(Path(base)/mod['filename']).read_bytes()
  assert hashlib.sha512(data).hexdigest()==mod['sha512']
  assert hashlib.sha256(data).hexdigest()==mod['sha256']
versions=Path('/opt/game/versions.json')
value=json.loads(versions.read_text())
value['optimizationMods']=profile['mods']
value['optimizationProfile']=profile
versions.write_text(json.dumps(value)+'\\n')
info=Path('/opt/build-info.json')
value=json.loads(info.read_text())
value['optimizationBuild']=profile
info.write_text(json.dumps(value)+'\\n')
'''

GUARD = '''"""Experimental optimization runs are diagnostics until behavior validation is reviewed."""
import json,os,sys
from pathlib import Path
from conductor.worker import main
if __name__=='__main__':
 profile=json.loads(Path('/opt/optimization-profile.json').read_text())
 if profile.get('experimental') and not profile.get('behaviorValidated'):
  os.environ['MODE']='diagnostic'
 sys.exit(main())
'''


def numeric_version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+', value): raise ValueError('Unsupported version constraint')
    return tuple(int(part) for part in value.split('.'))


def build_profile(profile_name, base_image, tag, cache_dir, manifest_path=DEFAULT_MANIFEST, output=None):
    if not IMAGE.fullmatch(base_image) or not IMAGE.fullmatch(tag) or tag == base_image:
        raise ValueError('Use valid, distinct baseline and derivative image references')
    versions = json.loads(subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--entrypoint', 'python3', base_image, '-c',
        "from pathlib import Path; print(Path('/opt/game/versions.json').read_text())"], text=True))
    if versions['minecraft'] != '1.21.1': raise ValueError('Base image Minecraft version differs')
    if versions.get('optimizationMods'): raise ValueError('Base image must be an unmodified baseline profile')
    output = Path(output or ROOT / '.state/optimization-build' / (profile_name + '-' + uuid.uuid4().hex[:10]))
    record = stage_profile(profile_name, output, cache_dir, manifest_path)
    for mod in record['mods']:
        requirement = mod.get('dependencies', {}).get('fabricloader', '')
        if not requirement.startswith('>=') or numeric_version(versions['loader']) < numeric_version(requirement[2:]):
            raise ValueError('Base Fabric loader does not satisfy pinned mod requirement')
    record['baseImage'] = base_image
    record['baseImageId'] = subprocess.check_output(['docker', 'image', 'inspect', base_image, '--format', '{{.Id}}'], text=True).strip()
    atomic_json(output / 'optimization-profile.json', record)
    (output / 'finalize.py').write_text(FINALIZE)
    (output / 'guard.py').write_text(GUARD)
    lines = ['FROM ' + base_image, 'USER 0:0']
    for mod in record['mods']:
        for side in ['game', 'server']:
            lines.append('COPY ' + json.dumps(['mods/' + mod['filename'], '/opt/' + side + '/mods/' + mod['filename']]))
    lines.extend(['COPY --chmod=0444 optimization-profile.json /opt/optimization-profile.json',
                  'COPY finalize.py /opt/finalize-optimization.py',
                  'COPY guard.py /opt/conductor/optimization_guard.py',
                  'RUN python3 /opt/finalize-optimization.py',
                  'LABEL dev.vibrato.optimization.profile=' + json.dumps(profile_name),
                  'LABEL dev.vibrato.optimization.experimental=' + json.dumps(str(record['experimental']).lower()),
                  'USER 1000:1000', 'ENTRYPOINT ["python3","-m","conductor.optimization_guard"]'])
    (output / 'Dockerfile').write_text('\n'.join(lines) + '\n')
    subprocess.run(['docker', 'build', '--progress=plain', '-t', tag, str(output)], check=True)
    return {'profile': record, 'tag': tag, 'imageId': subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()}


def json_lines(path):
    if not path.exists(): return []
    rows = []
    with path.open() as stream:
        for line in stream:
            try: value = json.loads(line)
            except ValueError: continue
            if isinstance(value, dict): rows.append(value)
    return rows


def observation_sets(root):
    observed = {key: set() for key in ['items', 'blocks', 'mobs', 'packets', 'modes', 'permissions']}
    for heartbeat in json_lines(root / 'heartbeats.jsonl'):
        for kind, rule in [('items', 'inventory_present'), ('blocks', 'crosshair_target'), ('mobs', 'loaded_nearby')]:
            if heartbeat.get('observationSemantics', {}).get(kind) == rule:
                observed[kind].update(heartbeat.get('observations', {}).get(kind, []))
        if heartbeat.get('actualMode'): observed['modes'].add(heartbeat['actualMode'])
        if heartbeat.get('permissionSource') == 'server_observer': observed['permissions'].add(heartbeat.get('permissionLevel'))
    for index in (root / 'captures').glob('*.jsonl'):
        if index.name.endswith('.parsed.jsonl'): continue
        marker = index.with_suffix('.status.json')
        if not marker.exists() or not json.loads(marker.read_text()).get('complete'): continue
        with index.with_suffix('.bin').open('rb') as raw:
            for packet in json_lines(index):
                if 'off' not in packet: continue
                identifier = packet_id(raw, packet['off'], packet['len'])
                observed['packets'].add(f'{packet.get("state")}:{packet.get("dir")}:{identifier}')
    return {key: sorted(value) for key, value in observed.items()}


def scenario_name(metadata):
    if metadata.get('scenario'): return metadata['scenario']
    assignment = metadata.get('assignment', '')
    if assignment.startswith('@get '): return 'survival_resource'
    if assignment == '@nether': return 'nether'
    if assignment == '@gamer': return 'gamer'
    return 'autonomous' if assignment == 'director' else assignment


def summarize_run(path, monitor=None):
    root = Path(path)
    result = verify_manifest(root)
    metadata = json.loads((root / 'metadata.json').read_text())
    resources = json_lines(root / 'resources.jsonl')
    monitoring = json_lines(Path(monitor)) if monitor else []
    if monitoring:
        run_id = metadata.get('runId', root.name)
        limits = monitoring[0].get('runtimeLimits')
        if (not isinstance(limits, dict) or any(sample.get('runId') != run_id
                or sample.get('runtimeLimits') != limits for sample in monitoring)):
            raise ValueError('Monitoring must identify this run and consistent runtime limits')
    def numbers(key): return [r[key] for r in resources if isinstance(r.get(key), (int, float)) and math.isfinite(r[key])]
    def peak(key): return max(numbers(key), default=None)
    cpu = peak('cpuUsageSeconds')
    end_wall = max((r.get('wall', 0) for r in resources), default=0)
    measured_elapsed = end_wall - metadata.get('startedWall', end_wall)
    rss = [r['aggregateProcessRssBytes'] for r in monitoring if isinstance(r.get('aggregateProcessRssBytes'), (int, float))]
    versions = metadata.get('versions', {})
    profile = versions.get('optimizationProfile', {'id': 'baseline', 'experimental': False})
    scenario_path = root / 'scenario-result.json'
    scenario_result = json.loads(scenario_path.read_text()) if scenario_path.exists() else None
    context = {'scenario': scenario_name(metadata), 'assignment': metadata.get('assignment'), 'seed': metadata.get('seed'),
        'worldRevision': metadata.get('world', {}).get('revision'), 'worldId': metadata.get('world', {}).get('id'),
        'gamemode': metadata.get('assignedGamemode', metadata.get('serverProperties', {}).get('gamemode', 'survival')),
        'operator': metadata.get('operator', False), 'operatorLevel': metadata.get('operatorLevel', metadata.get('operatorPermissionLevel', 2)),
        'perf': metadata.get('perf'), 'minecraft': versions.get('minecraft'), 'loader': versions.get('loader'),
        'fabricApi': versions.get('fabricApi'), 'clientMod': versions.get('clientMod'), 'serverTelemetry': versions.get('serverTelemetry'),
        'workerSource': metadata.get('build', {}).get('inputs', {}).get('conductor/worker.py'),
        'runSeconds': metadata.get('runSeconds')}
    if monitoring: context['runtimeLimits'] = monitoring[0].get('runtimeLimits')
    metrics = {'serverMeanTps': result.get('serverHealth', {}).get('meanTps'),
        'clientMeanTps': result.get('clientHealth', {}).get('meanTps'), 'wholeWorkerCpuSeconds': cpu,
        'sampledCpuCoresAverage': cpu / measured_elapsed if cpu is not None and measured_elapsed > 0 else None,
        'cgroupPeakSampledMemoryBytes': peak('memoryBytes'),
        'cgroupReportedPeakMemoryBytes': max((int(r['memory.peak']) for r in resources if str(r.get('memory.peak', '')).isdigit()), default=None),
        'aggregateProcessRssPeakBytes': max(rss) if rss else None,
        'captureBytes': result.get('capture', {}).get('bytes'), 'capturePackets': result.get('capture', {}).get('packets'),
        'elapsedSeconds': result.get('elapsedSeconds')}
    return {'runId': result.get('runId', root.name), 'path': str(root.resolve()), 'manifestSha256': digest(root / 'manifest.json'),
        'context': context, 'optimizationProfile': profile, 'optimizationMods': versions.get('optimizationMods', []),
        'metrics': metrics, 'taskSuccess': result.get('taskSuccess'), 'diagnosticPassed': result.get('diagnosticPassed'),
        'serverHealth': result.get('serverHealth'), 'clientHealth': result.get('clientHealth'),
        'quality': quality_report(root), 'scenarioResult': scenario_result, 'observations': observation_sets(root),
        'rssLimitation': None if rss else 'Per-process RSS was not sampled; cgroup memory includes page cache and kernel memory'}


def comparison_report(baselines, candidates, manifest_path=DEFAULT_MANIFEST):
    manifest = load_profiles(manifest_path)
    pairs = []
    matrix = defaultdict(list)
    for candidate in candidates:
        comparable = [base for base in baselines if base['context'] == candidate['context']]
        base = comparable[0] if comparable else next((b for b in baselines if b['context']['scenario'] == candidate['context']['scenario']), None)
        mismatches = [] if base is None else [k for k in sorted(base['context'].keys() | candidate['context'].keys())
                                              if base['context'].get(k) != candidate['context'].get(k)]
        delta = {}
        differences = {}
        if base:
            for key, value in base['metrics'].items():
                other = candidate['metrics'].get(key)
                if isinstance(value, (int, float)) and isinstance(other, (int, float)):
                    delta[key] = {'baseline': value, 'candidate': other, 'difference': other - value,
                                  'percent': (other - value) * 100 / value if value else None}
            for kind in base['observations']:
                left, right = set(base['observations'][kind]), set(candidate['observations'][kind])
                differences[kind] = {'missingFromCandidate': sorted(left - right), 'additionalInCandidate': sorted(right - left)}
        healthy = bool(base and all(run.get('diagnosticPassed') is True and run.get('taskSuccess') is True
            and (run.get('serverHealth') or {}).get('valid') is True and (run.get('clientHealth') or {}).get('valid') is True
            for run in [base, candidate]))
        limits = candidate['context'].get('runtimeLimits') or {}
        limits_proven = all(isinstance(limits.get(key), (int, float)) and math.isfinite(limits[key])
                            and limits[key] > 0 for key in ('cpuCores', 'memoryBytes'))
        evidence = bool(healthy and not mismatches and limits_proven and candidate['optimizationMods'])
        scenario = candidate['context']['scenario']
        matrix[scenario].append(evidence)
        pairs.append({'baseline': base['runId'] if base else None, 'candidate': candidate['runId'],
                      'sameStartingConditionsAndSoftware': bool(base and not mismatches),
                      'contextMismatches': mismatches, 'authoritativeOutcomesBothSucceeded': healthy,
                      'matchedRuntimeLimitsProven': bool(base and not mismatches and limits_proven),
                      'metrics': delta, 'observationDifferences': differences,
                      'requiresBehaviorReview': True, 'evidenceReadyForScenarioReview': evidence})
    required = manifest['requiredBehaviorScenarios']
    missing = [scenario for scenario in required if not any(matrix.get(scenario, []))]
    return {'schemaVersion': 1, 'baselineRuns': baselines, 'candidateRuns': candidates, 'comparisons': pairs,
        'requiredBehaviorScenarios': required, 'missingBehaviorScenarios': missing,
        'behaviorEquivalent': False, 'behaviorValidation': 'unvalidated', 'trainingEligible': False,
        'readyForBehaviorReview': bool(candidates) and not missing,
        'limitations': ['Resource and timing improvements do not establish gameplay equivalence',
            'Observation differences may reflect nondeterminism and require scenario-specific review',
            'Matched CPU/memory limits require external monitoring evidence; absent limits remain unproven',
            'Complete physics, mob AI, redstone, container, dimension, and progression evidence is required before review']}


def sample_container(container, output, duration=180, interval=2):
    if not 1 <= duration <= 3600 or not 0.5 <= interval <= 60: raise ValueError('Invalid sampling interval or duration')
    info = json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))[0]
    if info.get('Config', {}).get('Labels', {}).get('dev.vibrato.conductor.worker') != 'true':
        raise ValueError('RSS sampling is restricted to owned conductor workers')
    runtime_limits = {'cpuCores': info['HostConfig'].get('NanoCpus', 0) / 1e9,
                      'memoryBytes': info['HostConfig'].get('Memory', 0)}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + duration
    with output.open('x') as stream:
        while time.monotonic() < deadline:
            try:
                table = subprocess.check_output(['docker', 'top', container, '-eo', 'pid,rss,comm'], text=True, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError: break  # completed container; preserve already collected samples
            processes = []
            for line in table.splitlines()[1:]:
                fields = line.split(None, 2)
                if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                    processes.append({'pid': int(fields[0]), 'rssBytes': int(fields[1]) * 1024, 'command': fields[2]})
            stream.write(json.dumps({'wall': time.time(), 'containerId': info['Id'],
                'runId': info['Config']['Labels'].get('dev.vibrato.conductor.run'), 'runtimeLimits': runtime_limits,
                'aggregateProcessRssBytes': sum(p['rssBytes'] for p in processes), 'processes': processes}) + '\n')
            stream.flush()
            time.sleep(interval)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    for command in ['stage', 'build']:
        item = commands.add_parser(command)
        item.add_argument('--profile', required=True)
        item.add_argument('--cache', type=Path, default=ROOT / '.state/optimization-downloads')
        item.add_argument('--output', type=Path, required=command == 'stage')
        if command == 'build':
            item.add_argument('--base-image', default='vibrato-worker:dev')
            item.add_argument('--tag', required=True)
    summary = commands.add_parser('summarize')
    summary.add_argument('run', type=Path); summary.add_argument('--monitor', type=Path)
    summary.add_argument('--output', type=Path)
    compare = commands.add_parser('compare')
    compare.add_argument('--baseline', type=Path, nargs='+', required=True)
    compare.add_argument('--candidate', type=Path, nargs='*', default=[])
    compare.add_argument('--baseline-monitor', type=Path, nargs='*', default=[])
    compare.add_argument('--candidate-monitor', type=Path, nargs='*', default=[])
    compare.add_argument('--output', type=Path, required=True)
    sampling = commands.add_parser('sample')
    sampling.add_argument('--container', required=True); sampling.add_argument('--output', type=Path, required=True)
    sampling.add_argument('--duration', type=float, default=180); sampling.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    if args.command == 'list':
        print(json.dumps(load_profiles(args.manifest), indent=2)); return 0
    if args.command == 'stage':
        value = stage_profile(args.profile, args.output, args.cache, args.manifest)
    elif args.command == 'build':
        value = build_profile(args.profile, args.base_image, args.tag, args.cache, args.manifest, args.output)
    elif args.command == 'sample':
        print(sample_container(args.container, args.output, args.duration, args.interval)); return 0
    elif args.command == 'summarize':
        value = summarize_run(args.run, args.monitor)
    else:
        for runs, monitors in [(args.baseline, args.baseline_monitor), (args.candidate, args.candidate_monitor)]:
            if monitors and len(monitors) != len(runs): raise ValueError('Supply one monitor per run, in the same order')
        value = comparison_report(
            [summarize_run(p, args.baseline_monitor[i] if args.baseline_monitor else None) for i, p in enumerate(args.baseline)],
            [summarize_run(p, args.candidate_monitor[i] if args.candidate_monitor else None) for i, p in enumerate(args.candidate)], args.manifest)
    if getattr(args, 'output', None) and args.command not in ('stage', 'build'):
        atomic_json(args.output, value)
    print(json.dumps(value, indent=2))
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
