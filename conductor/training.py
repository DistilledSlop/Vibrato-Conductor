"""Bring independently admitted runs into delivery and describe trainer inputs.

Raw runs and admission receipts remain immutable inputs; this module never runs
preprocessing or training and never treats a worker verdict as admission.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import sqlite3
from pathlib import Path

from .artifacts import ArtifactStore, QUALITY_POLICY_VERSION, quality_report
from .coverage import CoverageInventory
from .fleet import FleetConfig
from .lineage import source_admitted
from .backends import BackendError
from .history import migration_workers
from .storage import IDENTIFIER, atomic_json, digest, verify_manifest


def import_run(source: Path, config_path: Path) -> dict:
    config_data = json.loads(Path(config_path).read_text())
    service_keys = {'max_buffer_bytes', 'enabled_tasks', 'canary_tasks', 'max_active_canaries', 'catalog_dir', 'delivery_retry_seconds',
                    'spool_success_days', 'spool_failure_days', 'central_diagnostic_days', 'central_failure_days',
                    'workspace_success_days', 'workspace_failure_days', 'max_central_bytes', 'compact_retention'}
    compact_enabled = config_data.get('compact_retention', True)
    if not isinstance(compact_enabled, bool): raise ValueError('compact_retention must be a boolean')
    fleet = FleetConfig.from_dict({k: v for k, v in config_data.items() if k not in service_keys})
    if fleet.central_dir is None:
        raise ValueError('Import requires central_dir')
    source = Path(source).resolve(strict=True)
    result = verify_manifest(source)
    run_id = source.name
    metadata = json.loads((source / 'metadata.json').read_text())
    if not IDENTIFIER.fullmatch(run_id) or result.get('runId') != run_id or metadata.get('runId') != run_id:
        raise ValueError('Source run identity mismatch')
    quality = quality_report(source, fleet.central_dir / '.admission')
    if not quality['eligible']:
        raise ValueError('Source is not independently eligible: ' + ', '.join(quality['reasons']))
    fingerprint = digest(source / 'manifest.json')
    existing_target = fleet.central_dir / run_id
    existing_receipt = fleet.central_dir / '.admission' / (run_id + '.json')
    if existing_target.exists() or existing_receipt.exists():
        if not existing_target.is_dir() or not existing_receipt.is_file():
            raise ValueError('Existing central run lacks a matching admission receipt')
        verify_manifest(existing_target)
        prior = json.loads(existing_receipt.read_text())
        if (prior.get('runId') != run_id or prior.get('manifestSha256') != digest(existing_target / 'manifest.json')
                or not source_admitted(fleet.central_dir / '.admission', run_id, fingerprint)
                or not quality_report(existing_target, fleet.central_dir / '.admission').get('eligible')):
            raise ValueError('Existing central admission is stale, revoked or inconsistent')
    spool = fleet.artifacts_dir.resolve()
    spool.mkdir(parents=True, exist_ok=True)
    dest = spool / run_id
    if dest.exists():
        verify_manifest(dest)
        if digest(dest / 'manifest.json') != fingerprint:
            raise ValueError('Spool run conflicts with source manifest')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.import-', dir=spool))
        try:
            shutil.copytree(source, stage / run_id)
            copied = stage / run_id
            verify_manifest(copied)
            if digest(copied / 'manifest.json') != fingerprint:
                raise ValueError('Copied manifest differs')
            try:
                os.rename(copied, dest)
            except FileExistsError:
                verify_manifest(dest)
                if digest(dest / 'manifest.json') != fingerprint:
                    raise ValueError('Concurrent spool import conflicts')
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    store = ArtifactStore(spool, fleet.central_dir, state_dir=fleet.state_dir / 'delivery',
                          max_buffer_bytes=int(config_data.get('max_buffer_bytes', 64 * 1024**3)),
                          retry_seconds=float(config_data.get('delivery_retry_seconds', 5)),
                          max_central_bytes=int(config_data.get('max_central_bytes', 1024**4)))
    # A central run that already has a receipt must never be redelivered merely
    # to enroll coverage: redelivery could replace an independent revocation.
    target = fleet.central_dir / run_id
    if not target.exists():
        store.enqueue(run_id, compact=compact_enabled)
        store.deliver_pending(limit=1)
    else:
        with store._db() as db:
            row = db.execute('SELECT state,manifest_sha FROM deliveries WHERE run_id=?', (run_id,)).fetchone()
        if row is None or row['state'] != 'delivered' or row['manifest_sha'] != fingerprint:
            raise ValueError('Existing central run is not tracked as delivered in this queue')
    if not target.is_dir():
        raise ValueError('Delivery is pending; retry the import after the delivery queue recovers')
    verify_manifest(target)
    target_fingerprint = digest(target / 'manifest.json')
    if not source_admitted(fleet.central_dir / '.admission', run_id, fingerprint):
        raise ValueError('Central run conflicts with source manifest')
    # Do not promote a previously revoked or stale central receipt implicitly.
    receipt = json.loads((fleet.central_dir / '.admission' / (run_id + '.json')).read_text())
    if (receipt.get('manifestSha256') != target_fingerprint or receipt.get('quality', {}).get('policyVersion') != QUALITY_POLICY_VERSION
            or receipt.get('quality', {}).get('eligible') is not True):
        raise ValueError('Central admission is stale, revoked or inconsistent')
    inventory = CoverageInventory(fleet.state_dir / 'coverage.sqlite3', catalog_path=config_data.get('catalog_dir'),
                                  enabled_tasks=config_data.get('enabled_tasks'), world_bank=fleet.world_bank, seed=fleet.seed,
                                  canary_tasks=config_data.get('canary_tasks'),
                                  max_active_canaries=config_data.get('max_active_canaries', 0), central_dir=fleet.central_dir)
    # Ingest the full source before pruning; its identity is the managed-import proof.
    inventory.ingest(dest)
    if source != dest and receipt.get('compactSchemaVersion') is not None:
        try:
            with store.history_guard():
                candidates = migration_workers(fleet, store, include_imports=True)
                match = [candidate for candidate in candidates if candidate.get('runId') == run_id
                         and candidate.get('provenance') == 'managed_import']
                if match: store.prune_compact(None, match)
        except (OSError, ValueError, sqlite3.Error, BackendError):
            # Missing authoritative runtime/fleet evidence leaves the spool protected.
            pass
    return {'runId': run_id, 'manifestSha256': target_fingerprint, 'quality': receipt['quality'], 'central': str(target), 'coverageIngested': True}


def export_index(central: Path, output: Path, run_ids: list[str] | None = None) -> dict:
    central = Path(central).resolve(strict=True)
    accepted, rejected = [], []
    selected = set(run_ids) if run_ids is not None else None
    if selected is not None and (not selected or any(not IDENTIFIER.fullmatch(run_id) for run_id in selected)):
        raise ValueError('Run selection must contain valid run IDs')
    seen = set()
    for run in sorted(central.iterdir()):
        if run.name.startswith('.') or not run.is_dir() or run.is_symlink() or not IDENTIFIER.fullmatch(run.name):
            continue
        if selected is not None and run.name not in selected:
            continue
        seen.add(run.name)
        try:
            result = verify_manifest(run)
            meta = json.loads((run / 'metadata.json').read_text())
            if result.get('runId') != run.name or meta.get('runId') != run.name:
                raise ValueError('run_identity_mismatch')
            manifest_hash = digest(run / 'manifest.json')
            receipt_path = central / '.admission' / (run.name + '.json')
            receipt = json.loads(receipt_path.read_text())
            quality = receipt.get('quality') or {}
            if receipt.get('runId') != run.name or receipt.get('manifestSha256') != manifest_hash:
                raise ValueError('admission_identity_mismatch')
            if quality.get('policyVersion') != QUALITY_POLICY_VERSION:
                raise ValueError('admission_policy_stale')
            if quality.get('eligible') is not True:
                raise ValueError('admission_excluded:' + ','.join(quality.get('reasons', [])))
            fresh = quality_report(run, central / '.admission')
            if not fresh['eligible']:
                raise ValueError('current_quality_excluded:' + ','.join(fresh['reasons']))
            world = meta.get('world') or {}
            seed = world.get('sourceSeed')
            if seed is None:
                if world.get('id'):
                    raise ValueError('banked_world_source_seed_missing')
                seed = world.get('seed', meta.get('seed'))
            if seed is None:
                raise ValueError('world_group_missing')
            bases = []
            for index in sorted((run / 'captures').glob('*.jsonl')):
                if index.name.endswith('.parsed.jsonl'): continue
                base = index.with_suffix('')
                binary = base.with_suffix('.bin')
                bases.append({'base': str(base), 'name': base.name, 'binarySha256': digest(binary),
                              'indexSha256': digest(index)})
            if not bases: raise ValueError('no_capture_pairs')
            accepted.append({'runId': run.name, 'manifestSha256': manifest_hash,
                             'admissionSha256': digest(receipt_path), 'policyVersion': quality['policyVersion'],
                             'worldGroup': str(seed), 'worldId': world.get('id'),
                             'worldRevision': world.get('revision'), 'assignment': meta.get('assignment'), 'captures': bases})
        except (OSError, ValueError, TypeError, KeyError) as error:
            rejected.append({'runId': run.name, 'reason': str(error)})
    if selected is not None and seen != selected:
        raise ValueError('Selected runs absent from central storage: ' + ', '.join(sorted(selected - seen)))
    # The reader names outputs by basename; reject collisions instead of overwriting rows.
    names = [c['name'] for r in accepted for c in r['captures']]
    if len(names) != len(set(names)):
        raise ValueError('Capture basename collision in eligible runs')
    groups = sorted({r['worldGroup'] for r in accepted}, key=lambda s: hashlib.sha256(s.encode()).hexdigest())
    # Keep all captures from the same world seed together, including resumed checkpoints.
    val_groups = set(groups[:max(1, len(groups) // 5)]) if len(groups) >= 2 else set()
    train = [c for r in accepted if r['worldGroup'] not in val_groups for c in r['captures']]
    val = [c for r in accepted if r['worldGroup'] in val_groups for c in r['captures']]
    manifest = {'schemaVersion': 1, 'qualityPolicyVersion': QUALITY_POLICY_VERSION,
                'selectedRunIds': sorted(selected) if selected is not None else None,
                'split': {'method': 'deterministic_world_seed', 'validationWorldGroups': sorted(val_groups),
                          'ready': bool(train and val)},
                'accepted': accepted, 'rejected': rejected,
                'trainBases': [c['base'] for c in train], 'validationBases': [c['base'] for c in val],
                'validationSessionNames': [c['name'] for c in val]}
    atomic_json(Path(output), manifest)
    return manifest
