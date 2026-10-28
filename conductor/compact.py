"""Controller-produced compact training records and their external validation binding.

Full evidence is assessed before this projection is made. A compact record cannot
be re-assessed under a newer policy that requires evidence discarded here.
"""
from __future__ import annotations

import json
import copy
from collections import Counter, OrderedDict
import shutil
import stat
import threading
from pathlib import Path

from .storage import artifact_manifest, atomic_json, digest, verify_manifest
from .lineage import lineage_admitted

SCHEMA_VERSION = 1
_CACHE_LIMIT = 1024
_static_cache = OrderedDict()
_cache_lock = threading.RLock()


def _stat_signature(path: Path, *, directory: bool = False) -> tuple:
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Unsafe compact record path')
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _tree_signature(root: Path) -> tuple:
    # lstat every entry, including directories, so new files and symlinks cannot
    # hide behind a cached manifest validation. Never follow a symlinked tree.
    entries = [(str(Path('.')), _stat_signature(root, directory=True))]
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('Unsafe compact record tree')
        entries.append((path.relative_to(root).as_posix(),
                        (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns)))
    return tuple(entries)

# These fields are consumed by coverage ingestion and assignment validation.
_HEARTBEAT_FIELDS = frozenset(('rootClass', 'actualMode', 'abilities', 'permissionLevel',
    'permissionSource', 'observationSemantics', 'observations'))
_KEEP = ('metadata.json', 'result.json', 'script-result.json', 'activity-result.json',
         'scenario-result.json', 'packet-coverage.json', 'source-world-metadata.json',
         'earned-context-migration.json')


def _project_coverage(source: Path, destination: Path) -> None:
    counts = Counter()
    if source.exists():
        with source.open() as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except ValueError:
                    counts['!malformed'] = 1
                    continue
                if isinstance(value, dict):
                    value = {key: value[key] for key in _HEARTBEAT_FIELDS if key in value}
                key = json.dumps(value, sort_keys=True, separators=(',', ':'))
                counts[key] += 1
    atomic_json(destination, {'schemaVersion': 1,
                'rows': [{'value': key, 'count': count} for key, count in sorted(counts.items())]})


def coverage_lines(path: Path):
    projection = json.loads(path.read_text())
    if projection.get('schemaVersion') != 1 or not isinstance(projection.get('rows'), list):
        raise ValueError('Unsupported coverage projection')
    for row in projection['rows']:
        value, count = row.get('value'), row.get('count')
        if not isinstance(value, str) or not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError('Invalid coverage projection')
        for _ in range(count):
            yield value


def inspect_compact(root: Path) -> dict:
    root = Path(root)
    from .artifacts import QUALITY_POLICY_VERSION
    result = verify_manifest(root)
    evidence = json.loads((root / 'validation-evidence.json').read_text())
    source_hash = digest(root / 'source-manifest.json')
    source_manifest = json.loads((root / 'source-manifest.json').read_text())
    if (evidence.get('schemaVersion') != SCHEMA_VERSION
            or evidence.get('policyVersion') != QUALITY_POLICY_VERSION
            or evidence.get('sourceManifestSha256') != source_hash
            or evidence.get('quality', {}).get('policyVersion') != QUALITY_POLICY_VERSION
            or evidence.get('quality', {}).get('eligible') is not True
            or source_manifest.get('result') != result):
        raise ValueError('Invalid compact validation evidence')
    return {'sourceManifestSha256': source_hash,
            'compactManifestSha256': digest(root / 'manifest.json'),
            'evidenceSha256': digest(root / 'validation-evidence.json'), 'quality': evidence['quality']}


def build_compact(source: Path, stage: Path, admission_root: Path | None) -> dict:
    """Project an independently admitted full source into a new, unpublished tree."""
    from .artifacts import quality_report, QUALITY_POLICY_VERSION
    source, stage = Path(source), Path(stage)
    result = verify_manifest(source)
    initial_hash = digest(source / 'manifest.json')
    quality = quality_report(source, admission_root)
    if quality.get('eligible') is not True:
        raise ValueError('Cannot compact an ineligible run as training data')
    if stage.exists():
        raise ValueError('Compact staging directory already exists')
    stage.mkdir(parents=True)
    for name in _KEEP:
        src = source / name
        if src.is_file():
            shutil.copyfile(src, stage / name)
    capture_dest = stage / 'captures'
    capture_dest.mkdir()
    for src in (source / 'captures').iterdir():
        if src.is_file() and (src.suffix == '.bin' or
                (src.suffix == '.jsonl' and not src.name.endswith('.parsed.jsonl')) or
                src.name.endswith('.status.json')):
            shutil.copyfile(src, capture_dest / src.name)
    _project_coverage(source / 'heartbeats.jsonl', stage / 'coverage-projection.json')
    shutil.copyfile(source / 'manifest.json', stage / 'source-manifest.json')
    source_hash = digest(stage / 'source-manifest.json')
    if source_hash != initial_hash or digest(source / 'manifest.json') != initial_hash:
        raise ValueError('Source manifest changed during compaction')
    verify_manifest(source)
    source_records = {row['path']: row for row in json.loads((source / 'manifest.json').read_text())['files']}
    for path in stage.rglob('*'):
        if path.is_file() and path.name not in ('source-manifest.json', 'coverage-projection.json'):
            relative = path.relative_to(stage).as_posix()
            record = source_records.get(relative)
            if not record or digest(path) != record['sha256'] or path.stat().st_size != record['bytes']:
                raise ValueError('Compact copy differs from source: ' + relative)
    evidence = {'schemaVersion': SCHEMA_VERSION, 'policyVersion': QUALITY_POLICY_VERSION,
                'sourceManifestSha256': source_hash, 'quality': quality}
    atomic_json(stage / 'validation-evidence.json', evidence)
    atomic_json(stage / 'manifest.json', artifact_manifest(stage, result))
    verify_manifest(stage)
    return {'sourceManifestSha256': source_hash, 'compactManifestSha256': digest(stage / 'manifest.json'),
            'evidenceSha256': digest(stage / 'validation-evidence.json'), 'quality': quality}


