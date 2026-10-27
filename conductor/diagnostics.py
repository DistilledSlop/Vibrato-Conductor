"""Conservative behavior watchdogs based on task, outcome and world evidence, never motion alone."""
from __future__ import annotations
from collections import Counter, deque
import math
from statistics import mean


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


class Watchdog:
    def __init__(self, stall_seconds=600, loop_seconds=90, pillar_seconds=45):
        self.stall_seconds = stall_seconds
        self.loop_seconds = loop_seconds
        self.pillar_seconds = pillar_seconds
        self.history = deque(maxlen=360)
        self.server_tps = deque(maxlen=10)
        self.client_tps = deque(maxlen=10)
        self.last_client_seconds = None
        self.last_progress = None
        self.last_semantic = None
        self.seen_chunks = set()
        self.last_server_tick = None
        self.started = None
        self.idle_since = None

    @staticmethod
    def assessment(reason, kind, confidence, **evidence):
        return {'reason': reason, 'kind': kind, 'confidence': confidence, 'evidence': evidence}

    def observe(self, client, server, resources, now):
        if not client: return None
        if self.started is None: self.started = now
        if server and server.get('ticks') != self.last_server_tick and finite(server.get('tps')):
            self.server_tps.append(server['tps'])
            self.last_server_tick = server.get('ticks')
        if client.get('seconds') != self.last_client_seconds and finite(client.get('tps')):
            self.client_tps.append(client['tps'])
            self.last_client_seconds = client.get('seconds')
        # Timing or resource pressure takes precedence over conclusions about bot behavior.
        if len(self.server_tps) == 10 and mean(self.server_tps) < 18 and now - self.started >= 20:
            return self.assessment('server_tick_overload', 'overload', 'inferred',
                                   meanServerTps=mean(self.server_tps), samples=10,
                                   cpuPressure=resources.get('cpuPressure'), memoryPressure=resources.get('memoryPressure'))
        if len(self.client_tps) == 10 and mean(self.client_tps) < 18 and now - self.started >= 20:
            return self.assessment('client_tick_overload', 'overload', 'inferred',
                                   meanClientTps=mean(self.client_tps), samples=10,
                                   cpuPressure=resources.get('cpuPressure'), memoryPressure=resources.get('memoryPressure'))
        if resources.get('memoryPressure') and resources.get('oomKills', 0) > 0:
            return self.assessment('worker_memory_exhausted', 'overload', 'confirmed', **resources)
        semantic = (tuple(sorted(client.get('inventory', {}).items())), client.get('blocksBrokenCount'),
                    client.get('taskCompletedCount'), client.get('dimension'), client.get('experienceLevel'),
                    client.get('experienceProgress'))
        description = ' '.join(str(client.get(k) or '') for k in ('rootClass', 'leafClass', 'leaf')).lower()
        chunk = tuple(client.get('chunk') or ())
        exploring = any(word in description for word in ('wander', 'explor', 'travel', 'locate', 'gettox', 'get2'))
        new_chunk = bool(exploring and chunk and chunk not in self.seen_chunks)
        if new_chunk:
            if len(self.seen_chunks) >= 4096: self.seen_chunks.clear()
            self.seen_chunks.add(chunk)
        if semantic != self.last_semantic or new_chunk or self.last_progress is None:
            self.last_progress = now
            self.last_semantic = semantic
        entry = {'time': now, 'position': client.get('position'), 'grounded': client.get('onGround'),
                 'pillarAttempts': client.get('pillarAttempts', 0), 'movement': client.get('movement')}
        self.history.append(entry)
        no_progress = now - self.last_progress
        # A server or client under timing pressure cannot establish a behavior failure.
        if resources.get('memoryPressure') or (finite(client.get('tps')) and client['tps'] < 15): return None
        if not client.get('rootTask') and not client.get('leafClass'):
            if self.idle_since is None: self.idle_since = now
            if now - self.idle_since >= 60:
                return self.assessment('idle_without_active_task', 'behavior', 'inferred', idleSeconds=now-self.idle_since)
        else:
            self.idle_since = None
        if client.get('taskFailure'):
            return self.assessment('task_reported_failure', 'behavior', 'confirmed', failure=client['taskFailure'])
        starts = [s for s in client.get('recentTaskStarts', [])
                  if finite(s.get('seconds')) and 0 <= client.get('seconds', 0) - s['seconds'] <= 10
                  and s.get('taskClass') == client.get('leafClass')
                  and str(s.get('task')).split('>')[0] == str(client.get('leaf')).split('>')[0]]
        target_counts = Counter((s.get('taskClass'), str(s.get('task')).split('>')[0]) for s in starts)
        if no_progress >= 10 and target_counts and max(target_counts.values()) >= 8:
            target, count = target_counts.most_common(1)[0]
            return self.assessment('repeated_task_restart', 'behavior', 'inferred',
                                   taskClass=target[0], task=target[1], starts=count, windowSeconds=10,
                                   noSemanticProgressSeconds=no_progress)
        pillar = [p for p in self.history if now - p['time'] <= self.pillar_seconds]
        if len(pillar) >= 3 and pillar[-1]['time'] - pillar[0]['time'] >= self.pillar_seconds - 3:
            grounded_y = [p['position'][1] for p in pillar if p['grounded'] and p['position']]
            attempts = pillar[-1]['pillarAttempts'] - pillar[0]['pillarAttempts']
            if (attempts >= 5 and len(grounded_y) >= 3 and max(grounded_y) - min(grounded_y) < 0.8
                    and sum(p['movement'] == 'MovementPillar' for p in pillar) >= 3):
                return self.assessment('nerd_pole_no_height_gain', 'behavior', 'inferred',
                                       pillarAttempts=attempts, groundedHeightRange=max(grounded_y)-min(grounded_y),
                                       windowSeconds=pillar[-1]['time']-pillar[0]['time'])
        window = [p for p in self.history if now - p['time'] <= self.loop_seconds and p['position']]
        if no_progress >= self.loop_seconds and len(window) >= 10:
            positions = [p['position'] for p in window]
            cells = Counter((int(p[0] // 2), int(p[1] // 2), int(p[2] // 2)) for p in positions)
            traveled = sum(math.dist(a, b) for a, b in zip(positions, positions[1:]))
            if traveled >= 20 and len(cells) <= 6 and max(cells.values()) >= 4:
                return self.assessment('movement_loop_without_progress', 'behavior', 'inferred',
                                       traveledBlocks=traveled, cellsVisited=len(cells),
                                       noSemanticProgressSeconds=no_progress)
        if no_progress >= self.stall_seconds and client.get('leafClass'):
            # Idle at one location alone never triggers this; the task and full semantic window are required.
            return self.assessment('task_semantic_stall', 'behavior', 'inferred',
                                   leaf=client.get('leaf'), root=client.get('rootTask'),
                                   noSemanticProgressSeconds=no_progress,
                                   semanticSignals=['inventory', 'block_breaks', 'completed_tasks', 'dimension', 'experience', 'novel_exploration_chunks'])
        return None


def task_success(task, script):
    """True only for a completed dispatched command and matching non-cancelled task outcome."""
    if task == 'director': return None
    commands = [c for c in script.get('commands', []) if c.get('action') == task]
    if not commands: return None
    command = commands[-1]
    if command.get('status') in ('error', 'timeout'): return False
    if any(c.get('status') in ('error', 'timeout') for c in script.get('commands', [])): return False
    if command.get('status') != 'completed': return False if script.get('status') == 'exit' else None
    after = command.get('seconds', 0)
    outcomes = [o for o in script.get('taskOutcomes', []) if o.get('seconds', -1) >= after]
    if not outcomes: return None
    if any(o.get('status') in ('failed', 'cancelled') for o in outcomes): return False
    if not any(o.get('status') == 'completed' for o in outcomes): return None
    if task == '@nether' and script.get('finalDimension') != 'minecraft:the_nether': return False
    if task == '@gamer' and not script.get('creditsVisible'): return False
    return True


def script_failure_reason(task, script):
    """Keep task budget/command failures distinguishable from a normal client exit."""
    if script.get('status') == 'load_error': return 'script_load_error'
    commands = script.get('commands', [])
    if any(c.get('status') == 'timeout' for c in commands): return 'task_budget_exceeded'
    if any(c.get('status') == 'error' for c in commands): return 'task_command_error'
    if task != 'director':
        dispatch = next((c.get('seconds', 0) for c in reversed(commands) if c.get('action') == task), 0)
        outcomes = [o for o in script.get('taskOutcomes', []) if o.get('seconds', -1) >= dispatch]
        if any(o.get('status') == 'failed' for o in outcomes): return 'task_reported_failure'
        if any(o.get('status') == 'cancelled' for o in outcomes): return 'task_cancelled'
        if task_success(task, script) is not True: return 'task_success_unconfirmed'
    return None
