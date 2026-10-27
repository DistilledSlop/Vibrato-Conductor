"""Durable filesystem artifacts and verified isolated world copies."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _regular_tree(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f'Missing or symlinked directory: {root}')
    entries = sorted(root.rglob('*'))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in entries):
        raise ValueError('Storage trees must contain regular files and directories only')
    return [p for p in entries if p.is_file()]


def world_revision(source: Path) -> dict:
    """Fingerprint the complete bank world, ignoring only the transient server lock."""
    files = _regular_tree(source)
    if not (source / 'level.dat').is_file():
        raise ValueError('Bank world must contain level.dat')
    records = [{'path': p.relative_to(source).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p)}
               for p in files if p.name != 'session.lock']
    canonical = json.dumps(records, separators=(',', ':'), sort_keys=True).encode()
    return {'revision': hashlib.sha256(canonical).hexdigest(), 'files': records,
            'fileCount': len(records), 'logicalBytes': sum(r['bytes'] for r in records)}


def inspect_world_bank(bank: Path) -> list[dict]:
    worlds = []
    if not bank.is_dir(): return worlds
    for entry in sorted(bank.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or not IDENTIFIER.fullmatch(entry.name): continue
        try:
            revision = world_revision(entry / 'world')
            config_path = entry / 'world.json'
            if config_path.is_symlink(): raise ValueError('Symlinked world metadata')
            config = json.loads(config_path.read_text()) if config_path.exists() else {}
            if not isinstance(config, dict): raise ValueError('World metadata must be an object')
            gamemodes = config.get('gamemodes', ['survival', 'creative', 'adventure', 'spectator'])
            if (not isinstance(gamemodes, list) or not gamemodes
                    or any(mode not in ('survival', 'creative', 'adventure', 'spectator') for mode in gamemodes)):
                raise ValueError('World gamemodes must be an explicit nonempty list of valid modes')
            tags = config.get('tags', [])
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise ValueError('World tags must be a string list')
            if not isinstance(config.get('allowOperator', True), bool): raise ValueError('allowOperator must be boolean')
            expected = config.get('revision')
            if expected and expected != revision['revision']: raise ValueError('World revision mismatch')
            worlds.append({'id': entry.name, 'revision': revision['revision'], 'logicalBytes': revision['logicalBytes'],
                           'fileCount': revision['fileCount'], 'tags': tags, 'gamemodes': gamemodes,
                           'allowOperator': config.get('allowOperator', True),
                           'minecraftVersion': config.get('minecraftVersion', '1.21.1'),
                           'seed': config.get('seed'), 'supported': True})
        except (OSError, ValueError, TypeError) as error:
            worlds.append({'id': entry.name, 'supported': False, 'error': str(error), 'tags': []})
    return worlds


def stage_world(bank: Path, world_id: str, dest: Path, allow_copy: bool = False) -> dict:
    if not IDENTIFIER.fullmatch(world_id): raise ValueError('Invalid world id')
    source = bank / world_id / 'world'
    if source.parent.is_symlink(): raise ValueError('Symlinked bank world')
    if dest.exists() or dest.is_symlink(): raise ValueError('Mutable world destination already exists')
    before = world_revision(source)
    metadata_path = source.parent / 'world.json'
    if metadata_path.is_symlink(): raise ValueError('Symlinked world metadata')
    metadata_bytes = metadata_path.read_bytes() if metadata_path.exists() else None
    metadata = json.loads(metadata_bytes) if metadata_bytes is not None else {}
    if not isinstance(metadata, dict): raise ValueError('World metadata must be an object')
    if metadata.get('revision') and metadata['revision'] != before['revision']:
        raise ValueError('World revision mismatch')
    result = subprocess.run(['cp', '-R', '--reflink=always', '--no-preserve=mode,ownership',
                             str(source), str(dest)], capture_output=True, text=True)
    mode = 'reflink'
    if result.returncode:
        if dest.exists(): shutil.rmtree(dest)
        if not allow_copy:
            raise RuntimeError(f'Reflinks unavailable; full copy requires ALLOW_WORLD_COPY=true ({before["logicalBytes"]} bytes): {result.stderr.strip()}')
        shutil.copytree(source, dest, copy_function=shutil.copyfile)
        for directory in [dest, *(p for p in dest.rglob('*') if p.is_dir())]:
            directory.chmod(directory.stat().st_mode | 0o700)
        mode = 'full-copy'
    # Both sides are checked: a bank mutation or incomplete copy must not silently change starting conditions.
    after = world_revision(source)
    copied = world_revision(dest)
    metadata_after = metadata_path.read_bytes() if metadata_path.exists() else None
    if (before['revision'] != after['revision'] or copied['revision'] != before['revision']
            or metadata_path.is_symlink() or metadata_after != metadata_bytes):
        shutil.rmtree(dest)
        raise RuntimeError('World bank changed during copying or mutable copy failed verification')
    (dest / 'session.lock').unlink(missing_ok=True)
    return {'id': world_id, 'mode': mode, 'logicalBytes': before['logicalBytes'],
            'levelDatSha256': digest(source / 'level.dat'), 'revision': before['revision'],
            'fileCount': before['fileCount'],
            # Existing-world seeds are authoritative; runtime level-seed does not replace them.
            'sourceSeed': metadata.get('seed'), 'sourceGameType': metadata.get('sourceGameType'),
            'minecraftVersion': metadata.get('minecraftVersion'),
            'sourceMetadataSha256': hashlib.sha256(metadata_bytes).hexdigest() if metadata_bytes is not None else None}


def artifact_manifest(root: Path, result: dict) -> dict:
    records = []
    for path in _regular_tree(root):
        relative = path.relative_to(root).as_posix()
        if relative == 'manifest.json' or relative == 'manifest.json.tmp': continue
        records.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': digest(path)})
    return {'schemaVersion': 1, 'result': result, 'files': records}


def verify_manifest(root: Path, strict: bool = True) -> dict:
    """Verify an immutable finalized run without following manifest-controlled paths."""
    files = _regular_tree(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('schemaVersion') != 1 or not isinstance(manifest.get('files'), list):
        raise ValueError('Unsupported or missing artifact manifest')
    seen = set()
    for record in manifest['files']:
        relative = record.get('path', '')
        path = Path(relative)
        if (not relative or path.is_absolute() or '..' in path.parts or relative in seen
                or relative in ('manifest.json', 'delivery.json', 'delivery-receipt.json')):
            raise ValueError('Unsafe or duplicate artifact manifest path')
        seen.add(relative)
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe artifact manifest path')
        if not target.is_file() or target.stat().st_size != record['bytes'] or digest(target) != record['sha256']:
            raise ValueError(f'Artifact verification failed: {relative}')
    if strict:
        actual = {p.relative_to(root).as_posix() for p in files}
        extra = actual - seen - {'manifest.json', 'delivery.json', 'delivery-receipt.json'}
        if extra: raise ValueError('Unlisted artifact files: ' + ', '.join(sorted(extra)[:5]))
    result = manifest.get('result')
    if not isinstance(result, dict): raise ValueError('Missing artifact result')
    # A copied/tampered result file must not disagree with the finalized manifest.
    result_path = root / 'result.json'
    if result_path.exists() and json.loads(result_path.read_text()) != result:
        raise ValueError('Manifest and result.json disagree')
    return result
