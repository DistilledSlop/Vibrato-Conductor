#!/usr/bin/env python3
"""Stage explicit inputs from sibling projects; no saves, credentials, or captures enter the image."""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vibrato', type=Path, default=ROOT.parent / 'Vibrato')
    parser.add_argument('--proxy', type=Path, default=ROOT.parent / 'Protocol-Transformer')
    parser.add_argument('--tag', default='vibrato-worker:dev')
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    context = ROOT / '.state' / 'build-context'
    context.mkdir(parents=True, exist_ok=True)
    jars = [p for p in (args.vibrato / 'build/libs').glob('vibrato-*.jar')
            if not p.name.endswith(('-sources.jar', '-dev.jar'))]
    if len(jars) != 1:
        parser.error('Build Vibrato first; build/libs must contain exactly one production jar')
    telemetry_jars = [p for p in (args.vibrato / 'conductor-telemetry/build/libs').glob('vibrato-conductor-telemetry-*.jar')
                      if not p.name.endswith(('-sources.jar', '-dev.jar'))]
    if len(telemetry_jars) != 1:
        parser.error('Build Vibrato including conductor-telemetry before building the worker')
    info = {}
    for name, repo in [('vibrato', args.vibrato), ('proxy', args.proxy), ('conductor', ROOT)]:
        info[name] = {
            'commit': subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(),
            'dirty': bool(subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], text=True)),
        }
    inputs = {
        'vibrato/gradle.properties': args.vibrato / 'gradle.properties',
        'vibrato/scripts/fleet/launch.py': args.vibrato / 'scripts/fleet/launch.py',
        f'vibrato/build/libs/{jars[0].name}': jars[0],
        f'vibrato/build/server-mods/{telemetry_jars[0].name}': telemetry_jars[0],
        'Dockerfile': ROOT / 'worker/Dockerfile', 'install.py': ROOT / 'worker/install.py',
    }
    for name in ('index.js', 'recorder.js', 'sanitize.js', 'wire.js', 'package.json', 'package-lock.json'):
        inputs['proxy/' + name] = args.proxy / 'proxy' / name
    for source in (ROOT / 'conductor').glob('*.py'):
        inputs['conductor/' + source.name] = source
    # Remove obsolete staged files so old assets cannot leak into subsequent builds.
    for old in sorted(context.rglob('*'), reverse=True):
        if old.is_file(): old.unlink()
        elif old.is_dir(): old.rmdir()
    info['inputs'] = {}
    for target, source in inputs.items():
        dest = context / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        info['inputs'][target] = hashlib.sha256(dest.read_bytes()).hexdigest()
    (context / 'build-info.json').write_text(json.dumps(info, indent=2) + '\n')
    print(context, flush=True)
    if not args.stage_only:
        subprocess.run(['docker', 'build', '--progress=plain', '-t', args.tag, str(context)], check=True)

if __name__ == '__main__': main()
