"""Verified filesystem delivery, restartable retries, and conservative dataset admission."""
from __future__ import annotations
import json
from contextlib import contextmanager
import fcntl
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .storage import IDENTIFIER, atomic_json, digest, verify_manifest


def capture_integrity(root: Path) -> bool:
    """Check all raw/index pairs and flush markers independently of worker summary labels."""
    captures = root / 'captures'
    indexes = [p for p in captures.glob('*.jsonl') if not p.name.endswith('.parsed.jsonl')]
    if not indexes or {p.stem for p in captures.glob('*.bin')} != {p.stem for p in indexes}: return False
    directions = set()
    for index in indexes:
        binary = index.with_suffix('.bin')
        marker = index.with_suffix('.status.json')
        if not marker.is_file(): return False
        status = json.loads(marker.read_text())
        if status.get('complete') is not True: return False
        end = 0
        with index.open() as lines:
            for line in lines:
                packet = json.loads(line)
                if 'off' not in packet: continue
                length = packet.get('len')
                if not isinstance(length, int) or length < 1 or packet['off'] != end: return False
                end += length
                if packet.get('state') == 'play' and packet.get('dir') in ('C2S', 'S2C'):
                    directions.add(packet['dir'])
        if end != binary.stat().st_size or status.get('bytes') != end: return False
    return directions == {'C2S', 'S2C'}


