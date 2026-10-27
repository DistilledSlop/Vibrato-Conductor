"""Evidence-based coverage inventory and reproducible deficit scheduling."""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from .storage import digest, inspect_world_bank, verify_manifest

CATALOGS = Path(__file__).resolve().parents[1] / 'catalogs'
DIRECTIONS = {'C2S': 'serverbound', 'S2C': 'clientbound'}


def packet_id(stream, offset: int, length: int) -> int:
    stream.seek(offset)
    value = 0
    for index in range(min(5, length)):
        data = stream.read(1)
        if not data: raise ValueError('Truncated packet id')
        value |= (data[0] & 0x7f) << (7 * index)
        if not data[0] & 0x80: return value
    raise ValueError('Invalid packet id VarInt')


class CoverageInventory:
    def __init__(self, db_path: Path, catalog_path: Path | None = None,
                 enabled_tasks: list[str] | None = None, world_bank: Path | None = None, seed: int = 424242):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path = Path(catalog_path or CATALOGS)
        self.world_bank = Path(world_bank) if world_bank is not None else None
        self.seed = int(seed)
        self.lock = threading.RLock()
        self.tasks = json.loads((self.catalog_path / 'tasks.json').read_text())['tasks']
        self.enabled_tasks = set(enabled_tasks) if enabled_tasks is not None else None
        if self.enabled_tasks is not None:
            unknown = self.enabled_tasks - {t['id'] for t in self.tasks}
            if unknown: raise ValueError('Unknown scheduling task ids: ' + ', '.join(sorted(unknown)))
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS targets(id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
                    supported INTEGER NOT NULL, evidence_rule TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(run_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    count INTEGER NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(run_id,target_id));
                CREATE TABLE IF NOT EXISTS successes(run_id TEXT NOT NULL,target_id TEXT NOT NULL,
                    PRIMARY KEY(run_id,target_id));
                CREATE TABLE IF NOT EXISTS assignments(run_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                    assignment TEXT NOT NULL, assigned_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY,manifest_sha TEXT NOT NULL,
                    ingested_at REAL NOT NULL,result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS unknown_evidence(run_id TEXT NOT NULL,kind TEXT NOT NULL,
                    identity TEXT NOT NULL,PRIMARY KEY(run_id,kind,identity));
            ''')
            self._populate(db)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db: yield db
        finally: db.close()

    def _populate(self, db):
        def add(identifier, kind, label, supported, rule, details=None):
            db.execute('INSERT OR REPLACE INTO targets VALUES(?,?,?,?,?,?)',
                       (identifier, kind, label, int(supported), rule, json.dumps(details or {})))
        manifest = json.loads((self.catalog_path / 'protocol-767.json').read_text())
        for packet in manifest['packets']:
            state = packet['state'].lower()
            # The raw proxy terminates login and never captures handshake/status; those remain requested gaps.
            supported = state in ('configuration', 'play')
            add(f'packet:{state}:{packet["direction"]}:{packet["id"]}', 'packet', packet['name'], supported,
                'raw_packet_bytes_with_state_direction_and_wire_id',
                {'state': state, 'direction': packet['direction'], 'wireId': packet['id'],
                 'unsupportedReason': None if supported else 'Recorder does not preserve this protocol phase completely'})
        registries = json.loads((self.catalog_path / 'registries-1.21.1.json').read_text())
        obtainable = set((self.catalog_path / 'obtainable-items.txt').read_text().splitlines())
        for kind, key, rule in [('item', 'items', 'inventory_present'), ('block', 'blocks', 'crosshair_target'), ('mob', 'mobs', 'loaded_nearby')]:
            for name in registries[key]:
                supported = kind != 'item' or name in obtainable
                add(kind + ':minecraft:' + name, kind, name, supported, rule,
                    {'unsupportedReason': None if supported else 'No exact matching resource name in the existing obtainable-item catalogue'})
        for task in self.tasks:
            add('task:' + task['id'], 'task', task.get('description', task['id']), task['supported'],
                'authoritative_root_task_success_not_dispatch_or_elapsed_budget',
                {'command': task['command'], 'seconds': task['seconds'],
                 'requiresTags': task.get('requiresTags', []), 'unsupportedReason': task.get('unsupportedReason')})

    def choose(self, run_id: str, worlds=None) -> dict:
        """Persist allocation before backend create; retries of a run id receive the same assignment."""
        with self.lock, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT assignment FROM assignments WHERE run_id=?', (run_id,)).fetchone()
            if previous: return json.loads(previous[0])
            bank = {w['id']: w for w in inspect_world_bank(self.world_bank)} if self.world_bank else {}
            if worlds:
                available = [bank.get(w, {'id': w, 'tags': [], 'supported': not bool(self.world_bank)}) if isinstance(w, str) else w for w in worlds]
            elif bank:
                available = list(bank.values())
            elif self.world_bank:
                raise ValueError('No valid world-bank entries are available')
            else:
                available = [{'id': None, 'tags': [], 'supported': True}]
            eligible = []
            assigned = dict(db.execute('SELECT task_id,COUNT(*) FROM assignments GROUP BY task_id'))
            counts = dict(db.execute('SELECT target_id,COUNT(*) FROM observations GROUP BY target_id'))
            successes = dict(db.execute('SELECT target_id,COUNT(*) FROM successes GROUP BY target_id'))
            for task in self.tasks:
                if not task['supported'] or self.enabled_tasks is not None and task['id'] not in self.enabled_tasks: continue
                for world in available:
                    if not world.get('supported', True) or world.get('minecraftVersion', '1.21.1') != '1.21.1': continue
                    if not set(task.get('requiresTags', [])).issubset(world.get('tags', [])): continue
                    targets = ['task:' + task['id'], *task.get('targets', [])]
                    deficit = sum(1 / (1 + counts.get(t, 0) + 2 * successes.get(t, 0)) for t in targets) / len(targets)
                    score = deficit / (1 + assigned.get(task['id'], 0))
                    tie = hashlib.sha256(f'{self.seed}:{run_id}:{task["id"]}:{world.get("id")}'.encode()).hexdigest()
                    eligible.append((score, tie, task, world, targets))
            if not eligible: raise ValueError('No supported task matches the available worlds and scheduling filters')
            _, _, task, world, targets = max(eligible, key=lambda row: (row[0], row[1]))
            value = int.from_bytes(hashlib.sha256(f'{self.seed}:{run_id}'.encode()).digest()[:8], 'big') >> 1
            assignment = {'task': task['command'], 'task_id': task['id'], 'seconds': task['seconds'],
                          'seed': value, 'world_id': world.get('id'), 'target_ids': targets,
                          'world_revision': world.get('revision'), 'requires_tags': task.get('requiresTags', [])}
            db.execute('INSERT INTO assignments VALUES(?,?,?,?)', (run_id, task['id'], json.dumps(assignment), time.time()))
            return assignment

    def ingest(self, run_dir: Path) -> dict:
        run_dir = Path(run_dir)
        result = verify_manifest(run_dir)
        metadata = json.loads((run_dir / 'metadata.json').read_text())
        run_id = result.get('runId') or metadata.get('runId') or run_dir.name
        fingerprint = digest(run_dir / 'manifest.json')
        with self.lock, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT manifest_sha,result FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint: raise ValueError('Ingested run identity changed')
                return json.loads(prior[1])
            known = {r[0] for r in db.execute('SELECT id FROM targets')}
            observed = {}
            unknown = set()
            def record(identifier, evidence, count=1):
                if identifier not in known:
                    unknown.add((identifier.split(':', 1)[0], identifier)); return
                if identifier not in observed: observed[identifier] = [0, set()]
                observed[identifier][0] += count
                observed[identifier][1].add(evidence)
            for index in sorted((run_dir / 'captures').glob('*.jsonl')):
                if index.name.endswith('.parsed.jsonl'): continue
                raw_path = index.with_suffix('.bin')
                status_path = index.with_suffix('.status.json')
                if not raw_path.exists() or not status_path.exists(): continue
                status = json.loads(status_path.read_text())
                if not status.get('complete'): continue
                end = 0
                with index.open() as lines, raw_path.open('rb') as raw:
                    for line in lines:
                        packet = json.loads(line)
                        if 'off' not in packet: continue
                        if packet['off'] != end or packet['len'] < 1: raise ValueError('Invalid capture offsets')
                        end += packet['len']
                        state, direction = packet.get('state'), DIRECTIONS.get(packet.get('dir'))
                        if not state or not direction: raise ValueError('Unscoped packet evidence')
                        wire_id = packet_id(raw, packet['off'], packet['len'])
                        record(f'packet:{state.lower()}:{direction}:{wire_id}', 'raw_capture:' + index.name)
                if end != raw_path.stat().st_size or status.get('bytes') != end:
                    raise ValueError('Capture completeness marker disagrees with indexed bytes')
            observed_director = False
            heartbeats = run_dir / 'heartbeats.jsonl'
            if heartbeats.exists():
                with heartbeats.open() as lines:
                    for line in lines:
                        heartbeat = json.loads(line)
                        observed_director = observed_director or heartbeat.get('rootClass') == 'DirectorTask'
                        semantics = heartbeat.get('observationSemantics', {})
                        for plural, kind, rule in [('items','item','inventory_present'),('blocks','block','crosshair_target'),('mobs','mob','loaded_nearby')]:
                            # Require the instrumentation's declared actual observation semantics.
                            if semantics.get(plural) != rule: continue
                            for identifier in set(heartbeat.get('observations', {}).get(plural, [])):
                                if isinstance(identifier, str) and re.fullmatch(r'[a-z0-9_.-]+:[a-z0-9_./-]+', identifier):
                                    record(kind + ':' + identifier, 'heartbeat:' + rule)
            assignment = db.execute('SELECT task_id FROM assignments WHERE run_id=?', (run_id,)).fetchone()
            task_id = assignment[0] if assignment else next((t['id'] for t in self.tasks if t['command'] == metadata.get('assignment')), None)
            script_path = run_dir / 'script-result.json'
            script = json.loads(script_path.read_text()) if script_path.exists() else {}
            command = next((t['command'] for t in self.tasks if t['id'] == task_id), None)
            task_seen = (command == 'director' and observed_director) or any(
                c.get('action') == command and c.get('status') in ('dispatched', 'completed')
                for c in script.get('commands', []))
            if task_id and task_seen:
                record('task:' + task_id, 'observed_director_root' if command == 'director' else 'script_command_dispatch')
            for target, (count, evidence) in observed.items():
                db.execute('INSERT INTO observations VALUES(?,?,?,?)', (run_id, target, count, json.dumps(sorted(evidence))))
            successful_targets = []
            if result.get('taskSuccess') is True and result.get('diagnosticPassed') is True:
                if task_id and task_seen: successful_targets.append('task:' + task_id)
                # Inventory possession, seeing a mob/block, or observing a packet does not prove a successful interaction.
                for target in successful_targets:
                    db.execute('INSERT OR IGNORE INTO successes VALUES(?,?)', (run_id, target))
            for kind, identity in unknown:
                db.execute('INSERT INTO unknown_evidence VALUES(?,?,?)', (run_id, kind, identity))
            summary = {'runId': run_id, 'observedTargets': len(observed), 'successfulTargets': len(successful_targets),
                       'unknownTargets': len(unknown), 'taskSuccess': result.get('taskSuccess')}
            db.execute('INSERT INTO runs VALUES(?,?,?,?)', (run_id, fingerprint, time.time(), json.dumps(summary)))
            return summary

    def snapshot(self) -> dict:
        with self.lock, self._db() as db:
            counts = {r[0]: (r[1], r[2]) for r in db.execute('SELECT target_id,COUNT(*),SUM(count) FROM observations GROUP BY target_id')}
            successes = dict(db.execute('SELECT target_id,COUNT(*) FROM successes GROUP BY target_id'))
            targets = []
            categories = {}
            for row in db.execute('SELECT * FROM targets ORDER BY kind,id'):
                observed_runs, observations = counts.get(row['id'], (0, 0))
                success = successes.get(row['id'], 0)
                target = {'id': row['id'], 'kind': row['kind'], 'label': row['label'], 'supported': bool(row['supported']),
                          'evidenceRule': row['evidence_rule'], 'observedRuns': observed_runs,
                          'observations': observations, 'successfulRuns': success, **json.loads(row['details'])}
                targets.append(target)
                group = categories.setdefault(row['kind'], dict(requested=0, observed=0, successful=0, missing=0, unsupported=0))
                group['requested'] += 1; group['observed'] += bool(observed_runs); group['successful'] += bool(success)
                group['missing'] += not bool(observed_runs); group['unsupported'] += not bool(row['supported'])
            return {'categories': categories, 'targets': targets,
                    'missing': [t['id'] for t in targets if not t['observedRuns']],
                    'unsupported': [t['id'] for t in targets if not t['supported']],
                    'assignments': db.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],
                    'ingestedRuns': db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],
                    'unknownEvidence': [dict(r) for r in db.execute('SELECT * FROM unknown_evidence ORDER BY run_id,kind,identity')]}
