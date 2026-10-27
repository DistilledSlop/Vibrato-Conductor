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

from .storage import IDENTIFIER, digest, inspect_world_bank, verify_manifest

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
        self.tasks = json.loads((self.catalog_path / 'tasks.json').read_text())['tasks']
        for task in self.tasks:
            task.setdefault('gamemode', 'survival')
            task.setdefault('operator', False)
            task.setdefault('operatorLevel', 2)
            if task['gamemode'] not in ('survival', 'creative', 'adventure', 'spectator'):
                raise ValueError('Invalid task gamemode: ' + str(task['gamemode']))
            if not isinstance(task['operator'], bool): raise ValueError('Task operator flag must be boolean')
            if isinstance(task['operatorLevel'], bool) or task['operatorLevel'] not in (1, 2, 3, 4):
                raise ValueError('Task operatorLevel must be 1..4')
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

    @staticmethod
    def _context_id(task_id, gamemode, operator):
        return 'task-context:' + task_id + ':' + gamemode + ':' + ('operator' if operator else 'non_operator')

    def choose(self, run_id: str, worlds=None) -> dict:
        """Persist allocation before backend create; retries of a run id receive the same assignment."""
        if not IDENTIFIER.fullmatch(run_id): raise ValueError('Invalid run id')
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
            health = {r['task_id']: dict(r) for r in db.execute('SELECT * FROM task_health')}
            now = time.time()
            assigned = dict(db.execute('SELECT task_id,COUNT(*) FROM assignments GROUP BY task_id'))
            counts = dict(db.execute('SELECT target_id,COUNT(*) FROM observations GROUP BY target_id'))
            successes = dict(db.execute('SELECT target_id,COUNT(*) FROM successes GROUP BY target_id'))
            for task in self.tasks:
                task_health = health.get(task['id'], {})
                if task_health.get('cooldown_until', 0) > now: continue
                if not task['supported'] or self.enabled_tasks is not None and task['id'] not in self.enabled_tasks: continue
                for world in available:
                    if not world.get('supported', True) or world.get('minecraftVersion', '1.21.1') != '1.21.1': continue
                    if not set(task.get('requiresTags', [])).issubset(world.get('tags', [])): continue
                    if task['gamemode'] not in world.get('gamemodes', ['survival', 'creative', 'adventure', 'spectator']): continue
                    if task['operator'] and world.get('allowOperator', True) is not True: continue
                    targets = ['task:' + task['id'], self._context_id(task['id'], task['gamemode'], task['operator']), *task.get('targets', [])]
                    deficit = sum(1 / (1 + counts.get(t, 0) + 2 * successes.get(t, 0)) for t in targets) / len(targets)
                    score = deficit / ((1 + assigned.get(task['id'], 0)) * (1 + task_health.get('consecutive_failures', 0)))
                    tie = hashlib.sha256(f'{self.seed}:{run_id}:{task["id"]}:{world.get("id")}'.encode()).hexdigest()
                    eligible.append((score, tie, task, world, targets))
            if not eligible: raise ValueError('No supported task matches the available worlds and scheduling filters')
            _, _, task, world, targets = max(eligible, key=lambda row: (row[0], row[1]))
            value = int.from_bytes(hashlib.sha256(f'{self.seed}:{run_id}'.encode()).digest()[:8], 'big') >> 1
            assignment = {'task': task['command'], 'task_id': task['id'], 'seconds': task['seconds'],
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
                            operator = permission_level >= 2
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
            return {'categories': categories, 'targets': targets, 'taskHealth': list(health.values()),
                    'missing': [t['id'] for t in targets if not t['observedRuns']],
                    'unsupported': [t['id'] for t in targets if not t['supported']],
                    'assignments': db.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],
                    'ingestedRuns': db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],
                    'unknownEvidence': [dict(r) for r in db.execute('SELECT * FROM unknown_evidence ORDER BY run_id,kind,identity')]}
