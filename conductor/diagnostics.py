"""Conservative behavior watchdogs based on task, outcome and world evidence, never motion alone."""
from __future__ import annotations
from collections import Counter, deque
import math
from statistics import mean


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


class IntervalRate:
    """Cumulative counter deltas avoid aliasing one-second snapshots sampled less frequently."""
    def __init__(self, window=10):
        self.previous = None
        self.intervals = deque(maxlen=window)
        self.total_count = 0
        self.total_seconds = 0.0
        self.samples = 0
        self.latest = None
        self.window = window
        self.degraded_windows = 0

    def update(self, count, seconds):
        if not finite(count) or not finite(seconds): return None
        current = (count, seconds)
        if self.previous is None:
            self.previous = current
            return None
        change, elapsed = count-self.previous[0], seconds-self.previous[1]
        if change == 0 and elapsed == 0: return self.latest
        self.previous = current
        if change < 0 or elapsed <= 0:
            self.intervals.clear()
            self.latest = None
            return None
        self.intervals.append((change, elapsed))
        self.total_count += change
        self.total_seconds += elapsed
        self.samples += 1
        self.latest = change / elapsed
        if self.rolling is not None and self.rolling < 18:
            self.degraded_windows += 1
        return self.latest

    @property
    def rolling(self):
        if len(self.intervals) < self.window: return None
        return sum(c for c,t in self.intervals) / sum(t for c,t in self.intervals)

    @property
    def average(self):
        return self.total_count/self.total_seconds if self.total_seconds else None

    def summary(self):
        return {'samples': self.samples, 'ticks': self.total_count, 'seconds': self.total_seconds,
                'meanTps': self.average, 'method': 'cumulative_counter_delta_weighted_elapsed',
                'degradedWindows': self.degraded_windows,
                'valid': self.samples >= 10 and self.average is not None and self.average >= 18
                         and self.degraded_windows == 0}


def native_thread_failure(text):
    """Require explicit JVM/native spawn failure; heap pressure and unrelated exceptions are distinct."""
    for line in text.splitlines():
        if ('OutOfMemoryError: unable to create native thread' in line
                or ('pthread_create failed' in line and 'EAGAIN' in line)):
            return {'source': 'jvm_log', 'message': line.strip()[:500]}
    return None


