"""Durable filesystem artifacts and verified isolated world copies."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
import stat
import tempfile
import uuid
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


def validate_earned_template(metadata: dict, revision: str) -> dict | None:
    template = metadata.get('earnedContextTemplate')
    if template is None: return None
    if not isinstance(template, dict) or metadata.get('resumeProvenance') is not None:
        raise ValueError('Invalid or conflicting earned context template')
    if template.get('kind') != 'earned_inventory_sort_v1':
        raise ValueError('Unsupported earned context template')
    source_uuid = template.get('sourceUuid')
    try:
        if not isinstance(source_uuid, str) or str(uuid.UUID(source_uuid)) != source_uuid: raise ValueError()
    except ValueError:
        raise ValueError('Invalid template source UUID') from None
    if (not IDENTIFIER.fullmatch(str(template.get('sourceRun', '')))
            or not re.fullmatch('[0-9a-f]{64}', str(template.get('sourceManifestSha256', '')))
            or not re.fullmatch('[0-9a-f]{64}', str(template.get('sourceAdmissionSha256', '')))
            or not re.fullmatch('[0-9a-f]{64}', str(template.get('sourceWorldMetadataSha256', '')))
            or template.get('sourceWorldRevision') != revision):
        raise ValueError('Earned template source provenance or revision invalid')
    if (metadata.get('tags') != ['inventory-sort-kit'] or metadata.get('gamemodes') != ['survival']
            or metadata.get('allowOperator') is not False or metadata.get('allowedTasks') != ['mechanic-organize-inventory']):
        raise ValueError('Earned inventory template must require survival and non-operator')
    proof = template.get('sortProof')
    if (not isinstance(proof, dict) or isinstance(proof.get('sourceSlot'), bool)
            or not isinstance(proof.get('sourceSlot'), int) or not 0 <= proof['sourceSlot'] < 36
            or isinstance(proof.get('hotbarSlot'), bool) or proof.get('hotbarSlot') != 1
            or proof['sourceSlot'] == 1 or not isinstance(proof.get('item'), str)
            or not re.fullmatch(r'minecraft:(wooden|stone|iron|golden|diamond|netherite)_pickaxe', proof['item'])
            or isinstance(proof.get('count'), bool) or proof.get('count') != 1 or proof.get('hotbarEmpty') is not True):
        raise ValueError('Earned inventory sort proof invalid')
    return template


def validate_earned_evidence(entry: Path, template: dict, current_policy: int | None = None) -> None:
    for name, key in [('source-run-manifest.json','sourceManifestSha256'),
                      ('source-admission-receipt.json','sourceAdmissionSha256'),
                      ('source-original-world.json','sourceWorldMetadataSha256')]:
        proof_path = entry / name
        if proof_path.is_symlink() or not proof_path.is_file() or digest(proof_path) != template[key]:
            raise ValueError('Earned template evidence missing or mismatched: ' + name)
    receipt = json.loads((entry/'source-admission-receipt.json').read_text())
    quality = receipt.get('quality') or {}
    if (receipt.get('runId') != template['sourceRun'] or receipt.get('manifestSha256') != template['sourceManifestSha256']
            or quality.get('eligible') is not True or (current_policy is not None and quality.get('policyVersion') != current_policy)):
        raise ValueError('Earned template source receipt is inconsistent or stale')


def _world_tree_signature(root: Path) -> tuple:
    """Stat every entry; reject symlinks and non-regular types before cache use."""
    if root.is_symlink() or not root.is_dir(): raise ValueError('Invalid bank world directory')
    records = []
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('Storage trees must contain regular files and directories only')
        records.append((str(path.relative_to(root)), info.st_dev, info.st_ino, info.st_mode,
                        info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return tuple(records)


def _cached_world_revision(root: Path, cache: dict) -> dict:
    signature = _world_tree_signature(root)
    cached = cache.get(root)
    if cached is not None and cached[0] == signature: return cached[1]
    revision = world_revision(root)
    if _world_tree_signature(root) != signature:
        cache.pop(root, None)
        raise ValueError('Bank world changed during inspection')
    cache[root] = (signature, revision)
    return revision


def inspect_world_bank(bank: Path, revision_cache: dict | None = None) -> list[dict]:
    worlds = []
    if not bank.is_dir():
        if revision_cache is not None: revision_cache.clear()
        return worlds
    for entry in sorted(bank.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or not IDENTIFIER.fullmatch(entry.name): continue
        try:
            revision = (world_revision(entry / 'world') if revision_cache is None
                        else _cached_world_revision(entry / 'world', revision_cache))
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
            template = validate_earned_template(config, revision['revision'])
            if template is not None: validate_earned_evidence(entry, template)
            allowed_tasks = config.get('allowedTasks')
            if allowed_tasks is not None and (not isinstance(allowed_tasks, list) or not allowed_tasks
                    or any(not isinstance(task, str) or not IDENTIFIER.fullmatch(task) for task in allowed_tasks)):
                raise ValueError('World allowedTasks must be a nonempty list of task ids')
            expected = config.get('revision')
            if expected and expected != revision['revision']: raise ValueError('World revision mismatch')
            worlds.append({'id': entry.name, 'revision': revision['revision'], 'logicalBytes': revision['logicalBytes'],
                           'fileCount': revision['fileCount'], 'tags': tags, 'gamemodes': gamemodes, 'allowedTasks': allowed_tasks,
                           'earnedContext': ({'sourceRun': template['sourceRun'], 'sourceManifestSha256': template['sourceManifestSha256']} if template else None),
                           'allowOperator': config.get('allowOperator', True),
                           'minecraftVersion': config.get('minecraftVersion', '1.21.1'),
                           'seed': config.get('seed'), 'supported': True})
        except (OSError, ValueError, TypeError) as error:
            worlds.append({'id': entry.name, 'supported': False, 'error': str(error), 'tags': []})
    if revision_cache is not None:
        present = {entry / 'world' for entry in bank.iterdir() if entry.is_dir() and not entry.is_symlink()}
        for path in list(revision_cache):
            if path not in present: revision_cache.pop(path, None)
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
    template = validate_earned_template(metadata, before['revision'])
    if template is not None: validate_earned_evidence(source.parent, template)
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
