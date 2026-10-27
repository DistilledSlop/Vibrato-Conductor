"""Filesystem artifacts and isolated world copies. Filesystem mounts can be local or shared storage."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def atomic_json(path: Path, data: object) -> None:
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def stage_world(bank: Path, world_id: str, dest: Path, allow_copy: bool = False) -> dict:
    source = bank / world_id / 'world'
    # Do not follow symlinks into another world or host location.
    if not source.is_dir() or source.is_symlink() or source.parent.is_symlink():
        raise ValueError(f'Missing or symlinked bank world: {source}')
    entries = list(source.rglob('*'))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in entries):
        raise ValueError('World bank must contain regular files and directories only')
    if not (source / 'level.dat').is_file():
        raise ValueError('Bank world must contain level.dat')
    size = sum(p.stat().st_size for p in entries if p.is_file())
    result = subprocess.run(['cp', '-R', '--reflink=always', '--no-preserve=mode,ownership',
                             str(source), str(dest)], capture_output=True, text=True)
    mode = 'reflink'
    if result.returncode:
        if dest.exists(): shutil.rmtree(dest)
        if not allow_copy:
            raise RuntimeError(f'Reflinks unavailable; full copy requires ALLOW_WORLD_COPY=true ({size} bytes): {result.stderr.strip()}')
        shutil.copytree(source, dest, copy_function=shutil.copyfile)
        # copytree preserves directory modes, including read-only bank directories.
        for directory in [dest, *(p for p in dest.rglob('*') if p.is_dir())]:
            directory.chmod(directory.stat().st_mode | 0o700)
        mode = 'full-copy'
    # Clear only the copied server's stale lock; the bank stays untouched.
    (dest / 'session.lock').unlink(missing_ok=True)
    return {'id': world_id, 'mode': mode, 'logicalBytes': size, 'levelDatSha256': digest(source / 'level.dat')}


def artifact_manifest(root: Path, result: dict) -> dict:
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.name not in ('manifest.json', 'manifest.json.tmp'):
            files.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': digest(path)})
    return {'schemaVersion': 1, 'result': result, 'files': files}