class Watchdog:
    def __init__(self, stall_seconds=600, loop_seconds=90, pillar_seconds=45, *, assigned_task=None, settle_seconds=0):
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
        self.assigned_task = assigned_task
        self.settle_seconds = settle_seconds
        self.settle_started = None
        self.overload_since = {'server': None, 'client': None}

    @staticmethod
    def assessment(reason, kind, confidence, **evidence):
        return {'reason': reason, 'kind': kind, 'confidence': confidence, 'evidence': evidence}

    @classmethod
    def infrastructure(cls, resources):
        proof = resources.get('nativeThreadFailure')
        if proof:
            return cls.assessment('worker_native_thread_exhausted', 'infrastructure', 'confirmed',
                                  nativeThreadFailure=proof, pidsCurrent=resources.get('pidsCurrent'),
                                  pidsMax=resources.get('pidsMax'), pidsLimitEvents=resources.get('pidsLimitEvents'))
        return None

    def observe(self, client, server, resources, now):
        infrastructure = self.infrastructure(resources)
        if infrastructure: return infrastructure
        if resources.get('memoryPressure') and resources.get('oomKills', 0) > 0:
            return self.assessment('worker_memory_exhausted', 'overload', 'confirmed', **resources)
        if not client: return None
        if self.started is None: self.started = now
        if server and server.get('ticks') != self.last_server_tick and finite(server.get('tps')):
            self.server_tps.append(server['tps'])
            self.last_server_tick = server.get('ticks')
        if client.get('seconds') != self.last_client_seconds and finite(client.get('tps')):
            self.client_tps.append(client['tps'])
            self.last_client_seconds = client.get('seconds')
        # Timing or resource pressure takes precedence over conclusions about bot behavior.
        server_mean = (server.get('rollingTps') if server else None) if server and 'rollingSamples' in server else (mean(self.server_tps) if len(self.server_tps)==10 else None)
        client_mean = client.get('rollingTps') if 'rollingSamples' in client else (mean(self.client_tps) if len(self.client_tps)==10 else None)
        pending_overload = False
        for side, value in (('server', server_mean), ('client', client_mean)):
            if finite(value) and value < 18 and now - self.started >= 20:
                if self.overload_since[side] is None: self.overload_since[side] = now
                duration = now - self.overload_since[side]
                if duration >= 30:
                    return self.assessment(side + '_tick_overload', 'overload', 'inferred',
                        **{'mean' + side.title() + 'Tps': value}, samples=10,
                        sustainedSeconds=duration, terminationGraceSeconds=30,
                        cpuPressure=resources.get('cpuPressure'), memoryPressure=resources.get('memoryPressure'))
                pending_overload = True
            else:
                self.overload_since[side] = None
        if pending_overload:
            # Timing uncertainty cannot establish a behavior failure during its recovery interval.
            self.last_progress = now
            self.idle_since = None
            self.history.clear()
            return None
        # Only the configured assignment's successful idle barrier and bounded final EXIT permit idle.
        remaining = client.get('settleRemainingSeconds')
        planned_settle = (self.assigned_task is not None and self.assigned_task.startswith('@')
            and self.settle_seconds > 0 and client.get('scriptPhase') == 'post_task_settle'
            and client.get('assignedTask') == self.assigned_task and client.get('assignedTaskStatus') == 'completed'
            and client.get('settleSource') == 'completed_command_idle_barrier_pending_exit'
            and client.get('settleBudgetSeconds') == self.settle_seconds
            and finite(remaining) and 0 < remaining <= self.settle_seconds
            and not client.get('rootTask') and not client.get('rootClass') and not client.get('leafClass'))
        if planned_settle:
            if self.settle_started is None: self.settle_started = now
            if now - self.settle_started <= self.settle_seconds + 2:
                self.idle_since = None
                self.last_progress = now
                self.history.clear()
                return None
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
        leaf_class = str(client.get('leafClass') or '')
        fast_restart_supported = leaf_class == 'MoveItemToSlotFromInventoryTask' or leaf_class.startswith('CraftGeneric')
        # Chase/navigation children legitimately restart when their target moves; lifecycle churn alone is not a loop.
        if fast_restart_supported and no_progress >= 10 and target_counts and max(target_counts.values()) >= 8:
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


def focused_activity_confirmed(task, activity):
    """A completed wrapper needs its specific goal/action proof, not merely a dispatched command."""
    if not isinstance(task, str) or not task.startswith('@activity ') or not isinstance(activity, dict): return False
    if (activity.get('activity') != task.split(' ', 1)[1] or activity.get('source') != 'focused_activity_task'
            or activity.get('status') != 'completed'): return False
    start, end = activity.get('goalStartedWall'), activity.get('goalFinishedWall')
    if not finite(start) or not finite(end) or start <= 0 or end < start: return False
    evidence = activity.get('evidence')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('mechanics'), list): return False
    for event in evidence['mechanics']:
        if (not isinstance(event, dict) or not event.get('id') or event.get('actionObserved') is not True
                or event.get('goalTaskFinished') is not True or event.get('success') is not True): continue
        if event.get('source') == 'server_observer' and event.get('serverConfirmed') is True: return True
        if event.get('source') != 'client_action_and_task_outcome': continue
        packets = event.get('packetDeltas')
        if isinstance(packets, dict) and any(key.startswith('SERVERBOUND:') and isinstance(count, int)
                and not isinstance(count, bool) and count > 0 for key, count in packets.items() if isinstance(key, str)):
            return True
    return False
