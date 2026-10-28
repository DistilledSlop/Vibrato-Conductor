"""Evidence-based coverage inventory and reproducible deficit scheduling."""
from __future__ import annotations
import hashlib
from collections import Counter, defaultdict
from contextlib import contextmanager
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from .storage import IDENTIFIER, digest, inspect_world_bank, verify_manifest

CATALOGS = Path(__file__).resolve().parents[1] / 'catalogs'
DIRECTIONS = {'C2S': 'serverbound', 'S2C': 'clientbound'}


class NoEligibleTask(ValueError):
    """An expected scheduling pause, optionally until a compatible task's cooldown expires."""
    def __init__(self, message, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


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
                 enabled_tasks: list[str] | None = None, world_bank: Path | None = None, seed: int = 424242,
                 task_failure_limit: int = 3, task_cooldown_seconds: float = 900):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path = Path(catalog_path or CATALOGS)
        self.world_bank = Path(world_bank) if world_bank is not None else None
        self.seed = int(seed)
        self.task_failure_limit = int(task_failure_limit)
        self.task_cooldown_seconds = float(task_cooldown_seconds)
        if self.task_failure_limit < 1 or not 0 <= self.task_cooldown_seconds <= 86400:
            raise ValueError('Invalid task failure cooldown settings')
        self.lock = threading.RLock()
        self.protocol_manifest = json.loads((self.catalog_path / 'protocol-767.json').read_text())
        raw_ids = {f'packet:{p["state"]}:{p["direction"]}:{p["id"]}' for p in self.protocol_manifest['packets']}
        model_path = self.catalog_path / 'model-packets.json'
        self.model_packet_catalog = json.loads(model_path.read_text()) if model_path.exists() else {'packets': []}
        self.model_packets = {}
        for packet in self.model_packet_catalog['packets']:
            role = 'output' if packet['tier'] in ('MODEL', 'HYBRID') else 'input'
            if (packet['tier'] not in ('MODEL','HYBRID','INPUT','INPUT_ACK') or packet['state'] != 'play'
                    or packet['id'] not in raw_ids or packet['role'] != role
                    or packet['id'] != f'packet:play:{packet["direction"]}:{packet["wireId"]}'):
                raise ValueError('Invalid model packet classification: ' + str(packet.get('id')))
            self.model_packets[packet['id']] = packet
        self.tasks = json.loads((self.catalog_path / 'tasks.json').read_text())['tasks']
        for task in self.tasks:
            task.setdefault('family', task['id'])
            if not isinstance(task['family'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', task['family']):
                raise ValueError('Invalid task family: ' + str(task['family']))
            task.setdefault('gamemode', 'survival')
            task.setdefault('operator', False)
            task.setdefault('operatorLevel', 2)
            if task['gamemode'] not in ('survival', 'creative', 'adventure', 'spectator'):
                raise ValueError('Invalid task gamemode: ' + str(task['gamemode']))
            if not isinstance(task['operator'], bool): raise ValueError('Task operator flag must be boolean')
            if isinstance(task['operatorLevel'], bool) or task['operatorLevel'] not in (1, 2, 3, 4):
                raise ValueError('Task operatorLevel must be 1..4')
            task.setdefault('packet_targets', [])
            task.setdefault('mechanics', [])
            if (not isinstance(task['packet_targets'], list)
                    or any(not isinstance(p, str) or p not in self.model_packets for p in task['packet_targets'])):
                raise ValueError('Task packet_targets must use canonical model-relevant Play packet IDs: ' + task['id'])
            if (not isinstance(task['mechanics'], list) or any(not isinstance(m, str)
                    or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', m) for m in task['mechanics'])):
                raise ValueError('Task mechanics must contain explicit mechanic IDs: ' + task['id'])
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
                CREATE TABLE IF NOT EXISTS task_health(task_id TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
                    last_reason TEXT, last_failure REAL);
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
        manifest = self.protocol_manifest
        for packet in manifest['packets']:
            state = packet['state'].lower()
            # The raw proxy terminates login and never captures handshake/status; those remain requested gaps.
            supported = state in ('configuration', 'play')
            model = self.model_packets.get(f'packet:{state}:{packet["direction"]}:{packet["id"]}', {})
            add(f'packet:{state}:{packet["direction"]}:{packet["id"]}', 'packet', packet['name'], supported,
                'raw_packet_bytes_with_state_direction_and_wire_id',
                {'state': state, 'direction': packet['direction'], 'wireId': packet['id'],
                 'modelRelevant': bool(model), 'readerTier': model.get('tier'), 'modelRole': model.get('role'),
                 'unsupportedReason': None if supported else 'Recorder does not preserve this protocol phase completely'})
        registries = json.loads((self.catalog_path / 'registries-1.21.1.json').read_text())
        obtainable = set((self.catalog_path / 'obtainable-items.txt').read_text().splitlines())
        entity_types = {e['name']: e.get('type') for e in registries.get('entities', [])}
        mapped_items = {target for task in self.tasks for target in task.get('targets', [])}
        action_capabilities = set()
        for task in self.tasks:
            if not task.get('supported'): continue
            if task.get('scenario') == 'creative_inventory': action_capabilities.add('item-inventory-interact:minecraft:stone')
            if task.get('scenario') == 'creative_build':
                action_capabilities.update({'item-use:minecraft:stone', 'block-statechange:minecraft:stone'})
        for kind, key, rule in [('item', 'items', 'inventory_present'), ('block', 'blocks', 'crosshair_target'), ('mob', 'mobs', 'loaded_nearby')]:
            for name in registries[key]:
                supported = kind != 'item' or name in obtainable or 'item:minecraft:' + name in mapped_items
                reason = None if supported else 'No exact matching resource name or task mapping in the existing obtainable-item catalogue'
                if kind == 'mob' and entity_types.get(name) in ('other', 'projectile', 'player', 'living'):
                    supported = False
                    reason = 'Registry category contains a non-MobEntity; current nearby-mob instrumentation cannot observe it'
                add(kind + ':minecraft:' + name, kind, name, supported, rule,
                    {'registryId': 'minecraft:' + name, 'facet': rule,
                     'unsupportedReason': reason})
                # Requested interactions stay separate from mere presence/visibility. No current heartbeat field
                # proves these actions, so they remain explicit unsupported gaps rather than receiving proxy credit.
                facets = {
                    'item': [('item-acquire', 'server_confirmed_item_acquisition'),
                             ('item-use', 'server_confirmed_item_use'),
                             ('item-inventory-interact', 'server_confirmed_inventory_interaction_with_item')],
                    'block': [('block-interact', 'server_confirmed_block_interaction'),
                              ('block-statechange', 'server_confirmed_block_state_change')],
                    'mob': [('mob-interact', 'server_confirmed_mob_interaction')],
                }[kind]
                for prefix, evidence_rule in facets:
                    identity = prefix + ':minecraft:' + name
                    capability = identity in action_capabilities
                    add(identity, kind, name + ' / ' + prefix.split('-', 1)[1], capability,
                        evidence_rule, {'registryId': 'minecraft:' + name, 'facet': prefix,
                                        'gamemodes': ['creative'] if capability else [],
                                        'unsupportedReason': None if capability else 'Requires authoritative action-specific event instrumentation; presence or inventory delta is insufficient'})
        for mode in ['survival', 'creative', 'adventure', 'spectator']:
            add('mode:' + mode, 'mode', mode, True, 'actual_observed_game_mode')
        for flag in ['creativeMode', 'allowFlying', 'flying', 'invulnerable', 'allowModifyWorld']:
            for enabled in [False, True]:
                add('ability:' + flag + ':' + str(enabled).lower(), 'permission', flag + ' = ' + str(enabled).lower(), True,
                    'actual_observed_player_ability', {'ability': flag, 'enabled': enabled})
        for operator in [False, True]:
            add('permission:' + ('operator' if operator else 'non_operator'), 'permission', 'operator' if operator else 'non-operator', True,
                'server_observed_permission_level', {'operator': operator})
        for level in range(5):
            add('permission-level:' + str(level), 'permission', 'permission level ' + str(level), level in (0, 2),
                'server_observed_permission_level',
                {'level': level, 'unsupportedReason': None if level in (0, 2) else 'No explicit curriculum scenario configures this permission level'})
        add('permission:adventure-break-denied', 'permission', 'Adventure block breaking denied', True,
            'client_break_attempt_and_confirmed_unchanged_server_block')
        add('permission:spectator-pass-through', 'permission', 'Spectator movement through solid blocks', True,
            'client_spectator_move_and_server_position_through_unchanged_solid_block')
        add('permission:operator-command-success', 'permission', 'authorized operator command result', True,
            'server_confirmed_operator_command_result')
        add('permission:operator-command-denied', 'permission', 'denied operator command result', False,
            'server_confirmed_permission_denial', {'unsupportedReason': 'No authoritative denied-command response instrumentation yet'})
        for task in self.tasks:
            details = {'command': task['command'], 'seconds': task['seconds'], 'gamemode': task['gamemode'],
                       'family': task['family'],
                       'mechanics': task['mechanics'], 'packetTargets': task['packet_targets'],
                       'operator': task['operator'], 'operatorLevel': task['operatorLevel'], 'scenario': task.get('scenario'),
                       'requiresTags': task.get('requiresTags', []), 'unsupportedReason': task.get('unsupportedReason')}
            add('task:' + task['id'], 'task', task.get('description', task['id']), task['supported'],
                'authoritative_root_task_success_not_dispatch_or_elapsed_budget', details)
            context = self._context_id(task['id'], task['gamemode'], task['operator'])
            add(context, 'task', task['id'] + ' / ' + task['gamemode'] + ' / ' + ('operator' if task['operator'] else 'non-operator'),
                task['supported'], 'observed_task_and_actual_mode_and_server_permission', {**details, 'facet': 'mode-permission-context'})
            if task.get('scenario'):
                add('scenario:' + task['scenario'], 'task', task['scenario'] + ' server-confirmed outcome', task['supported'],
                    'client_actions_plus_confirmed_server_observer', {**details, 'facet': 'scenario-outcome'})
        for mechanic in sorted({m for task in self.tasks for m in task['mechanics']}):
            declarations = [task for task in self.tasks if mechanic in task['mechanics']]
            supported = any(task['supported'] and task['command'].startswith('@activity ') for task in declarations)
            add('mechanic:' + mechanic, 'mechanic', mechanic, supported,
                'explicit_action_proof_and_finite_task_outcome',
                {'declaredBy': [task['id'] for task in declarations],
                 'unsupportedReason': None if supported else 'No focused activity proof adapter is declared'})

    @staticmethod
    def _context_id(task_id, gamemode, operator):
        return 'task-context:' + task_id + ':' + gamemode + ':' + ('operator' if operator else 'non_operator')

    def choose(self, run_id: str, worlds=None, pending_run_ids=None) -> dict:
        """Persist allocation before backend create; retries of a run id receive the same assignment."""
        if not IDENTIFIER.fullmatch(run_id): raise ValueError('Invalid run id')
        with self.lock, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT assignment FROM assignments WHERE run_id=?', (run_id,)).fetchone()
            if previous: return json.loads(previous[0])
            if isinstance(pending_run_ids, str): raise ValueError('pending_run_ids must contain explicit run ids')
            pending_ids = list(pending_run_ids or ())
            if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in pending_ids):
                raise ValueError('Invalid pending run id')
            pending_ids = sorted(set(pending_ids))
            bank = {w['id']: w for w in inspect_world_bank(self.world_bank)} if self.world_bank else {}
            if worlds:
                available = [bank.get(w, {'id': w, 'tags': [], 'supported': not bool(self.world_bank)}) if isinstance(w, str) else w for w in worlds]
            elif bank:
                available = list(bank.values())
            elif self.world_bank:
                raise NoEligibleTask('No valid world-bank entries are available')
            else:
                available = [{'id': None, 'tags': [], 'supported': True}]
            eligible = []
            cooldown_deadlines = []
            health = {r['task_id']: dict(r) for r in db.execute('SELECT * FROM task_health')}
            now = time.time()
            assigned = dict(db.execute('SELECT task_id,COUNT(*) FROM assignments GROUP BY task_id'))
            families = {task['id']: task['family'] for task in self.tasks}
            family_assigned = Counter()
            for task_id, count in assigned.items(): family_assigned[families.get(task_id, task_id)] += count
            world_assigned, task_world_assigned = Counter(), Counter()
            for record in db.execute("SELECT task_id,json_extract(assignment,'$.world_id') AS world_id,COUNT(*) AS n FROM assignments GROUP BY task_id,world_id"):
                world_assigned[record['world_id']] += record['n']
                task_world_assigned[record['task_id'], record['world_id']] += record['n']
            # Only the controller knows which reservations are actually pending/live. Failed creates
            # and terminal un-ingested runs must never become permanent inferred active reservations.
            pending_task, pending_family, pending_world = Counter(), Counter(), Counter()
            if pending_ids:
                placeholders = ','.join('?' for _ in pending_ids)
                for record in db.execute('SELECT task_id,assignment FROM assignments WHERE run_id IN (' + placeholders + ')', pending_ids):
                    task_id = record['task_id']
                    pending_task[task_id] += 1
                    pending_family[families.get(task_id, task_id)] += 1
                    pending_world[json.loads(record['assignment']).get('world_id')] += 1
            counts = dict(db.execute('SELECT target_id,COUNT(*) FROM observations GROUP BY target_id'))
            successes = dict(db.execute('SELECT target_id,COUNT(*) FROM successes GROUP BY target_id'))
            for task in self.tasks:
                task_health = health.get(task['id'], {})
                if not task['supported'] or self.enabled_tasks is not None and task['id'] not in self.enabled_tasks: continue
                for world in available:
                    if not world.get('supported', True) or world.get('minecraftVersion', '1.21.1') != '1.21.1': continue
                    if not set(task.get('requiresTags', [])).issubset(world.get('tags', [])): continue
                    if task['gamemode'] not in world.get('gamemodes', ['survival', 'creative', 'adventure', 'spectator']): continue
                    if task['operator'] and world.get('allowOperator', True) is not True: continue
                    cooldown_until = task_health.get('cooldown_until', 0)
                    if cooldown_until > now:
                        cooldown_deadlines.append(cooldown_until)
                        continue
                    targets = list(dict.fromkeys(['task:' + task['id'], self._context_id(task['id'], task['gamemode'], task['operator']),
                        *task.get('targets', []), *task['packet_targets'], *('mechanic:' + m for m in task['mechanics'])]))
                    deficit = sum(1 / (1 + counts.get(t, 0) + 2 * successes.get(t, 0)) for t in targets) / len(targets)
                    score = deficit / ((1 + assigned.get(task['id'], 0)) * (1 + task_health.get('consecutive_failures', 0)))
                    tie = hashlib.sha256(f'{self.seed}:{run_id}:{task["id"]}:{world.get("id")}'.encode()).hexdigest()
                    eligible.append({'score': score, 'tie': tie, 'task': task, 'world': world,
                                     'targets': targets, 'deficit': deficit / (1 + task_health.get('consecutive_failures', 0))})
            if not eligible:
                raise NoEligibleTask('No supported task matches the available worlds and scheduling filters',
                                     retry_at=min(cooldown_deadlines, default=None))
            # Average each family's task deficits: a family with many catalogue variants must not
            # receive more slots merely because it contains more individually underrepresented tasks.
            family_tasks = defaultdict(dict)
            for row in eligible: family_tasks[row['task']['family']][row['task']['id']] = row
            family = max(family_tasks, key=lambda name: (
                -pending_family[name],
                sum(row['deficit'] for row in family_tasks[name].values()) / len(family_tasks[name]) / (1 + family_assigned[name]),
                hashlib.sha256(f'{self.seed}:{run_id}:family:{name}'.encode()).hexdigest()))
            task_row = max(family_tasks[family].values(), key=lambda row: (
                -pending_task[row['task']['id']], row['score'],
                hashlib.sha256(f'{self.seed}:{run_id}:task:{row["task"]["id"]}'.encode()).hexdigest()))
            task = task_row['task']
            selected = max((row for row in eligible if row['task']['id'] == task['id']), key=lambda row: (
                -pending_world[row['world'].get('id')],
                -task_world_assigned[task['id'], row['world'].get('id')],
                -world_assigned[row['world'].get('id')], row['tie']))
            world, targets = selected['world'], selected['targets']
            value = int.from_bytes(hashlib.sha256(f'{self.seed}:{run_id}'.encode()).digest()[:8], 'big') >> 1
            assignment = {'task': task['command'], 'task_id': task['id'], 'family': task['family'], 'seconds': task['seconds'],
                          'seed': value, 'world_id': world.get('id'), 'target_ids': targets,
                          'world_revision': world.get('revision'), 'requires_tags': task.get('requiresTags', []),
                          'gamemode': task['gamemode'], 'operator': task['operator'], 'operatorLevel': task['operatorLevel'], 'scenario': task.get('scenario')}
            db.execute('INSERT INTO assignments VALUES(?,?,?,?)', (run_id, task['id'], json.dumps(assignment), time.time()))
            return assignment

    def ingest(self, run_dir: Path) -> dict:
        run_dir = Path(run_dir)
        result = verify_manifest(run_dir)
        metadata = json.loads((run_dir / 'metadata.json').read_text())
        run_id = result.get('runId') or metadata.get('runId') or run_dir.name
        if not IDENTIFIER.fullmatch(run_id) or run_dir.name != run_id or metadata.get('runId', run_id) != run_id:
            raise ValueError('Coverage run identity does not match artifact directory')
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
                try: status = json.loads(status_path.read_text())
                except (OSError, ValueError):
                    unknown.add(('artifact', 'malformed_capture_marker:' + status_path.name)); continue
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
            observed_contexts = set()
            proven_interactions = set()
            heartbeats = run_dir / 'heartbeats.jsonl'
            if heartbeats.exists():
                with heartbeats.open() as lines:
                    for line in lines:
                        try: heartbeat = json.loads(line)
                        except ValueError:
                            unknown.add(('artifact', 'malformed_heartbeat_line')); continue
                        if not isinstance(heartbeat, dict):
                            unknown.add(('artifact', 'invalid_heartbeat_shape')); continue
                        observed_director = observed_director or heartbeat.get('rootClass') == 'DirectorTask'
                        actual_mode = heartbeat.get('actualMode')
                        if actual_mode in ('survival', 'creative', 'adventure', 'spectator'):
                            record('mode:' + actual_mode, 'heartbeat:actual_mode')
                        abilities = heartbeat.get('abilities') or {}
                        if isinstance(abilities, dict):
                            for flag in ['creativeMode', 'allowFlying', 'flying', 'invulnerable', 'allowModifyWorld']:
                                if isinstance(abilities.get(flag), bool):
                                    record('ability:' + flag + ':' + str(abilities[flag]).lower(), 'heartbeat:actual_ability')
                        permission_level = heartbeat.get('permissionLevel')
                        if (heartbeat.get('permissionSource') == 'server_observer' and isinstance(permission_level, int)
                                and not isinstance(permission_level, bool) and 0 <= permission_level <= 4):
                            operator = permission_level >= 1
                            record('permission:' + ('operator' if operator else 'non_operator'), 'heartbeat:server_permission')
                            record('permission-level:' + str(permission_level), 'heartbeat:server_permission')
                            if actual_mode in ('survival', 'creative', 'adventure', 'spectator'):
                                observed_contexts.add((actual_mode, operator, permission_level))
                        semantics = heartbeat.get('observationSemantics', {})
                        observations = heartbeat.get('observations', {})
                        if not isinstance(semantics, dict) or not isinstance(observations, dict):
                            unknown.add(('artifact', 'invalid_observation_shape')); continue
                        for plural, kind, rule in [('items','item','inventory_present'),('blocks','block','crosshair_target'),('mobs','mob','loaded_nearby')]:
                            # Require the instrumentation's declared actual observation semantics.
                            if semantics.get(plural) != rule: continue
                            values = observations.get(plural, [])
                            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                                unknown.add(('artifact', 'invalid_observation_values:' + plural)); continue
                            for identifier in set(values):
                                if isinstance(identifier, str) and re.fullmatch(r'[a-z0-9_.-]+:[a-z0-9_./-]+', identifier):
                                    record(kind + ':' + identifier, 'heartbeat:' + rule)
            assignment = db.execute('SELECT task_id FROM assignments WHERE run_id=?', (run_id,)).fetchone()
            task_id = assignment[0] if assignment else next((t['id'] for t in self.tasks if t['command'] == metadata.get('assignment')), None)
            script_path = run_dir / 'script-result.json'
            try: script = json.loads(script_path.read_text()) if script_path.exists() else {}
            except ValueError:
                unknown.add(('artifact', 'malformed_script_result')); script = {}
            if not isinstance(script, dict): script = {}
            command = next((t['command'] for t in self.tasks if t['id'] == task_id), None)
            task_seen = (command == 'director' and observed_director) or any(
                c.get('action') == command and c.get('status') in ('dispatched', 'completed')
                for c in script.get('commands', []))
            task_definition = next((t for t in self.tasks if t['id'] == task_id), None)
            actual_context = bool(task_definition and (task_definition['gamemode'], task_definition['operator'],
                task_definition['operatorLevel'] if task_definition['operator'] else 0) in observed_contexts)
            scenario_proven = False
            if task_id and task_seen:
                record('task:' + task_id, 'observed_director_root' if command == 'director' else 'script_command_dispatch')
                if actual_context:
                    record(self._context_id(task_id, task_definition['gamemode'], task_definition['operator']),
                           'observed_task_and_server_mode_permission')
            scenario_path = run_dir / 'scenario-result.json'
            if task_definition and task_definition.get('scenario') and scenario_path.exists():
                try: scenario = json.loads(scenario_path.read_text())
                except ValueError:
                    unknown.add(('artifact', 'malformed_scenario_result')); scenario = {}
                evidence = scenario.get('evidence') or {} if isinstance(scenario, dict) else {}
                scenario_proven = (isinstance(scenario, dict) and isinstance(evidence, dict) and task_seen and actual_context
                    and scenario.get('scenario') == task_definition['scenario'] and scenario.get('status') == 'completed'
                    and scenario.get('source') == 'client_actions_plus_server_observer' and evidence.get('serverConfirmed') is True)
                if scenario_proven:
                    record('scenario:' + task_definition['scenario'], 'scenario:client_actions_and_server_confirmation')
                    if task_definition['scenario'] == 'adventure_inspect':
                        record('permission:adventure-break-denied', 'scenario:confirmed_adventure_restriction')
                        proven_interactions.add('permission:adventure-break-denied')
                    elif task_definition['scenario'] == 'spectator_explore':
                        record('permission:spectator-pass-through', 'scenario:confirmed_spectator_movement')
                        proven_interactions.add('permission:spectator-pass-through')
                    events = evidence.get('events', [])
                    if isinstance(events, list):
                        prefixes = {'item_acquire': 'item-acquire', 'item_use': 'item-use',
                                    'inventory_interact': 'item-inventory-interact', 'block_interact': 'block-interact',
                                    'block_statechange': 'block-statechange', 'mob_interact': 'mob-interact'}
                        for event in events:
                            if not isinstance(event, dict) or event.get('serverConfirmed') is not True or event.get('success') is not True: continue
                            prefix, identifier = prefixes.get(event.get('kind')), event.get('id')
                            if prefix and isinstance(identifier, str) and re.fullmatch(r'[a-z0-9_.-]+:[a-z0-9_./-]+', identifier):
                                target = prefix + ':' + identifier
                                record(target, 'scenario:server_confirmed_action')
                                if target in known: proven_interactions.add(target)
                    operator_command = evidence.get('operatorCommand') or {}
                    if (task_definition['operator'] and isinstance(operator_command, dict)
                            and operator_command.get('serverConfirmed') is True and operator_command.get('success') is True
                            and operator_command.get('command')):
                        record('permission:operator-command-success', 'scenario:server_confirmed_command_result')
                        proven_interactions.add('permission:operator-command-success')
            activity_path = run_dir / 'activity-result.json'
            if task_seen and command and command.startswith('@activity ') and activity_path.exists():
                try: activity = json.loads(activity_path.read_text())
                except ValueError:
                    unknown.add(('artifact', 'malformed_activity_result')); activity = {}
                expected_activity = command.split()[1]
                if (isinstance(activity, dict) and activity.get('activity') == expected_activity
                        and activity.get('source') == 'focused_activity_task'):
                    evidence = activity.get('evidence') or {}
                    events = evidence.get('mechanics', []) if isinstance(evidence, dict) else []
                    if isinstance(events, list):
                        for event in events:
                            if (not isinstance(event, dict) or event.get('id') not in task_definition['mechanics']
                                    or event.get('actionObserved') is not True): continue
                            source = event.get('source')
                            if source == 'server_observer':
                                if event.get('serverConfirmed') is not True: continue
                            elif source != 'client_action_and_task_outcome': continue
                            target = 'mechanic:' + event['id']
                            record(target, 'activity:' + expected_activity + ':' + source)
                            if (activity.get('status') == 'completed' and event.get('success') is True
                                    and event.get('goalTaskFinished') is True):
                                proven_interactions.add(target)
            for target, (count, evidence) in observed.items():
                db.execute('INSERT INTO observations VALUES(?,?,?,?)', (run_id, target, count, json.dumps(sorted(evidence))))
            successful_targets = []
            if result.get('taskSuccess') is True and result.get('diagnosticPassed') is True:
                if task_id and task_seen:
                    successful_targets.append('task:' + task_id)
                    if actual_context:
                        successful_targets.append(self._context_id(task_id, task_definition['gamemode'], task_definition['operator']))
                    if scenario_proven: successful_targets.append('scenario:' + task_definition['scenario'])
                successful_targets.extend(proven_interactions)
                # Inventory possession, seeing a mob/block, or observing a packet does not prove a successful interaction.
                for target in successful_targets:
                    db.execute('INSERT OR IGNORE INTO successes VALUES(?,?)', (run_id, target))
            for kind, identity in unknown:
                db.execute('INSERT INTO unknown_evidence VALUES(?,?,?)', (run_id, kind, identity))
            if task_id and task_seen:
                reason = str(result.get('reason', 'unknown'))
                failure = result.get('taskSuccess') is False or result.get('diagnosticPassed') is False
                operator_or_host = any(token in reason.lower() for token in ('operator', 'scale_down', 'overload', 'storage_limit', 'replaced', 'runtime_', 'killed', 'crash', 'exited'))
                db.execute('INSERT OR IGNORE INTO task_health(task_id) VALUES(?)', (task_id,))
                if result.get('taskSuccess') is True and result.get('diagnosticPassed') is True:
                    db.execute('UPDATE task_health SET consecutive_failures=0,cooldown_until=0,last_reason=NULL WHERE task_id=?', (task_id,))
                elif failure and not operator_or_host:
                    previous = db.execute('SELECT consecutive_failures FROM task_health WHERE task_id=?', (task_id,)).fetchone()[0]
                    consecutive = previous + 1
                    cooldown = time.time() + min(86400, self.task_cooldown_seconds * 2**min(consecutive - self.task_failure_limit, 6)) if consecutive >= self.task_failure_limit else 0
                    db.execute('UPDATE task_health SET failures=failures+1,consecutive_failures=?,cooldown_until=?,last_reason=?,last_failure=? WHERE task_id=?',
                               (consecutive, cooldown, reason, time.time(), task_id))
            summary = {'runId': run_id, 'observedTargets': len(observed), 'successfulTargets': len(successful_targets),
                       'unknownTargets': len(unknown), 'taskSuccess': result.get('taskSuccess')}
            db.execute('INSERT INTO runs VALUES(?,?,?,?)', (run_id, fingerprint, time.time(), json.dumps(summary)))
            return summary

    def snapshot(self) -> dict:
        with self.lock, self._db() as db:
            counts = {r[0]: (r[1], r[2]) for r in db.execute('SELECT target_id,COUNT(*),SUM(count) FROM observations GROUP BY target_id')}
            successes = dict(db.execute('SELECT target_id,COUNT(*) FROM successes GROUP BY target_id'))
            health = {r['task_id']: dict(r) for r in db.execute('SELECT * FROM task_health')}
            targets = []
            categories = {}
            for row in db.execute('SELECT * FROM targets ORDER BY kind,id'):
                observed_runs, observations = counts.get(row['id'], (0, 0))
                success = successes.get(row['id'], 0)
                details = json.loads(row['details'])
                action_proven = bool(observed_runs) and row['evidence_rule'].startswith('server_confirmed_')
                supported = bool(row['supported']) or action_proven
                if action_proven: details.update({'unsupportedReason': None, 'supportProvenByObservation': True})
                target = {'id': row['id'], 'kind': row['kind'], 'label': row['label'], 'supported': supported,
                          'evidenceRule': row['evidence_rule'], 'observedRuns': observed_runs,
                          'observations': observations, 'successfulRuns': success, **details}
                if row['kind'] == 'task':
                    record = health.get(row['id'].removeprefix('task:'), {})
                    target['failures'] = record.get('failures', 0)
                    target['cooldownUntil'] = record.get('cooldown_until', 0)
                targets.append(target)
                group = categories.setdefault(row['kind'], dict(requested=0, observed=0, successful=0, missing=0, unsupported=0))
                group['requested'] += 1; group['observed'] += bool(observed_runs); group['successful'] += bool(success)
                group['missing'] += not bool(observed_runs); group['unsupported'] += not supported
            model_targets = [{**target, 'readerName': self.model_packets[target['id']]['name']}
                             for target in targets if target['id'] in self.model_packets]
            def packet_stats(rows):
                return {'requested': len(rows), 'observed': sum(bool(row['observedRuns']) for row in rows),
                        'missing': sum(not bool(row['observedRuns']) for row in rows),
                        'missingTargets': [row['id'] for row in rows if not row['observedRuns']]}
            by_task = defaultdict(dict)
            for row in db.execute("""SELECT substr(t.target_id,6) AS task_id,p.target_id,
                    COUNT(*) AS observed_runs,SUM(p.count) AS observations
                    FROM observations t JOIN observations p ON p.run_id=t.run_id
                    WHERE t.target_id LIKE 'task:%' AND p.target_id LIKE 'packet:%'
                    GROUP BY t.target_id,p.target_id"""):
                by_task[row['task_id']][row['target_id']] = {'id': row['target_id'],
                    'observedRuns': row['observed_runs'], 'observations': row['observations']}
            task_definitions = {task['id']: task for task in self.tasks}
            task_packets = []
            for task_id in sorted(task_definitions.keys() | by_task.keys()):
                task = task_definitions.get(task_id, {})
                actual = by_task.get(task_id, {})
                requested = task.get('packet_targets', [])
                task_packets.append({'taskId': task_id, 'family': task.get('family'),
                    'packetTargets': requested, 'targetedObserved': [identifier for identifier in requested if identifier in actual],
                    'rawPacketTypesObserved': len(actual),
                    'packets': [{**value, 'readerTier': self.model_packets[identifier]['tier'],
                                 'modelRole': self.model_packets[identifier]['role']}
                                for identifier, value in sorted(actual.items()) if identifier in self.model_packets]})
            model_coverage = {**packet_stats(model_targets), 'targets': model_targets,
                'output': packet_stats([row for row in model_targets if row['modelRole'] == 'output']),
                'input': packet_stats([row for row in model_targets if row['modelRole'] == 'input']),
                'byTask': task_packets, 'source': self.model_packet_catalog.get('source'),
                'evidenceRule': 'actual_raw_wire_presence_in_reader_relevant_Play_subset',
                'limitations': ['Wire presence does not prove a HYBRID model-owned branch or a decoded training row',
                    'Per-task packet counts describe runs with actual task dispatch; bootstrap/incidental traffic is included and causality is unproven']}
            return {'categories': categories, 'targets': targets, 'taskHealth': list(health.values()),
                    'modelPacketCoverage': model_coverage,
                    'missing': [t['id'] for t in targets if not t['observedRuns']],
                    'unsupported': [t['id'] for t in targets if not t['supported']],
                    'assignments': db.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],
                    'ingestedRuns': db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],
                    'unknownEvidence': [dict(r) for r in db.execute('SELECT * FROM unknown_evidence ORDER BY run_id,kind,identity')]}
