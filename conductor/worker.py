"""One isolated client -> recorder -> Fabric server, with durable diagnostic artifacts."""
from __future__ import annotations
import hashlib
import uuid
import importlib.util
import json
import math
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from collections import deque
from statistics import mean
from pathlib import Path

from .storage import atomic_json, artifact_manifest, stage_world
from .diagnostics import Watchdog, task_success, script_failure_reason


def number(env: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    value = float(env.get(key, default))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{key} must be between {minimum} and {maximum}')
    return value


@dataclass(frozen=True)
class Config:
    run_id: str
    seed: int
    task: str
    seconds: float
    startup_seconds: float
    heartbeat_seconds: float
    max_bytes: int
    world_id: str | None
    allow_copy: bool
    client_heap: str
    server_heap: str
    perf: str
    mode: str = 'diagnostic'
    stall_seconds: float = 600
    loop_seconds: float = 90
    pillar_seconds: float = 45
    screenshots: bool = True
    screenshot_seconds: int = 15
    collection_settle_seconds: float = 45
    warmup_seconds: float = 20
    gamemode: str = 'survival'
    operator: bool = False
    operator_level: int = 0

    @classmethod
    def read(cls, env=None):
        env = os.environ if env is None else env
        run_id = env.get('RUN_ID', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', run_id):
            raise ValueError('RUN_ID must be 1-64 directory-safe characters')
        world_id = env.get('WORLD_ID') or None
        if world_id and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', world_id):
            raise ValueError('WORLD_ID must be a directory-safe bank identifier')
        task = env.get('TASK', 'director')
        if task != 'director' and not re.fullmatch(r'@[a-z][a-z0-9_]*(?: [A-Za-z0-9_:.-]+)*', task):
            raise ValueError('TASK must be director or one Vibrato command, e.g. @nether')
        seed = int(env.get('SEED', '424242'))
        if not -(2**63) <= seed < 2**63:
            raise ValueError('SEED must fit a signed 64-bit integer')
        heaps = [env.get('CLIENT_HEAP', '1536M'), env.get('SERVER_HEAP', '2G')]
        if any(not re.fullmatch(r'[1-9][0-9]*[MG]', h) for h in heaps):
            raise ValueError('Heaps must look like 1536M or 2G')
        perf = env.get('PERF_MODE', 'normal')
        if perf not in ('normal', 'low'):
            raise ValueError('PERF_MODE must be normal or low')
        scenario = task.removeprefix('@conductor_scenario ') if task.startswith('@conductor_scenario ') else None
        if env.get('SCENARIO') and env['SCENARIO'] != scenario: raise ValueError('SCENARIO must match the dispatched scenario task')
        mode = env.get('MODE', 'diagnostic')
        if mode not in ('diagnostic', 'collection'): raise ValueError('MODE must be diagnostic or collection')
        gamemode = env.get('GAMEMODE', 'survival')
        if gamemode not in ('survival','creative','adventure','spectator'): raise ValueError('Invalid GAMEMODE')
        if env.get('OPERATOR', 'false') not in ('true','false'): raise ValueError('OPERATOR must be true or false')
        operator = env.get('OPERATOR', 'false') == 'true'
        permission = number(env, 'OP_PERMISSION_LEVEL', 2, 1, 4) if operator else 0
        if permission != int(permission): raise ValueError('OP_PERMISSION_LEVEL must be an integer')
        operator_level = int(permission)
        return cls(run_id, seed, task, number(env, 'RUN_SECONDS', 300, 10, 86400),
                   number(env, 'STARTUP_SECONDS', 240, 10, 1800),
                   number(env, 'HEARTBEAT_SECONDS', 45, 5, 600),
                   int(number(env, 'MAX_LOCAL_BYTES', 4 * 1024**3, 1024**2, 1024**4)),
                   world_id, env.get('ALLOW_WORLD_COPY') == 'true', *heaps, perf, mode,
                   number(env, 'WATCHDOG_STALL_SECONDS', 600, 30, 86400),
                   number(env, 'WATCHDOG_LOOP_SECONDS', 90, 30, 3600),
                   number(env, 'WATCHDOG_PILLAR_SECONDS', 45, 15, 600),
                   env.get('SCREENSHOTS', 'true') == 'true',
                   int(number(env, 'SCREENSHOT_SECONDS', 15, 5, 300)),
                   number(env, 'COLLECTION_SETTLE_SECONDS', 45, 0, 120),
                   number(env, 'WARMUP_SECONDS', 20, 0, 120), gamemode, operator, operator_level)


def capture_evidence(captures: Path) -> dict:
    """Verify offsets and exact file lengths; count only packet rows actually in a capture."""
    summary = {'sessions': 0, 'bytes': 0, 'packets': 0, 'playC2S': 0, 'playS2C': 0, 'complete': True}
    indexes = list(captures.glob('*.jsonl'))
    indexes = [p for p in indexes if not p.name.endswith('.parsed.jsonl')]
    for index in indexes:
        base = index.with_suffix('')
        try:
            status = json.loads(base.with_suffix('.status.json').read_text())
            end = 0
            with index.open() as stream:
                for line in stream:
                    row = json.loads(line)
                    if 'off' not in row: continue
                    if row['off'] != end or row['len'] < 1: raise ValueError('Invalid packet offsets')
                    end += row['len']
                    summary['packets'] += 1
                    if row['state'] == 'play' and row['dir'] in ('C2S', 'S2C'):
                        summary['play' + row['dir']] += 1
            if not status['complete'] or status['bytes'] != end or base.with_suffix('.bin').stat().st_size != end:
                raise ValueError('Incomplete capture')
            summary['sessions'] += 1
            summary['bytes'] += end
        except (OSError, ValueError, KeyError, TypeError):
            summary['complete'] = False
    expected = {p.with_suffix('.bin').name for p in indexes}
    if {p.name for p in captures.glob('*.bin')} != expected:
        summary['complete'] = False
    if not indexes or not summary['playC2S'] or not summary['playS2C']:
        summary['complete'] = False
    return summary


class Worker:
    def __init__(self, config: Config):
        self.config = config
        self.root = Path('/work') / config.run_id
        self.artifacts = Path('/artifacts') / config.run_id
        self.health_path = Path('/work/health.json')
        self.username = 'V' + hashlib.sha256(config.run_id.encode()).hexdigest()[:15]
        self.player_uuid = str(uuid.UUID(bytes=hashlib.md5(('OfflinePlayer:'+self.username).encode()).digest(), version=3))
        self.children = {}
        self.stopping = False
        self.phase = 'starting'
        self.started = time.monotonic()
        self.last_heartbeat = None
        self.heartbeat_count = 0
        self.last_capture_bytes = 0
        self.capture_at = None
        self.last_client = None
        self.last_server = None
        self._last_server_ticks = None
        self._last_screenshot = None
        self.server_measurements = deque(maxlen=86400)
        self.client_measurements = deque(maxlen=86400)
        self.client_rolling = deque(maxlen=10)
        self.server_rolling = deque(maxlen=10)
        self.degraded_server_windows = 0
        self.classification = None
        self.resources = {}
        self.watchdog = Watchdog(config.stall_seconds, config.loop_seconds, config.pillar_seconds)
        self.finalized = False
        self.lifetime_deadline = self.started + config.startup_seconds * 3 + config.seconds + config.collection_settle_seconds + 90

    def stop_requested(self, *_): self.stopping = True

    def spawn(self, name, argv, cwd, env=None, stdin=None):
        with (self.artifacts / 'logs' / (name + '.log')).open('ab') as log:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.children[name] = proc
        return proc

    def check_children(self, names=None):
        for name in names or self.children:
            code = self.children[name].poll()
            if code is not None: raise RuntimeError(f'{name}_exited:{code}')
        if self.stopping: raise RuntimeError('operator_stop')

    @staticmethod
    def read_json(path):
        try: return json.loads(path.read_text())
        except (OSError, ValueError): return None

    def measurement_ready(self, data=None):
        data = self.last_client if data is None else data
        return bool(data and data.get('worldReady') and data.get('worldReadySeconds', 0) >= self.config.warmup_seconds)

    def sample_server(self):
        data = self.read_json(self.root / 'server-heartbeat.json')
        if not data or data.get('ticks') == self._last_server_ticks: return
        self.last_server = data
        self._last_server_ticks = data.get('ticks')
        if self.artifacts.exists():
            with (self.artifacts / 'server-heartbeats.jsonl').open('a') as stream:
                stream.write(json.dumps(data) + '\n')
            atomic_json(self.artifacts / 'server-heartbeat.json', data)
        if self.measurement_ready() and isinstance(data.get('tps'), (int, float)):
            self.server_measurements.append(data['tps'])
            self.server_rolling.append(data['tps'])
            if len(self.server_rolling) == 10 and mean(self.server_rolling) < 18:
                self.degraded_server_windows += 1

    def sample_resources(self):
        root = Path('/sys/fs/cgroup')
        def read(name):
            try: return (root / name).read_text().strip()
            except OSError: return None
        def stat(name):
            value = read(name)
            try: return {line.split()[0]: int(line.split()[1]) for line in value.splitlines()}
            except (TypeError, ValueError, AttributeError, IndexError): return {}
        def pressure(name):
            raw = read(name)
            if not raw: return None
            try: return float(raw.splitlines()[0].split('avg10=')[1].split()[0])
            except (IndexError, ValueError): return None
        usage, limit = read('memory.current'), read('memory.max')
        memory = int(usage) if usage and usage.isdigit() else None
        maximum = int(limit) if limit and limit.isdigit() else None
        cpu, events = stat('cpu.stat'), stat('memory.events')
        memory_psi, cpu_psi = pressure('memory.pressure'), pressure('cpu.pressure')
        self.resources = {
            'memoryBytes': memory, 'memoryLimitBytes': maximum,
            'cpuUsageSeconds': cpu.get('usage_usec', 0) / 1e6,
            'cpuThrottledSeconds': cpu.get('throttled_usec', 0) / 1e6,
            'cpuThrottledPeriods': cpu.get('nr_throttled', 0),
            'oomKills': events.get('oom_kill', 0),
            'memoryPressure': bool((memory is not None and maximum and memory / maximum >= .92)
                                   or (memory_psi is not None and memory_psi > 5)),
            'cpuPressure': bool(cpu_psi is not None and cpu_psi > 10),
            'memoryPressureAvg10': memory_psi, 'cpuPressureAvg10': cpu_psi,
        }
        return self.resources

    def health(self):
        if self.finalized: return
        self.sample_server()
        self.sample_resources()
        server_age = None if not self.last_server else max(0, time.time() - self.last_server.get('wall', 0))
        health = {
            'runId': self.config.run_id, 'phase': self.phase, 'wall': time.time(), 'mode': self.config.mode,
            'heartbeatAge': None if self.last_heartbeat is None else time.monotonic() - self.last_heartbeat,
            'captureAge': None if self.capture_at is None else time.monotonic() - self.capture_at,
            'serverHeartbeatAge': server_age,
            'clientTps': None if not self.last_client else self.last_client.get('tps'),
            'serverTps': None if not self.last_server else self.last_server.get('tps'),
            'clientMeanTps': mean(self.client_rolling) if len(self.client_rolling) >= 10 else None,
            'serverMeanTps': mean(self.server_rolling) if len(self.server_rolling) >= 10 else None,
            'clientMeanTpsSamples': len(self.client_rolling), 'serverMeanTpsSamples': len(self.server_rolling),
            'meanTickMs': None if not self.last_server else self.last_server.get('meanTickMs'),
            'serverMspt': None if not self.last_server else self.last_server.get('meanTickMs'),
            'maxTickMs': None if not self.last_server else self.last_server.get('maxTickMs'),
            'captureBytes': self.last_capture_bytes, 'resources': self.resources,
            'classification': self.classification,
            'worldReady': bool(self.last_client and self.last_client.get('worldReady')),
            'actualMode': None if not self.last_client else self.last_client.get('actualMode'),
            'permissionLevel': None if not self.last_client else self.last_client.get('permissionLevel'),
            'permissionSource': None if not self.last_client else self.last_client.get('permissionSource'),
            'assignedGamemode': self.config.gamemode, 'operator': self.config.operator,
            'warmingUp': not self.measurement_ready(),
            'capacityReady': self.measurement_ready() and len(self.server_measurements) >= 10 and len(self.client_measurements) >= 10,
            'screenshotWall': None if self._last_screenshot is None else self._last_screenshot.get('wall'),
            'screenshotAge': None if self._last_screenshot is None else max(0, time.time()-self._last_screenshot.get('wall', 0)),
        }
        atomic_json(self.health_path, health)
        if self.artifacts.exists(): atomic_json(self.artifacts / 'health.json', health)

    def copy_screenshot(self, client):
        ready = self.read_json(client / 'screenshot-ready.json')
        filename = None if not ready else ready.get('file')
        if filename not in ('live-0.png', 'live-1.png'): return
        image = client / 'screenshots' / filename
        if not ready or ready == self._last_screenshot or not image.is_file(): return
        self._last_screenshot = ready
        temp = self.artifacts / 'screenshot.png.tmp'
        shutil.copyfile(image, temp)
        temp.replace(self.artifacts / 'screenshot.png')

    def storage_bytes(self):
        # Allocated bytes account for sparse files; no traversal through immutable library/mod links.
        return sum(p.stat().st_blocks * 512 for root in (self.root, self.artifacts)
                   for p in root.rglob('*') if p.is_file() and not p.is_symlink())

    def wait_for(self, test, name):
        deadline = time.monotonic() + self.config.startup_seconds
        while time.monotonic() < deadline:
            self.check_children()
            if time.monotonic() > self.lifetime_deadline: raise RuntimeError('lifetime_limit')
            if self.storage_bytes() > self.config.max_bytes: raise RuntimeError('local_storage_limit')
            self.health()
            if test(): return
            time.sleep(1)
        raise RuntimeError(name + '_startup_timeout')

    def prepare(self):
        # Exclusive run directories prevent accidental overwrites and reuse of dirty worlds.
        self.artifacts.mkdir(parents=True, exist_ok=False)
        self.root.mkdir(parents=True, exist_ok=False)
        for name in ['logs', 'captures']:
            (self.artifacts / name).mkdir()
        server, client = self.root / 'server', self.root / 'client'
        server.mkdir(); client.mkdir()
        self.health()
        for name in ['libraries', 'versions', 'mods']:
            source = Path('/opt/server') / name
            if source.exists():
                if name == 'libraries':
                    # The vanilla bundler adds libraries on its first launch.
                    shutil.copytree(source, server / name)
                else: (server / name).symlink_to(source, target_is_directory=True)
        for jar in Path('/opt/server').glob('*.jar'):
            (server / jar.name).symlink_to(jar)
        (server / 'eula.txt').write_text('eula=true\n')
        properties = {
            'server-ip': '127.0.0.1', 'server-port': '25565', 'online-mode': 'false',
            'enforce-secure-profile': 'false', 'level-name': 'world', 'level-seed': str(self.config.seed),
            'difficulty': 'normal', 'gamemode': self.config.gamemode, 'force-gamemode': 'true', 'motd': 'Vibrato diagnostic worker',
            'max-players': '1', 'spawn-protection': '0', 'op-permission-level': str(self.config.operator_level or 2),
        }
        if self.config.operator:
            atomic_json(server / 'ops.json', [{'uuid': self.player_uuid, 'name': self.username,
                                               'level': self.config.operator_level, 'bypassesPlayerLimit': False}])
        (server / 'server.properties').write_text(''.join(f'{key}={value}\n' for key, value in properties.items()))
        world = {'id': None, 'mode': 'generated', 'seed': self.config.seed}
        if self.config.world_id:
            world = stage_world(Path('/world-bank'), self.config.world_id, server / 'world', self.config.allow_copy)
        (client / 'mods').symlink_to('/opt/game/mods', target_is_directory=True)
        # Baseline visuals by default; low rendering is explicit and recorded.
        (client / 'options.txt').write_text('onboardAccessibility:false\njoinedFirstServer:true\n'
                                          'skipMultiplayerWarning:true\ntutorialStep:none\npauseOnLostFocus:false\n')
        script = client / 'assignment.script'
        if self.config.task == 'director':
            script.write_text(f'{self.config.seconds} exit\n')
        else:
            settle = self.config.collection_settle_seconds if self.config.mode == 'collection' else 0
            script.write_text(f'0 @stop\n1 {self.config.task}\n0 wait-idle {self.config.seconds}\n{settle} exit\n')
        metadata = {
            'schemaVersion': 1, 'runId': self.config.run_id, 'seed': self.config.seed,
            'assignedGamemode': self.config.gamemode, 'operator': self.config.operator,
            'operatorPermissionLevel': self.config.operator_level, 'operatorLevel': self.config.operator_level,
            'scenario': self.config.task.removeprefix('@conductor_scenario ') if self.config.task.startswith('@conductor_scenario ') else None,
            'renderEnvironment': {key: os.environ.get(key) for key in ['LIBGL_ALWAYS_SOFTWARE','LP_NUM_THREADS','MESA_LOADER_DRIVER_OVERRIDE','DISPLAY']},
            'requestedResources': {'cpus': os.environ.get('REQUESTED_CPUS') or os.environ.get('WORKER_CPUS'),
                                   'cpuMax': (Path('/sys/fs/cgroup/cpu.max').read_text().strip() if Path('/sys/fs/cgroup/cpu.max').exists() else None)},
            'identity': {'username': self.username, 'uuid': self.player_uuid},
            'assignment': self.config.task, 'runSeconds': self.config.seconds, 'mode': self.config.mode,
            'world': world, 'perf': self.config.perf, 'connection': 'client -> 127.0.0.1:25566 -> 127.0.0.1:25565',
            'versions': json.loads(Path('/opt/game/versions.json').read_text()),
            'build': json.loads(Path('/opt/build-info.json').read_text()),
            'startedWall': time.time(), 'serverProperties': properties,
        }
        atomic_json(self.artifacts / 'metadata.json', metadata)
        return client, server, script, metadata

    def launch(self, client, server, script, metadata):
        self.spawn('display', ['Xvfb', ':99', '-screen', '0', '1280x720x24', '-nolisten', 'tcp'], self.root)
        self.wait_for(lambda: Path('/tmp/.X11-unix/X99').exists(), 'display')
        self.spawn('server', ['java', '-Xms512M', '-Xmx' + self.config.server_heap,
                             '-Dvibrato.telemetry.path=' + str(self.root / 'server-heartbeat.json'),
                             '-Dvibrato.assignedGamemode=' + self.config.gamemode,
                             '-jar', 'fabric-server-launch.jar', 'nogui'], server, stdin=subprocess.PIPE)
        server_log = self.artifacts / 'logs/server.log'
        self.wait_for(lambda: 'Done (' in server_log.read_text(errors='replace'), 'server')
        env = dict(os.environ, CAPTURE_DIR=str(self.artifacts / 'captures'), CAPTURE_RUN_ID=self.config.run_id,
                   LOG_PARSED='0', PROXY_HOST='127.0.0.1', PROXY_PORT='25566', TARGET_HOST='127.0.0.1', TARGET_PORT='25565')
        self.spawn('proxy', ['node', '/opt/proxy/index.js'], self.root, env=env)
        def proxy_ready():
            try:
                with socket.create_connection(('127.0.0.1', 25566), timeout=1): return True
            except OSError: return False
        self.wait_for(proxy_ready, 'proxy')
        import minecraft_launcher_lib as mll
        spec = importlib.util.spec_from_file_location('fleet', '/opt/vibrato/scripts/fleet/launch.py')
        fleet = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fleet)
        # Vanilla usernames have a 16-character limit; keep the full run id in metadata.
        name = self.username
        options = fleet.bot_options(name, client, '127.0.0.1', 25566, self.config.client_heap)
        natives = client / 'natives'
        natives.mkdir()
        options['nativesDirectory'] = str(natives)
        options['jvmArguments'] += [f'-Dvibrato.seed={self.config.seed}', f'-Dvibrato.perf={self.config.perf}',
                                    f'-Dvibrato.script={script}', '-Dvibrato.campaignTelemetry=true',
                                    '-Dvibrato.screenshots=' + str(self.config.screenshots).lower(),
                                    '-Dvibrato.screenshotIntervalSeconds=' + str(self.config.screenshot_seconds),
                                    '-Dvibrato.serverTelemetryFile=' + str(self.root / 'server-heartbeat.json')]
        argv = mll.command.get_minecraft_command(metadata['versions']['profile'], '/opt/game/minecraft', options)
        self.spawn('client', argv, client)

    def observe(self, client):
        previous_mtime = None
        joined_at = None
        launched = time.monotonic()
        deadline = launched + self.config.startup_seconds + self.config.seconds + self.config.collection_settle_seconds + 30
        while time.monotonic() < deadline:
            self.check_children(['display', 'server', 'proxy'])
            now = time.monotonic()
            heartbeat = client / 'campaign-heartbeat.json'
            if heartbeat.exists():
                stat = heartbeat.stat()
                if previous_mtime != stat.st_mtime_ns:
                    data = json.loads(heartbeat.read_text())
                    with (self.artifacts / 'heartbeats.jsonl').open('a') as stream:
                        stream.write(json.dumps({'wall': time.time(), **data}) + '\n')
                    atomic_json(self.artifacts / 'heartbeat.json', data)
                    previous_mtime = stat.st_mtime_ns
                    self.last_client = data
                    if self.measurement_ready(data) and isinstance(data.get('tps'), (int, float)):
                        self.client_measurements.append(data['tps'])
                        self.client_rolling.append(data['tps'])
                    self.last_heartbeat = now
                    self.heartbeat_count += 1
                    if data.get('worldReady'):
                        joined_at = joined_at or now
                        self.phase = 'running'
            size = sum(p.stat().st_size for p in (self.artifacts / 'captures').glob('*.bin'))
            if size > self.last_capture_bytes:
                self.last_capture_bytes, self.capture_at = size, now
            self.health()
            self.copy_screenshot(client)
            resources = {'wall': time.time(), 'captureBytes': size, **self.resources}
            for name in ['memory.current', 'memory.peak', 'cpu.stat', 'memory.events']:
                path = Path('/sys/fs/cgroup') / name
                resources[name] = path.read_text().strip() if path.exists() else None
            with (self.artifacts / 'resources.jsonl').open('a') as stream:
                stream.write(json.dumps(resources) + '\n')
            local_bytes = self.storage_bytes()
            if local_bytes > self.config.max_bytes: return 'local_storage_limit'
            if now > self.lifetime_deadline: return 'lifetime_limit'
            if self.children['client'].poll() is not None:
                return 'client_finished' if self.children['client'].returncode == 0 else 'client_crashed'
            if joined_at is None and now - launched > self.config.startup_seconds: return 'client_join_timeout'
            if self.last_heartbeat and now - self.last_heartbeat > self.config.heartbeat_seconds: return 'heartbeat_timeout'
            if joined_at and (not self.last_server or time.time() - self.last_server.get('wall', 0) > self.config.heartbeat_seconds):
                return 'server_heartbeat_timeout'
            if joined_at and self.last_client and self.last_client.get('permissionSource') == 'server_observer':
                if (self.last_client.get('actualMode') != self.config.gamemode
                        or self.last_client.get('serverMode') != self.config.gamemode
                        or self.last_client.get('permissionLevel') != self.config.operator_level):
                    self.classification = Watchdog.assessment('assignment_context_mismatch', 'infrastructure', 'confirmed',
                        assignedMode=self.config.gamemode, actualMode=self.last_client.get('actualMode'),
                        assignedPermission=self.config.operator_level, actualPermission=self.last_client.get('permissionLevel'))
                    self.phase = 'unhealthy'; self.health(); return 'assignment_context_mismatch'
            assessment = self.watchdog.observe(self.last_client, self.last_server, self.resources, now) if self.measurement_ready() else None
            if assessment:
                self.classification = assessment
                self.phase = 'unhealthy'
                self.health()
                return assessment['reason']
            if joined_at and (self.capture_at is None or now - self.capture_at > self.config.heartbeat_seconds): return 'capture_stalled'
            time.sleep(2)
        return 'lifetime_limit'

    def shutdown(self):
        # Client first, then proxy (drain capture), then server (save world), then display.
        for name in ['client', 'proxy', 'server', 'display']:
            proc = self.children.get(name)
            if proc is None: continue
            if name == 'server' and proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(b'stop\n')
                    proc.stdin.flush()
                except (OSError, ValueError): pass
                try: proc.wait(timeout=25)
                except subprocess.TimeoutExpired: pass
            if proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError: pass
            try: proc.wait(timeout=5 if name == 'display' else 15)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait()

    def run(self):
        if os.environ.get('ACCEPT_EULA') != 'true':
            raise ValueError('Set ACCEPT_EULA=true only after accepting the Minecraft EULA')
        if os.environ.get('ACCEPT_OFFLINE') != 'true':
            raise ValueError('Set ACCEPT_OFFLINE=true to use isolated offline-mode identities')
        # Never finalize or modify artifacts belonging to an already-existing run id.
        if self.root.exists() or self.artifacts.exists(): raise ValueError('RUN_ID already exists; use a fresh run id')
        reason = 'startup_failed'
        try:
            client, server, script, metadata = self.prepare()
            self.launch(client, server, script, metadata)
            reason = self.observe(client)
        except Exception as error:
            reason = str(error)
            self.sample_resources()
            if self.resources.get('oomKills', 0):
                self.classification = Watchdog.assessment('worker_memory_exhausted', 'overload', 'confirmed', **self.resources)
        finally:
            self.phase = 'stopping'
            self.shutdown()
        for name in ['script-result.json', 'campaign-heartbeat.json', 'scenario-result.json']:
            source = self.root / 'client' / name
            if source.exists(): shutil.copyfile(source, self.artifacts / name)
        self.copy_screenshot(self.root / 'client')
        self.sample_server()
        evidence = capture_evidence(self.artifacts / 'captures')
        script_result_path = self.artifacts / 'script-result.json'
        script_result = json.loads(script_result_path.read_text()) if script_result_path.exists() else {}
        script_ok = script_result.get('status') == 'exit' and all(
            c.get('status') not in ('timeout', 'error') for c in script_result.get('commands', []))
        if reason == 'client_finished':
            reason = script_failure_reason(self.config.task, script_result) or reason
        ok = reason == 'client_finished' and script_ok and self.heartbeat_count >= 3 and evidence['complete']
        ok = ok and all(self.children.get(name) is not None and self.children[name].returncode == 0
                        for name in ['client', 'proxy', 'server'])
        success = task_success(self.config.task, script_result)
        if self.config.task.startswith('@conductor_scenario '):
            scenario = self.read_json(self.artifacts / 'scenario-result.json') or {}
            if (scenario.get('scenario') != self.config.task.removeprefix('@conductor_scenario ')
                    or scenario.get('status') != 'completed' or scenario.get('source') != 'client_actions_plus_server_observer'
                    or scenario.get('evidence', {}).get('serverConfirmed') is not True):
                success = False; ok = False
                if reason == 'client_finished': reason = 'scenario_unconfirmed'
        if reason != 'client_finished' or not script_ok: success = False if self.config.task != 'director' else None
        context_valid = bool(self.last_client and self.last_client.get('actualMode') == self.config.gamemode
                             and self.last_client.get('serverMode') == self.config.gamemode
                             and self.last_client.get('permissionSource') == 'server_observer'
                             and self.last_client.get('permissionLevel') == self.config.operator_level)
        server_health = {
            'samples': len(self.server_measurements),
            'meanTps': mean(self.server_measurements) if self.server_measurements else None,
            'degradedWindows': self.degraded_server_windows,
        }
        server_health['valid'] = (server_health['samples'] >= 10 and server_health['meanTps'] >= 18
                                  and not self.degraded_server_windows)
        client_health = {'samples': len(self.client_measurements),
                         'meanTps': mean(self.client_measurements) if self.client_measurements else None}
        client_health['valid'] = client_health['samples'] >= 10 and client_health['meanTps'] >= 18
        invalid = []
        if not context_valid: invalid.append('assignment_context_unconfirmed')
        if not client_health['valid']: invalid.append('client_tick_health_not_established')
        if not server_health['valid']: invalid.append('server_tick_health_not_established')
        if self.classification: invalid.append(self.classification['reason'])
        if success is not True: invalid.append('task_success_not_confirmed')
        if not ok: invalid.append('worker_diagnostic_failed')
        eligible = self.config.mode == 'collection' and not invalid and evidence['complete']
        result = {
            'schemaVersion': 1, 'runId': self.config.run_id, 'reason': reason, 'diagnosticPassed': ok,
            'datasetEligible': eligible, 'taskSuccess': success, 'mode': self.config.mode,
            'assignmentContext': {'valid': context_valid, 'gamemode': self.config.gamemode, 'operatorPermissionLevel': self.config.operator_level},
            'serverHealth': server_health, 'clientHealth': client_health, 'qualityInvalid': invalid, 'watchdog': self.classification, 'heartbeatSamples': self.heartbeat_count,
            'capture': evidence, 'elapsedSeconds': time.monotonic() - self.started,
            'exitCodes': {name: proc.returncode for name, proc in self.children.items()},
            'limitations': ['Behavior loop/stall watchdog classifications are inferred unless the task reports failure',
                            'Diagnostic pass alone does not establish task success or fleet capacity'],
        }
        if not self.artifacts.exists(): raise RuntimeError(reason)
        atomic_json(self.artifacts / 'result.json', result)
        self.phase = 'completed' if ok else 'failed'
        self.health()
        atomic_json(self.artifacts / 'manifest.json', artifact_manifest(self.artifacts, result))
        self.finalized = True
        print(json.dumps(result), flush=True)
        return 0 if ok else 1


def main():
    if '--healthcheck' in sys.argv:
        try:
            health = json.loads(Path('/work/health.json').read_text())
            ok = (health['phase'] == 'running' and time.time() - health['wall'] < 30
                  and health['heartbeatAge'] is not None and health['heartbeatAge'] < 45
                  and health['captureAge'] is not None and health['captureAge'] < 45
                  and health.get('serverHeartbeatAge') is not None and health['serverHeartbeatAge'] < 45)
            return 0 if ok else 1
        except (OSError, ValueError, KeyError): return 1
    worker = Worker(Config.read())
    signal.signal(signal.SIGTERM, worker.stop_requested)
    signal.signal(signal.SIGINT, worker.stop_requested)
    return worker.run()

if __name__ == '__main__':
    try: sys.exit(main())
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