def quality_report(root: Path) -> dict:
    result = verify_manifest(root)
    metadata = json.loads((root / 'metadata.json').read_text())
    reasons = []
    if metadata.get('mode') != 'collection': reasons.append('diagnostic_mode')
    if result.get('diagnosticPassed') is not True: reasons.append('diagnostic_failed')
    if result.get('taskSuccess') is not True: reasons.append('task_success_unconfirmed')
    if result.get('datasetEligible') is not True: reasons.append('worker_excluded')
    try: capture_valid = capture_integrity(root)
    except (OSError, ValueError, TypeError, KeyError): capture_valid = False
    if result.get('capture', {}).get('complete') is not True or not capture_valid: reasons.append('capture_incomplete')
    def healthy_ticks(health):
        if not isinstance(health, dict): return False
        samples, mean = health.get('samples'), health.get('meanTps')
        return (health.get('valid') is True and isinstance(samples, int) and samples >= 10
                and isinstance(mean, (float, int)) and math.isfinite(mean) and mean >= 18
                and health.get('degradedWindows', 0) == 0)
    if not healthy_ticks(result.get('serverHealth')): reasons.append('server_capacity_unvalidated')
    if not healthy_ticks(result.get('clientHealth')): reasons.append('client_capacity_unvalidated')
    if result.get('qualityInvalid'): reasons.extend(str(r) for r in result['qualityInvalid'])
    watchdog = result.get('watchdog')
    if watchdog:
        # Worker assessments use direct reason/kind/confidence/evidence; healthy workers explicitly emit null.
        reasons.append('watchdog_classification')
    if 'assignedGamemode' in metadata:
        expected_mode = metadata['assignedGamemode']
        operator = metadata.get('operator')
        declared_level = metadata.get('operatorLevel', metadata.get('operatorPermissionLevel', 2))
        valid_definition = (expected_mode in ('survival', 'creative', 'adventure', 'spectator')
            and isinstance(operator, bool) and isinstance(declared_level, int)
            and not isinstance(declared_level, bool) and 1 <= declared_level <= 4)
        expected_level = declared_level if operator is True else 0
        context = result.get('assignmentContext') or {}
        observed_context = False
        mismatched_context = False
        heartbeats = root / 'heartbeats.jsonl'
        if heartbeats.exists():
            with heartbeats.open() as lines:
                for line in lines:
                    try: heartbeat = json.loads(line)
                    except ValueError: continue
                    if not isinstance(heartbeat, dict) or heartbeat.get('permissionSource') != 'server_observer': continue
                    actual_mode, actual_level = heartbeat.get('actualMode'), heartbeat.get('permissionLevel')
                    if actual_mode not in ('survival', 'creative', 'adventure', 'spectator') or not isinstance(actual_level, int) or isinstance(actual_level, bool): continue
                    if actual_mode == expected_mode and actual_level == expected_level: observed_context = True
                    else: mismatched_context = True
        if (not valid_definition or not isinstance(context, dict) or context.get('valid') is not True
                or not observed_context or mismatched_context):
            reasons.append('assignment_context_unconfirmed_or_mismatched')
        scenario_id = metadata.get('scenario')
        if scenario_id:
            try:
                scenario = json.loads((root / 'scenario-result.json').read_text())
                scenario_evidence = scenario.get('evidence') or {}
                confirmed = (scenario.get('scenario') == scenario_id and scenario.get('status') == 'completed'
                    and scenario.get('source') == 'client_actions_plus_server_observer'
                    and scenario_evidence.get('serverConfirmed') is True)
            except (OSError, ValueError, AttributeError, TypeError): confirmed = False
            if not confirmed: reasons.append('scenario_server_outcome_unconfirmed')
    versions = metadata.get('versions', {})
    if versions.get('optimizationMods'):
        profile = versions.get('optimizationProfile') or {}
        # The optional evaluation builder only emits unvalidated profiles. A future reviewed profile must
        # bind every required behavior scenario and the exact added mod hashes, not just claim faster ticks.
        required = {'survival_resource', 'creative_inventory', 'creative_build', 'adventure_inspect',
                    'spectator_explore', 'operator_commands', 'mob_ai', 'block_physics', 'redstone',
                    'containers', 'nether', 'gamer'}
        review = profile.get('behaviorEvidence') or {} if isinstance(profile, dict) else {}
        scenarios = review.get('scenarios') or {} if isinstance(review, dict) else {}
        mod_hashes = review.get('modSha256') or {} if isinstance(review, dict) else {}
        declared_mods = versions['optimizationMods']
        reviewed = (isinstance(profile, dict) and profile.get('behaviorValidated') is True
            and profile.get('behaviorValidation') == 'reviewed_equivalent' and isinstance(review, dict)
            and bool(review.get('reviewedBy')) and bool(review.get('reviewedAt'))
            and isinstance(scenarios, dict) and required.issubset(scenarios)
            and all(isinstance(scenarios[name], dict) and scenarios[name].get('verdict') == 'equivalent'
                    and re.fullmatch('[0-9a-f]{64}', str(scenarios[name].get('baselineManifestSha256', '')))
                    and re.fullmatch('[0-9a-f]{64}', str(scenarios[name].get('candidateManifestSha256', '')))
                    for name in required)
            and isinstance(declared_mods, list) and isinstance(mod_hashes, dict)
            and all(isinstance(mod, dict) and mod_hashes.get(mod.get('modId')) == mod.get('sha256')
                    and re.fullmatch('[0-9a-f]{64}', str(mod.get('sha256', ''))) for mod in declared_mods))
        if not reviewed: reasons.append('experimental_optimization_profile_unvalidated')
    if versions.get('minecraft') != '1.21.1' or not metadata.get('build'):
        reasons.append('software_identity_missing')
    world = metadata.get('world', {})
    if world.get('id') and not world.get('revision'): reasons.append('world_revision_missing')
    if not world.get('id') and 'seed' not in world: reasons.append('world_seed_missing')
    return {'eligible': not reasons, 'reasons': sorted(set(reasons)), 'taskSuccess': result.get('taskSuccess'),
            'diagnosticPassed': result.get('diagnosticPassed'), 'mode': metadata.get('mode')}


