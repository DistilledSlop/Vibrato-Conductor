"""Verified filesystem delivery, restartable retries, and conservative dataset admission."""
from __future__ import annotations
import json
from contextlib import contextmanager
import fcntl
import math
import os
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
    if result.get('capture', {}).get('complete') is not True or not capture_integrity(root): reasons.append('capture_incomplete')
    health = result.get('serverHealth', {})
    mean = health.get('meanTps')
    if (health.get('valid') is not True or health.get('samples', 0) < 10
            or not isinstance(mean, (int, float)) or not math.isfinite(mean) or mean < 18):
        reasons.append('server_capacity_unvalidated')
    if result.get('qualityInvalid'): reasons.extend(str(r) for r in result['qualityInvalid'])
    if result.get('watchdog', {}).get('classification'):
        reasons.append('watchdog_classification')
    versions = metadata.get('versions', {})
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
                 max_buffer_bytes: int = 64 * 1024**3, retry_seconds: float = 5):
        self.local_root = Path(local_root).resolve()
        self.central_root = Path(central_root).resolve()
        self.state_dir = Path(state_dir or self.local_root.parent / 'delivery-state').resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.central_root.mkdir(parents=True, exist_ok=True)
        (self.central_root / '.incoming').mkdir(exist_ok=True)
        self.max_buffer_bytes = int(max_buffer_bytes)
        self.retry_seconds = float(retry_seconds)
        if self.max_buffer_bytes < 1 or not math.isfinite(self.retry_seconds) or self.retry_seconds < 0:
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

    def enqueue(self, run_id: str) -> dict:
        if not IDENTIFIER.fullmatch(run_id): raise ValueError('Invalid run id')
        source = self.local_root / run_id
        verify_manifest(source)
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
                    self._copy(source, stage)
                    verify_manifest(stage)
                    if digest(stage / 'manifest.json') != job['manifest_sha']:
                        raise ValueError('Copied manifest differs')
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
        return outcomes

    def snapshot(self) -> dict:
        with self.lock, self._db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM deliveries ORDER BY queued_at DESC')]
        for row in rows: row['quality'] = json.loads(row['quality'])
        pending_bytes = sum(r['bytes'] for r in rows if r['state'] != 'delivered')
        return {'backend': 'filesystem', 'centralRoot': str(self.central_root), 'runs': rows,
                'pendingBytes': pending_bytes, 'maxBufferBytes': self.max_buffer_bytes,
                'backpressure': pending_bytes >= self.max_buffer_bytes,
                'delivered': sum(r['state'] == 'delivered' for r in rows),
                'eligible': sum(r['state'] == 'delivered' and r['quality']['eligible'] for r in rows)}

    def prune(self, active_run_ids=(), successful_days: float = 7, failure_days: float = 30,
              now: float | None = None, dry_run: bool = True) -> list[str]:
        """Prune only delivered local spools; central evidence is retained independently."""
        if self.local_root == self.central_root: return []
        if successful_days < 0 or failure_days < successful_days:
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
            if not dry_run: shutil.rmtree(source)
        return candidates
