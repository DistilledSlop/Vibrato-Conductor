#!/usr/bin/env python3
"""Build a Protocol Transformer dataset from current, independently admitted captures."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRANSFORMER = Path('/opt/Protocol-Transformer')
sys.path.insert(0, str(REPO_ROOT))
from conductor.training import export_index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--central', type=Path, default=REPO_ROOT / '.state/gallery/dataset', help='Central admitted capture store (default: repository .state/gallery/dataset)')
    parser.add_argument('--output', type=Path, help='New output directory; default: repository .state/training-<timestamp>-<unique id>; never overwritten')
    parser.add_argument('--transformer', type=Path, default=DEFAULT_TRANSFORMER)
    parser.add_argument('--jobs', type=int, default=48, help='Parallel preprocessing workers (default: 48)')
    parser.add_argument('--index-jobs', type=int, default=min(8, len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1), help='Parallel admission checks (default: up to 8)')
    parser.add_argument('--run-id', action='append', help='Restrict to selected central runs; repeat for each')
    runtime = parser.add_mutually_exclusive_group()
    runtime.add_argument('--runtime-image', default='vibrato-worker:dev', help='Use Node and proxy dependencies from this local worker image (default: vibrato-worker:dev)')
    runtime.add_argument('--local-runtime', action='store_true', help='Use local Node and reader dependencies instead of a worker image')
    args = parser.parse_args()
    if args.jobs is not None and args.jobs < 1: parser.error('--jobs must be positive')
    if args.index_jobs < 1: parser.error('--index-jobs must be positive')
    output = (args.output or REPO_ROOT / '.state' / f"training-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:12]}").resolve()
    if output.exists(): parser.error('output already exists; choose a new dataset directory')
    runtime_label = 'local Node' if args.local_runtime else args.runtime_image
    print(f'Building dataset: central={args.central.resolve()} output={output} runtime={runtime_label} jobs={args.jobs} index_jobs={args.index_jobs}', file=sys.stderr, flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    builder = args.transformer.resolve() / 'reader/dataset/dataset.js'
    if not builder.is_file(): parser.error('Protocol Transformer dataset builder missing')
    with tempfile.TemporaryDirectory(prefix='.dataset-build-', dir=output.parent) as tmp:
        root = Path(tmp)
        before = export_index(args.central, root / 'source-index.json', args.run_id, jobs=args.index_jobs)
        # Freeze the complete initial scan, including rejected runs, while collection continues.
        selected = args.run_id
        if selected is None:
            selected = sorted(run['runId'] for run in before['accepted'] + before['rejected'])
            before['selectedRunIds'] = selected
        if args.run_id and before['rejected']:
            raise ValueError('A selected run is not currently admitted; inspect export rejections before retrying')
        if not before['split']['ready']:
            raise ValueError('Need eligible captures from at least two distinct world seeds for train and validation')
        stage = root / 'dataset'
        builder_args = ['build', '--out', str(stage), '--augment', '3',
                        *(['--jobs', str(args.jobs)] if args.jobs is not None else []),
                        '--val', ','.join(before['validationBases']), *before['trainBases']]
        if not args.local_runtime:
            central = args.central.resolve(strict=True)
            reader = builder.parent.parent.resolve(strict=True)
            if any(',' in str(path) for path in (central, root, reader)):
                raise ValueError('Docker mount paths cannot contain commas')
            command = ('mkdir -p /tmp/transformer/proxy && cp -a /source-reader /tmp/transformer/reader '
                       '&& ln -s /opt/proxy/node_modules /tmp/transformer/proxy/node_modules '
                       '&& exec node /tmp/transformer/reader/dataset/dataset.js "$@"')
            argv = ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/bin/sh',
                    '--mount', f'type=bind,src={reader},dst=/source-reader,readonly',
                    '--mount', f'type=bind,src={central},dst={central},readonly',
                    '--mount', f'type=bind,src={root},dst={root}', args.runtime_image,
                    '-c', command, 'dataset-builder', *builder_args]
        else:
            argv = ['node', str(builder), *builder_args]
        subprocess.run(argv, check=True)
        after = export_index(args.central, root / 'source-index-after.json', selected, jobs=args.index_jobs)
        if before != after:
            raise ValueError('Admission or capture set changed during preprocessing; retry with fresh inputs')
        manifest_path = stage / 'manifest.json'
        dataset_manifest = json.loads(manifest_path.read_text())
        dataset_manifest['vocab'] = str(output / 'vocab.json')
        manifest_path.write_text(json.dumps(dataset_manifest, indent=2) + '\n')
        (stage / 'val.txt').write_text(''.join(name + '\n' for name in before['validationSessionNames']))
        (stage / 'source-index.json').write_text(json.dumps(before, indent=2) + '\n')
        # Prevent another process from replacing an existing dataset.
        if output.exists(): raise ValueError('Output appeared during build; refusing to overwrite')
        os.rename(stage, output)
    print(json.dumps({'dataset': str(output), 'trainSessions': len(before['trainBases']),
                      'validationSessions': len(before['validationBases']), 'trainingStarted': False}, indent=2))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error: raise SystemExit(str(error))