class ArtifactStore:
    """A central filesystem is the selected backend; .incoming is never a completed dataset entry.

    Capture writers finalize immutable spool directories. Delivery publishes only a verified complete directory,
    then records a receipt in SQLite. Retries survive restarts; failed partial copies never become visible runs.
    """
    def __init__(self, local_root: Path, central_root: Path, state_dir: Path | None = None,
                 max_buffer_bytes: int = 64 * 1024**3, retry_seconds: float = 5,
                 max_central_bytes: int = 1024**4, usage_cache_seconds: float = 2):
        self.local_root = Path(local_root).resolve()
        self.central_root = Path(central_root).resolve()
        self.state_dir = Path(state_dir or self.local_root.parent / 'delivery-state').resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.central_root.mkdir(parents=True, exist_ok=True)
        (self.central_root / '.incoming').mkdir(exist_ok=True)
        self.max_buffer_bytes = int(max_buffer_bytes)
        self.retry_seconds = float(retry_seconds)
        self.max_central_bytes = int(max_central_bytes)
        self.usage_cache_seconds = float(usage_cache_seconds)
        self._usage_at, self._usage_value = 0, None
        if (self.max_buffer_bytes < 1 or self.max_central_bytes < 1
                or not math.isfinite(self.usage_cache_seconds) or self.usage_cache_seconds < 0
                or not math.isfinite(self.retry_seconds) or self.retry_seconds < 0):
            raise ValueError('Invalid artifact buffering/retry settings')
        self.lock = threading.RLock()
        self.db_path = self.state_dir / 'delivery.sqlite'
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS deliveries (
                run_id TEXT PRIMARY KEY, manifest_sha TEXT NOT NULL, bytes INTEGER NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT, queued_at REAL NOT NULL, delivered_at REAL, quality TEXT NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db: yield db
        finally: db.close()

    def quarantine_orphan(self, run_id: str, reason: str, *, terminal: bool = False, metadata: dict | None = None,
                          confidence: str = 'confirmed', classification: str | None = None) -> dict:
        """Finalize evidence after a confirmed terminal runtime death, never while a worker may still write.

        The caller must establish backend terminal/absent state. This records infrastructure loss, not inferred bot
        failure. Original malformed/incomplete files remain available as forensic artifacts.
        """
        if not terminal: raise ValueError('Orphan quarantine requires confirmed terminal runtime state')
        if confidence not in ('confirmed', 'inferred'): raise ValueError('Invalid quarantine confidence')
        classification = classification or ('confirmed_runtime_termination' if confidence == 'confirmed' else 'runtime_missing_after_quiescence')
        if not IDENTIFIER.fullmatch(run_id): raise ValueError('Invalid run id')
        root = self.local_root / run_id
        if not root.is_dir() or root.is_symlink(): raise ValueError('No safe orphan artifact directory')
        with self.lock:
            if (root / 'manifest.json').exists():
                return verify_manifest(root)
            metadata_path = root / 'metadata.json'
            recovered = dict(metadata or {})
            if metadata_path.exists():
                try:
                    original = json.loads(metadata_path.read_text())
                    if isinstance(original, dict): recovered = {**recovered, **original}
                    else: raise ValueError('Metadata was not an object')
                except (OSError, ValueError):
                    backup = root / 'metadata.interrupted.json'
                    if backup.exists(): raise ValueError('Interrupted metadata backup already exists')
                    metadata_path.replace(backup)
                    recovered['metadataRecovery'] = 'Original bytes retained in metadata.interrupted.json'
            recovered.update({'runId': run_id, 'mode': 'diagnostic', 'quarantined': True})
            atomic_json(metadata_path, recovered)
            result_path = root / 'result.json'
            if result_path.exists():
                backup = root / 'result.interrupted.json'
                if backup.exists(): raise ValueError('Interrupted result backup already exists')
                result_path.replace(backup)
            result = {'schemaVersion': 1, 'runId': run_id, 'reason': str(reason), 'diagnosticPassed': False,
                      'taskSuccess': None, 'datasetEligible': False, 'quarantined': True,
                      'classification': classification, 'confidence': confidence,
                      'capture': {'complete': False, 'bytes': sum(p.stat().st_size for p in (root / 'captures').glob('*.bin') if not p.is_symlink())},
                      'serverHealth': {'valid': False, 'samples': 0, 'meanTps': None},
                      'qualityInvalid': ['runtime_terminated_before_finalization'], 'finalizedWall': time.time()}
            atomic_json(result_path, result)
            from .storage import artifact_manifest
            atomic_json(root / 'manifest.json', artifact_manifest(root, result))
            self._usage_at = 0
            return result

    def enqueue(self, run_id: str) -> dict:
        if not IDENTIFIER.fullmatch(run_id): raise ValueError('Invalid run id')
        source = self.local_root / run_id
        if source.is_symlink() or (source / 'manifest.json').is_symlink():
            raise ValueError('Symlinked artifact source')
        fingerprint = digest(source / 'manifest.json')
        with self.lock, self._db() as db:
            existing = db.execute('SELECT * FROM deliveries WHERE run_id=?', (run_id,)).fetchone()
            if existing:
                if existing['manifest_sha'] != fingerprint:
                    raise ValueError('Run id already queued with a different manifest')
                # Immutable manifest identity is cheap to check every controller tick. Payload hashes are checked
                # on first queueing, actual delivery, retention, and explicit verification.
                return dict(existing)
        result = verify_manifest(source)
        if result.get('runId') != run_id: raise ValueError('Artifact result does not match run directory identity')
        quality = quality_report(source)
        manifest = json.loads((source / 'manifest.json').read_text())
        size = sum(row['bytes'] for row in manifest['files']) + (source / 'manifest.json').stat().st_size
        fingerprint = digest(source / 'manifest.json')
        with self.lock, self._db() as db:
            existing = db.execute('SELECT * FROM deliveries WHERE run_id=?', (run_id,)).fetchone()
            if existing and existing['manifest_sha'] != fingerprint:
                raise ValueError('Run id already queued with a different manifest')
            if not existing:
                db.execute('INSERT INTO deliveries(run_id,manifest_sha,bytes,state,queued_at,quality) VALUES(?,?,?,?,?,?)',
                           (run_id, fingerprint, size, 'queued', time.time(), json.dumps(quality)))
            return dict(db.execute('SELECT * FROM deliveries WHERE run_id=?', (run_id,)).fetchone())

    def _copy(self, source: Path, stage: Path):
        shutil.copytree(source, stage, copy_function=shutil.copyfile)
        # Flush copied files before publishing the directory and its receipt.
        for path in stage.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream: os.fsync(stream.fileno())

    def deliver_pending(self, limit: int = 4) -> list[dict]:
        # A process lock makes recovery immediate after a crash and prevents two pumps copying the same queue.
        with (self.state_dir / 'delivery.lock').open('a') as lease:
            try: fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return []
            try:
                with self._db() as db:
                    known = {r[0] for r in db.execute('SELECT run_id FROM deliveries')}
                    db.execute("UPDATE deliveries SET state='retry',next_attempt=0,lease_until=0 WHERE state='delivering'")
                for partial in (self.central_root / '.incoming').iterdir():
                    identity, separator, suffix = partial.name.rpartition('-')
                    if identity in known and separator and len(suffix) == 32 and all(c in '0123456789abcdef' for c in suffix):
                        if partial.is_symlink(): partial.unlink()
                        elif partial.is_dir(): shutil.rmtree(partial)
                return self._deliver_pending(limit)
            finally: fcntl.flock(lease, fcntl.LOCK_UN)

    def _deliver_pending(self, limit: int = 4) -> list[dict]:
        if not 1 <= limit <= 1000: raise ValueError('Delivery limit out of range')
        outcomes = []
        for _ in range(limit):
            now = time.time()
            with self.lock, self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT * FROM deliveries WHERE (state IN ('queued','retry') AND next_attempt<=?) OR (state='delivering' AND lease_until<?) ORDER BY queued_at LIMIT 1", (now, now)).fetchone()
                if row is None: break
                job = dict(row)
                db.execute("UPDATE deliveries SET state='delivering',attempts=attempts+1,lease_until=? WHERE run_id=?", (now + 300, job['run_id']))
            run_id = job['run_id']
            source, target = self.local_root / run_id, self.central_root / run_id
            stage = self.central_root / '.incoming' / (run_id + '-' + uuid.uuid4().hex)
            try:
                verify_manifest(source)
                if digest(source / 'manifest.json') != job['manifest_sha']:
                    raise ValueError('Source manifest changed after queueing')
                if target.exists():
                    verify_manifest(target)
                    if digest(target / 'manifest.json') != job['manifest_sha']:
                        raise ValueError('Central run conflicts with queued identity')
                else:
                    usage = self._usage(force=True)
                    if usage['centralBytes'] + job['bytes'] > self.max_central_bytes:
                        raise OSError('Central artifact byte limit reached')
                    if shutil.disk_usage(self.central_root).free < job['bytes']:
                        raise OSError('Central artifact filesystem has insufficient free space')
                    self._copy(source, stage)
                    try:
                        verify_manifest(stage)
                        if digest(stage / 'manifest.json') != job['manifest_sha']:
                            raise ValueError('Copied manifest differs')
                    except ValueError as error:
                        raise OSError('Copied artifacts failed integrity validation: ' + str(error)) from error
                    stage.replace(target)
                    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(descriptor)
                    finally: os.close(descriptor)
                quality = quality_report(target)
                with self.lock, self._db() as db:
                    db.execute("UPDATE deliveries SET state='delivered',delivered_at=?,error=NULL,lease_until=0,quality=? WHERE run_id=?",
                               (time.time(), json.dumps(quality), run_id))
                outcomes.append({'runId': run_id, 'state': 'delivered', 'quality': quality})
            except Exception as error:
                # A missing source or transient filesystem outage remains a retry, never an apparent success.
                with self.lock, self._db() as db:
                    attempt = db.execute('SELECT attempts FROM deliveries WHERE run_id=?', (run_id,)).fetchone()[0]
                    delay = min(3600, self.retry_seconds * 2**min(attempt - 1, 10))
                    failure_state = 'invalid' if isinstance(error, ValueError) else 'retry'
                    db.execute("UPDATE deliveries SET state=?,error=?,next_attempt=?,lease_until=0 WHERE run_id=?",
                               (failure_state, str(error), time.time() + delay, run_id))
                outcomes.append({'runId': run_id, 'state': failure_state, 'error': str(error)})
            finally:
                if stage.exists(): shutil.rmtree(stage)
                self._usage_at = 0
        return outcomes

    def _usage(self, force=False) -> dict:
        with self.lock:
            now = time.monotonic()
            if not force and self._usage_value is not None and now - self._usage_at < self.usage_cache_seconds:
                return self._usage_value
            def size(root):
                total = 0
                for path in root.rglob('*'):
                    try:
                        if path.is_file() and not path.is_symlink(): total += path.stat().st_size
                    except FileNotFoundError: pass  # atomic replacements and live files can disappear during sampling
                return total
            spool = size(self.local_root)
            central = spool if self.local_root == self.central_root else size(self.central_root)
            self._usage_value = {'liveSpoolBytes': spool, 'centralBytes': central}
            self._usage_at = now
            return self._usage_value

    def snapshot(self) -> dict:
        with self.lock, self._db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM deliveries ORDER BY queued_at DESC')]
        for row in rows: row['quality'] = json.loads(row['quality'])
        pending_bytes = sum(r['bytes'] for r in rows if r['state'] not in ('delivered', 'expired'))
        usage = self._usage()
        return {'backend': 'filesystem', 'centralRoot': str(self.central_root), 'runs': rows, **usage,
                'pendingBytes': pending_bytes, 'maxBufferBytes': self.max_buffer_bytes,
                'maxCentralBytes': self.max_central_bytes,
                'backpressure': max(pending_bytes, usage['liveSpoolBytes']) >= self.max_buffer_bytes
                    or usage['centralBytes'] + (pending_bytes if self.local_root != self.central_root else 0) >= self.max_central_bytes,
                'delivered': sum(r['state'] == 'delivered' for r in rows),
                'eligible': sum(r['state'] == 'delivered' and r['quality']['eligible'] for r in rows)}

    def prune(self, active_run_ids=(), successful_days: float = 7, failure_days: float = 30,
              now: float | None = None, dry_run: bool = True) -> list[str]:
        """Prune only delivered local spools; central evidence is retained independently."""
        if self.local_root == self.central_root: return []
        if (not math.isfinite(successful_days) or not math.isfinite(failure_days)
                or successful_days < 0 or failure_days < successful_days):
            raise ValueError('Failures must be retained at least as long as successful diagnostics')
        active = set(active_run_ids)
        now = time.time() if now is None else now
        candidates = []
        for row in self.snapshot()['runs']:
            run_id = row['run_id']
            if run_id in active or row['state'] != 'delivered': continue
            age = now - row['delivered_at']
            days = successful_days if row['quality']['diagnosticPassed'] and row['quality']['taskSuccess'] is not False else failure_days
            source, target = self.local_root / run_id, self.central_root / run_id
            if age < days * 86400 or not source.exists(): continue
            verify_manifest(target)
            if digest(target / 'manifest.json') != row['manifest_sha']:
                raise ValueError('Refusing retention cleanup after central artifact corruption')
            if source.is_symlink() or source.parent != self.local_root:
                raise ValueError('Unsafe retention path')
            candidates.append(run_id)
            if not dry_run:
                shutil.rmtree(source)
                self._usage_at = 0
        return candidates


    def prune_central(self, active_run_ids=(), diagnostic_days: float = 30, failure_days: float = 90,
                      now: float | None = None, dry_run: bool = True) -> list[str]:
        """Expire delivered diagnostic evidence after explicit retention periods; training data is protected.

        This is a permanent diagnostic retention policy, distinct from local spool cleanup. Unknown, active,
        undelivered, or corrupted runs are never deleted. Eligible training entries are never removed here.
        """
        if (not math.isfinite(diagnostic_days) or not math.isfinite(failure_days)
                or diagnostic_days < 0 or failure_days < diagnostic_days):
            raise ValueError('Failures must be retained at least as long as ordinary diagnostics')
        active = set(active_run_ids)
        now = time.time() if now is None else now
        removed = []
        for row in self.snapshot()['runs']:
            run_id = row['run_id']
            if run_id in active or row['state'] != 'delivered' or row['quality']['eligible']: continue
            passed = row['quality']['diagnosticPassed'] and row['quality']['taskSuccess'] is not False
            days = diagnostic_days if passed else failure_days
            if now - row['delivered_at'] < days * 86400: continue
            target, source = self.central_root / run_id, self.local_root / run_id
            if not target.exists(): continue
            verify_manifest(target)
            if digest(target / 'manifest.json') != row['manifest_sha']:
                raise ValueError('Refusing retention cleanup of corrupted central evidence')
            # If another still-pending copy exists it must be byte-identical, not an in-progress replacement.
            if source != target and source.exists():
                verify_manifest(source)
                if digest(source / 'manifest.json') != row['manifest_sha']:
                    raise ValueError('Refusing retention cleanup with changed local evidence')
            removed.append(run_id)
            if not dry_run:
                shutil.rmtree(target)
                if source != target and source.exists(): shutil.rmtree(source)
                with self.lock, self._db() as db:
                    db.execute("UPDATE deliveries SET state='expired',error=NULL WHERE run_id=?", (run_id,))
                self._usage_at = 0
        return removed

    def prune_workspaces(self, workspace_root: Path, workers, successful_days: float = 1,
                         failure_days: float = 7, dry_run: bool = True, now: float | None = None) -> list[str]:
        """Remove only retained, definitively terminal, centrally verified worker worlds.

        Workspace paths are derived solely from validated run ids below the supplied root. No caller-provided
        artifact/workspace path from a worker record is trusted. Unknown or active directories stay untouched.
        """
        if (not math.isfinite(successful_days) or not math.isfinite(failure_days)
                or successful_days < 0 or failure_days < successful_days):
            raise ValueError('Failed worlds must be retained at least as long as successful worlds')
        workspace_root = Path(workspace_root)
        if workspace_root.is_symlink(): raise ValueError('Workspace root must not be a symlink')
        if not workspace_root.is_dir(): return []
        workspace_root = workspace_root.resolve()
        now = time.time() if now is None else now
        receipts = {row['run_id']: row for row in self.snapshot()['runs']}
        removed = []
        for worker in workers:
            run_id = worker.get('runId', worker.get('run_id', ''))
            if not isinstance(run_id, str) or not IDENTIFIER.fullmatch(run_id): continue
            if worker.get('state') not in {'completed', 'failed', 'lost', 'cancelled'}: continue
            receipt = receipts.get(run_id)
            if receipt is None or receipt['state'] != 'delivered': continue
            passed = receipt['quality']['diagnosticPassed'] and receipt['quality']['taskSuccess'] is not False
            days = successful_days if passed else failure_days
            terminal_at = worker.get('updatedWall', worker.get('updated', receipt['delivered_at']))
            if not isinstance(terminal_at, (float, int)) or not math.isfinite(terminal_at): continue
            if now - max(terminal_at, receipt['delivered_at']) < days * 86400: continue
            workspace = workspace_root / run_id
            if workspace.is_symlink() or not workspace.is_dir() or workspace.resolve().parent != workspace_root: continue
            target = self.central_root / run_id
            verify_manifest(target)
            if digest(target / 'manifest.json') != receipt['manifest_sha']:
                raise ValueError('Refusing world cleanup after central artifact corruption')
            removed.append(run_id)
            if not dry_run: shutil.rmtree(workspace)
        return removed
