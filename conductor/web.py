"""Authenticated local control API and operator dashboard for a persistent fleet."""
from __future__ import annotations
import copy
import hmac
import json
import mimetypes
import os
import re
import secrets
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
STATIC = Path(__file__).parent / 'dashboard'


def tail(path: Path, lines=100, limit=256 * 1024) -> str:
    with path.open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        value = stream.read(limit).decode('utf-8', errors='replace')
    return '\n'.join(value.splitlines()[-max(1, min(lines, 1000)):])


def read_json(path, fallback=None):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return fallback


def scoped_file(root: Path, relative: str) -> Path:
    if '\x00' in relative: raise ValueError('Invalid artifact path')
    candidate = root / relative
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
        raise FileNotFoundError('Artifact not found')
    return candidate


class Service:
    def __init__(self, fleet, *, coverage=None, artifacts=None, interval=3, retention=None):
        self.fleet = fleet
        self.coverage = coverage
        self.artifacts = artifacts
        self.interval = interval
        self.retention = retention
        self.last_retention = 0
        self.orphan_quiet = {}
        self.started = time.time()
        self.last_reconcile = None
        self.last_error = None
        self.delivery_error = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.cached = fleet.snapshot()
        self.thread = None

    def _deliver(self):
        if self.artifacts is None: return
        root = self.fleet.config.artifacts_dir
        workers = self.fleet.snapshot().get('workers', [])
        errors = []
        for worker in workers:
            run_id = worker['runId']
            run = root / run_id
            if (worker['state'] in ('failed', 'lost', 'cancelled') and run.is_dir()
                    and not run.is_symlink() and not (run / 'manifest.json').exists()):
                try:
                    confidence = 'confirmed'
                    if worker['state'] == 'lost':
                        health = read_json(run / 'health.json', {}) or {}
                        wall = health.get('wall')
                        if isinstance(wall, (int, float)) and time.time() - wall < self.fleet.config.health_timeout:
                            continue
                        signature = tuple(sorted((str(p.relative_to(run)), p.stat().st_size, p.stat().st_mtime_ns)
                            for p in run.rglob('*') if p.is_file() and not p.is_symlink()))
                        old = self.orphan_quiet.get(run_id)
                        if old is None or old[0] != signature:
                            self.orphan_quiet[run_id] = (signature, time.monotonic())
                            continue
                        if time.monotonic() - old[1] < 10: continue
                        confidence = 'inferred'
                    self.artifacts.quarantine_orphan(run_id, worker.get('reason') or 'runtime_terminated',
                        terminal=True, confidence=confidence,
                        metadata={'assignment': worker.get('task'), 'seed': worker.get('seed')})
                    self.orphan_quiet.pop(run_id, None)
                except Exception as error: errors.append(f'{run_id}: {error}')
        for entry in root.iterdir() if root.exists() else []:
            if entry.is_dir() and not entry.is_symlink() and RUN_ID.fullmatch(entry.name) and (entry / 'manifest.json').is_file():
                try: self.artifacts.enqueue(entry.name)
                except Exception as error: errors.append(f'{entry.name}: {error}')
        self.artifacts.deliver_pending(limit=4)
        if self.retention and time.monotonic() - self.last_retention > 60:
            self.last_retention = time.monotonic()
            active = [w['runId'] for w in workers if w['state'] not in ('failed', 'lost', 'cancelled', 'completed')]
            self.artifacts.prune_workspaces(self.fleet.config.workspace_dir, workers,
                successful_days=self.retention['workspace_success_days'],
                failure_days=self.retention['workspace_failure_days'], dry_run=False)
            self.artifacts.prune(active, successful_days=self.retention['spool_success_days'],
                failure_days=self.retention['spool_failure_days'], dry_run=False)
            self.artifacts.prune_central(active, diagnostic_days=self.retention['central_diagnostic_days'],
                failure_days=self.retention['central_failure_days'], dry_run=False)
        snapshot = self.artifacts.snapshot()
        self.fleet.set_inhibited('artifact_integrity_error' if errors else
            'artifact_storage_backpressure' if snapshot.get('backpressure') else None)
        if errors:
            with self.lock: self.delivery_error = '; '.join(errors[:3])

    def tick(self):
        self.delivery_error = None
        try:
            try: self._deliver()
            except Exception as delivery_error:
                self.fleet.set_inhibited('artifact_delivery_error')
                with self.lock: self.delivery_error = str(delivery_error)
            state = self.fleet.reconcile()
            with self.lock:
                self.cached = state
                self.last_reconcile = time.time()
                self.last_error = None
        except Exception as error:
            with self.lock: self.last_error = str(error)

    def start(self):
        def run():
            while not self.stop_event.is_set():
                self.tick()
                self.stop_event.wait(self.interval)
        self.thread = threading.Thread(target=run, name='fleet-reconciler', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=110)
        self.fleet.close()

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy({k: v for k, v in self.cached.items() if k != 'coverage'})
            inventory = self.cached.get('coverage')
            controller = {'startedWall': self.started, 'lastReconcileWall': self.last_reconcile,
                          'error': self.last_error, 'deliveryError': self.delivery_error, 'intervalSeconds': self.interval}
        state['controller'] = controller
        if inventory is None and self.coverage is not None: inventory = self.coverage.snapshot()
        if inventory is not None:
            state['coverage'] = {k: v for k, v in inventory.items() if k not in ('targets', 'missing', 'unsupported')}
        if self.artifacts is not None: state['storage'] = self.artifacts.snapshot()
        for worker in state.get('workers', []):
            path = self.artifact_root(worker['runId'])
            worker['liveHealth'] = read_json(path / 'health.json')
            worker['heartbeat'] = read_json(path / 'heartbeat.json')
            worker['result'] = read_json(path / 'result.json', worker.get('result'))
            worker['screenshot'] = (path / 'screenshot.png').is_file()
        return state

    def artifact_root(self, run_id):
        local = self.fleet.config.artifacts_dir / run_id
        if local.exists(): return local
        if self.artifacts is not None:
            central = self.artifacts.central_root / run_id
            if central.is_dir(): return central
        return local

    def worker(self, run_id):
        if not RUN_ID.fullmatch(run_id): raise ValueError('Invalid run id')
        state = self.snapshot()
        worker = next((w for w in state.get('workers', []) if w['runId'] == run_id), None)
        if worker is None: raise FileNotFoundError('Unknown worker')
        root = self.artifact_root(run_id)
        worker['metadata'] = read_json(root / 'metadata.json')
        worker['scriptResult'] = read_json(root / 'script-result.json')
        worker['manifest'] = read_json(root / 'manifest.json')
        worker['recentHeartbeats'] = []
        worker['recentResources'] = []
        for name, field in [('heartbeats.jsonl', 'recentHeartbeats'), ('resources.jsonl', 'recentResources')]:
            if (root / name).is_file():
                for line in tail(root / name, lines=120).splitlines():
                    try: worker[field].append(json.loads(line))
                    except ValueError: pass
        return worker


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, service, token):
        self.service, self.token = service, token
        self.sessions = {}
        self.session_lock = threading.Lock()
        self.login_attempts = {}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'VibratoConductor/1'
    def log_message(self, format, *args):
        # Never log query strings, credentials, request bodies, or Authorization headers.
        print(f'{self.address_string()} {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ""}', flush=True)

    def headers_common(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

    def json(self, value, status=200, *, cookie=None):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(status); self.headers_common()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        if cookie: self.send_header('Set-Cookie', cookie)
        self.end_headers(); self.wfile.write(data)

    def authenticated(self):
        bearer = self.headers.get('Authorization', '')
        if bearer.startswith('Bearer ') and hmac.compare_digest(bearer[7:].encode(), self.server.token.encode()): return True
        cookies = dict(pair.strip().split('=', 1) for pair in self.headers.get('Cookie', '').split(';') if '=' in pair)
        value = cookies.get('conductor_session', '')
        with self.server.session_lock:
            expiry = self.server.sessions.get(value, 0)
        return expiry > time.time()

    def body(self):
        try: length = int(self.headers.get('Content-Length', '0'))
        except ValueError: raise ValueError('Invalid content length')
        if not 0 < length <= 16 * 1024: raise ValueError('JSON body must be 1-16384 bytes')
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json': raise ValueError('Expected application/json')
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict): raise ValueError('Expected a JSON object')
        return value

    def origin_ok(self):
        origin = self.headers.get('Origin')
        if not origin: return True
        parsed = urlsplit(origin)
        return parsed.scheme in ('http', 'https') and parsed.netloc == self.headers.get('Host')

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path == '/healthz': return self.json({'ok': True})
            if path == '/readyz':
                service = self.server.service
                ready = service.last_reconcile is not None and time.time() - service.last_reconcile < 120 and service.last_error is None
                return self.json({'ok': ready}, 200 if ready else 503)
            if path.startswith('/api/'):
                if not self.authenticated(): return self.json({'error': 'Authentication required'}, 401)
                service = self.server.service
                if path == '/api/state': return self.json(service.snapshot())
                if path == '/api/coverage':
                    return self.json(service.coverage.snapshot() if service.coverage else {})
                match = re.fullmatch(r'/api/workers/([^/]+)(/logs)?', path)
                if match:
                    run_id, logs = match.groups()
                    if not logs: return self.json(service.worker(run_id))
                    service.worker(run_id)
                    query = parse_qs(parsed.query)
                    component = query.get('component', ['client'])[0]
                    if component not in ('client', 'server', 'proxy', 'display'): raise ValueError('Unknown component')
                    log = scoped_file(service.artifact_root(run_id), f'logs/{component}.log')
                    return self.json({'component': component, 'text': tail(log, int(query.get('lines', ['150'])[0]))})
                match = re.fullmatch(r'/api/artifacts/([^/]+)/(.*)', path)
                if match:
                    run_id, relative = match.groups()
                    if not RUN_ID.fullmatch(run_id): raise ValueError('Invalid run id')
                    file = scoped_file(service.artifact_root(run_id), relative)
                    return self.file(file, attachment=file.suffix not in ('.png', '.jpg', '.json'))
                return self.json({'error': 'Not found'}, 404)
            if path in ('/', '/index.html'): return self.file(STATIC / 'index.html')
            if path in ('/app.js', '/style.css'): return self.file(STATIC / path[1:])
            return self.json({'error': 'Not found'}, 404)
        except (ValueError, KeyError) as error: self.json({'error': str(error)}, 400)
        except FileNotFoundError as error: self.json({'error': str(error)}, 404)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as error: self.json({'error': str(error)}, 500)

    def file(self, path, *, attachment=False):
        # The screenshot may be atomically replaced while a browser fetches it.
        with path.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            self.send_response(200); self.headers_common()
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(size))
            if attachment: self.send_header('Content-Disposition', 'attachment; filename="' + path.name.replace('"', '') + '"')
            self.end_headers()
            while chunk := stream.read(1024 * 1024): self.wfile.write(chunk)

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if not self.origin_ok(): return self.json({'error': 'Origin rejected'}, 403)
            if path == '/api/session':
                # Rate-limit failed login attempts without retaining submitted tokens.
                now = time.time(); ip = self.client_address[0]
                with self.server.session_lock:
                    attempts = [t for t in self.server.login_attempts.get(ip, []) if now - t < 60]
                    if len(attempts) >= 15: return self.json({'error': 'Too many attempts; retry in a minute'}, 429)
                    self.server.login_attempts[ip] = [*attempts, now]
                value = self.body().get('token', '')
                if not isinstance(value, str) or not hmac.compare_digest(value.encode(), self.server.token.encode()):
                    return self.json({'error': 'Invalid access token'}, 401)
                session = secrets.token_urlsafe(32)
                with self.server.session_lock:
                    self.server.sessions = {s: expiry for s, expiry in self.server.sessions.items() if expiry > now}
                    self.server.sessions[session] = now + 8 * 3600
                    self.server.login_attempts.pop(ip, None)
                return self.json({'ok': True}, cookie=f'conductor_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800')
            if not self.authenticated(): return self.json({'error': 'Authentication required'}, 401)
            service = self.server.service
            if path == '/api/logout':
                cookies = dict(pair.strip().split('=', 1) for pair in self.headers.get('Cookie', '').split(';') if '=' in pair)
                with self.server.session_lock: self.server.sessions.pop(cookies.get('conductor_session', ''), None)
                return self.json({'ok': True}, cookie='conductor_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if path == '/api/fleet':
                target = self.body().get('desired')
                if isinstance(target, bool) or not isinstance(target, int): raise ValueError('desired must be an integer')
                service.fleet.set_target(target)
                return self.json({'ok': True, 'desired': target})
            match = re.fullmatch(r'/api/workers/([^/]+)/(stop|replace)', path)
            if match:
                run_id, action = match.groups()
                if not RUN_ID.fullmatch(run_id): raise ValueError('Invalid run id')
                service.fleet.stop_worker(run_id, replace=action == 'replace')
                return self.json({'ok': True, 'runId': run_id, 'action': action}, 202)
            return self.json({'error': 'Not found'}, 404)
        except (ValueError, KeyError, json.JSONDecodeError) as error: self.json({'error': str(error)}, 400)
        except FileNotFoundError as error: self.json({'error': str(error)}, 404)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as error: self.json({'error': str(error)}, 500)


def load_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream: stream.write(secrets.token_urlsafe(36) + '\n')
    token = path.read_text().strip()
    if len(token) < 24: raise ValueError('Dashboard token must contain at least 24 characters')
    return token


def serve(config_path, host='127.0.0.1', port=8080, token_file=None):
    from .fleet import Fleet, FleetConfig
    from .coverage import CoverageInventory
    from .artifacts import ArtifactStore
    config_data = json.loads(Path(config_path).read_text())
    service_keys = {'max_buffer_bytes', 'enabled_tasks', 'catalog_dir', 'delivery_retry_seconds',
                    'spool_success_days', 'spool_failure_days', 'central_diagnostic_days', 'central_failure_days',
                    'workspace_success_days', 'workspace_failure_days', 'max_central_bytes'}
    fleet_data = {k: v for k, v in config_data.items() if k not in service_keys}
    fleet_data.setdefault('central_dir', str(Path(fleet_data.get('state_dir', '.state/fleet')) / 'dataset'))
    config = FleetConfig.from_dict(fleet_data)
    inventory = CoverageInventory(config.state_dir / 'coverage.sqlite3', world_bank=config.world_bank,
                                  catalog_path=config_data.get('catalog_dir'),
                                  enabled_tasks=config_data.get('enabled_tasks'))
    fleet = Fleet(config, scheduler=inventory)
    central = config.central_dir
    store = ArtifactStore(config.artifacts_dir, central, state_dir=config.state_dir / 'delivery',
                          max_buffer_bytes=int(config_data.get('max_buffer_bytes', 64 * 1024**3)),
                          retry_seconds=float(config_data.get('delivery_retry_seconds', 5)),
                          max_central_bytes=int(config_data.get('max_central_bytes', 1024**4)))
    service = Service(fleet, coverage=inventory, artifacts=store,
                      interval=float(config_data.get('reconcile_seconds', 3)),
                      retention={name: float(config_data.get(name, default)) for name, default in [
                          ('spool_success_days', 7), ('spool_failure_days', 30),
                          ('central_diagnostic_days', 30), ('central_failure_days', 90),
                          ('workspace_success_days', 1), ('workspace_failure_days', 7)]})
    token_path = Path(token_file or config.state_dir / 'dashboard.token')
    token = load_token(token_path)
    server = Server((host, int(port)), service, token)
    def stop(*_): threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    service.start()
    print(f'Vibrato Conductor: http://{host}:{server.server_port}/ — access token file: {token_path}', flush=True)
    try: server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        service.close()