def compact_quality(root: Path, admission_root: Path | None) -> dict:
    """Verify static compact content once per file identity, and live lineage always."""
    from .artifacts import QUALITY_POLICY_VERSION, capture_integrity
    root = Path(root)
    if admission_root is None:
        raise ValueError('Compact record requires external admission receipt')
    admission_root = Path(admission_root)
    receipt_path = admission_root / (root.name + '.json')
    tree_signature = _tree_signature(root)
    receipt_signature = _stat_signature(receipt_path)
    receipt = json.loads(receipt_path.read_text())
    key = (str(root.absolute()), str(receipt_path.absolute()), QUALITY_POLICY_VERSION,
           tree_signature, receipt_signature)
    with _cache_lock:
        cached = _static_cache.get(key)
        if cached is not None:
            _static_cache.move_to_end(key)
            metadata, quality = copy.deepcopy(cached)
    if cached is None:
        result = verify_manifest(root)
        metadata = json.loads((root / 'metadata.json').read_text())
        evidence = json.loads((root / 'validation-evidence.json').read_text())
        quality = evidence.get('quality')
        if (evidence.get('schemaVersion') != SCHEMA_VERSION or evidence.get('policyVersion') != QUALITY_POLICY_VERSION
                or not isinstance(quality, dict) or quality.get('policyVersion') != QUALITY_POLICY_VERSION
                or quality.get('eligible') is not True or result.get('runId') != root.name
                or metadata.get('runId') != root.name or receipt.get('runId') != root.name
                or receipt.get('compactSchemaVersion') != SCHEMA_VERSION
                or receipt.get('manifestSha256') != digest(root / 'manifest.json')
                or receipt.get('sourceManifestSha256') != digest(root / 'source-manifest.json')
                or evidence.get('sourceManifestSha256') != receipt.get('sourceManifestSha256')
                or receipt.get('evidenceSha256') != digest(root / 'validation-evidence.json')
                or receipt.get('originalQuality') != quality):
            raise ValueError('Compact admission evidence is missing, stale or inconsistent')
        binding = inspect_compact(root)
        if binding['sourceManifestSha256'] != receipt['sourceManifestSha256']:
            raise ValueError('Compact source manifest is inconsistent')
        if not capture_integrity(root) or result.get('capture', {}).get('complete') is not True:
            raise ValueError('Compact capture integrity failed')
        if _tree_signature(root) != tree_signature or _stat_signature(receipt_path) != receipt_signature:
            raise ValueError('Compact record changed during verification')
        with _cache_lock:
            _static_cache[key] = copy.deepcopy((metadata, quality))
            _static_cache.move_to_end(key)
            while len(_static_cache) > _CACHE_LIMIT:
                _static_cache.popitem(last=False)
    current = receipt.get('quality')
    if (not isinstance(current, dict) or current.get('policyVersion') != QUALITY_POLICY_VERSION
            or not isinstance(current.get('eligible'), bool)
            or not isinstance(current.get('reasons'), list)
            or (current.get('eligible') is True and current.get('reasons') != [])
            or any(not isinstance(reason, str) for reason in current.get('reasons', []))):
        raise ValueError('Compact current admission is missing, stale or inconsistent')
    def unchanged():
        if _tree_signature(root) != tree_signature or _stat_signature(receipt_path) != receipt_signature:
            raise ValueError('Compact record changed during verification')
    # Current receipt and the entire earned ancestry are never cached.
    if current['eligible'] is not True:
        unchanged()
        return copy.deepcopy(current)
    world = metadata.get('world') or {}
    if isinstance(world, dict) and world.get('earnedContext') is not None:
        if not lineage_admitted(admission_root, world['earnedContext']):
            unchanged()
            return {**quality, 'eligible': False,
                    'reasons': sorted(set(quality.get('reasons', []) + ['earned_context_source_unconfirmed']))}
    unchanged()
    return copy.deepcopy(quality)
