#!/usr/bin/env python3
"""Build a Protocol Transformer dataset from current, independently admitted captures."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conductor.training import export_index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--central', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New output directory; never overwritten')
    parser.add_argument('--transformer', type=Path, default=Path('/opt/Protocol-Transformer'))
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--run-id', action='append', help='Restrict to selected central runs; repeat for each')
    parser.add_argument('--runtime-image', help='Use Node and proxy dependencies from this local worker image')
    args = parser.parse_args()
    if args.jobs < 1: parser.error('--jobs must be positive')
    output = args.output.resolve()
    if output.exists(): parser.error('output already exists; choose a new dataset directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    builder = args.transformer.resolve() / 'reader/dataset/dataset.js'
    if not builder.is_file(): parser.error('Protocol Transformer dataset builder missing')
    with tempfile.TemporaryDirectory(prefix='.dataset-build-', dir=output.parent) as tmp:
        root = Path(tmp)
        before = export_index(args.central, root / 'source-index.json', args.run_id)
        if args.run_id and before['rejected']:
            raise ValueError('A selected run is not currently admitted; inspect export rejections before retrying')
        if not before['split']['ready']:
            raise ValueError('Need eligible captures from at least two distinct world seeds for train and validation')
        stage = root / 'dataset'
        builder_args = ['build', '--out', str(stage), '--augment', '3', '--jobs', str(args.jobs),
                        '--val', ','.join(before['validationBases']), *before['trainBases']]
        if args.runtime_image:
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
        after = export_index(args.central, root / 'source-index-after.json', args.run_id)
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
