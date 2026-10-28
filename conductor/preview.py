"""Ephemeral, shared live X11 preview producers; no frame files are written."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid

from .storage import IDENTIFIER

# PID is printed before exec; the marker survives in ffmpeg's argv for guarded cleanup.
_LAUNCH = '''import os,sys
p='/usr/bin/ffmpeg'
if not os.path.isfile(p): sys.exit(127)
print(os.getpid(),flush=True)
os.execv(p,['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-f','x11grab','-framerate',sys.argv[2],'-i',':99','-filter_threads','1','-vf','scale=960:540:force_original_aspect_ratio=decrease','-threads','1','-metadata','comment=preview:'+sys.argv[1],'-c:v','mjpeg','-q:v','6','-f','image2pipe','pipe:1'])'''
_CLEAN = '''import os,sys,signal
pid=int(sys.argv[1]); token=('comment=preview:'+sys.argv[2]).encode()
try:
 if pid <= 1: sys.exit(0)
 args=open('/proc/%d/cmdline'%pid,'rb').read().split(b'\\0')
 if args and os.path.basename(args[0])==b'ffmpeg' and token in args: os.kill(pid,signal.SIGTERM)
except (OSError,ValueError): pass'''


def runtime_prefix(backend, run_id):
    """Resolve only an active, fleet-owned runtime, never caller-supplied CLI text."""
    if not IDENTIFIER.fullmatch(run_id): raise ValueError('Invalid run id')
    found = [w for w in backend.list_workers() if w.get('runId') == run_id]
    if len(found) != 1 or found[0].get('state') not in ('running', 'healthy'):
        raise ValueError('Live preview unavailable')
    name = backend.name(run_id)
    if hasattr(backend, '_inspect'):
        info = backend._inspect(name)
        if (not info.get('State', {}).get('Running')
                or info.get('Config', {}).get('Labels', {}).get('dev.vibrato.conductor.run') != run_id
                or info.get('Config', {}).get('Labels', {}).get('dev.vibrato.conductor.worker') != 'true'
                or info.get('Config', {}).get('Labels', {}).get('dev.vibrato.conductor.fleet') != backend.config.fleet_id):
            raise ValueError('Worker is not running or identity changed')
        return ['docker', 'exec', name]
    info = backend._owned(run_id)
    if (info.get('metadata', {}).get('labels', {}).get('dev.vibrato.conductor.run') != run_id
            or info.get('metadata', {}).get('labels', {}).get('dev.vibrato.conductor.worker') != 'true'
            or info.get('metadata', {}).get('labels', {}).get('dev.vibrato.conductor.fleet') != backend.config.fleet_id):
        raise ValueError('Worker identity mismatch')
    cfg = backend.config
    argv = [cfg.kubectl]
    if cfg.kubeconfig: argv += ['--kubeconfig', str(cfg.kubeconfig)]
    if cfg.kube_context: argv += ['--context', cfg.kube_context]
    return argv + ['--namespace', cfg.namespace, 'exec', name, '-c', 'worker', '--']


class Producer:
    def __init__(self, prefix, rate):
        self.prefix, self.rate = prefix, rate
        self.token = uuid.uuid4().hex
        self.proc = subprocess.Popen(prefix + ['python3', '-u', '-c', _LAUNCH, self.token, str(rate)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.pid = None
        self.frame = None
        self.sequence = 0
        self.error = None
        self.condition = threading.Condition()
        self.closed = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            line = self.proc.stdout.readline(32)
            if not re.fullmatch(rb'[1-9][0-9]{0,10}\n', line): raise ValueError('Live preview unavailable')
            self.pid = int(line)
            with self.condition: self.condition.notify_all()
            if self.closed:
                self._kill_remote(); return
            data = b''
            while not self.closed:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk: break
                data += chunk
                while True:
                    start = data.find(b'\xff\xd8')
                    if start < 0: data = data[-1:]; break
                    end = data.find(b'\xff\xd9', start + 2)
                    if end < 0:
                        data = data[start:]
                        if len(data) > 4 * 1024 * 1024: raise ValueError('Preview frame too large')
                        break
                    frame, data = data[start:end + 2], data[end + 2:]
                    with self.condition:
                        self.frame = frame; self.sequence += 1; self.condition.notify_all()
        except (OSError, ValueError) as error:
            self.error = str(error)
        finally:
            with self.condition:
                self.closed = True; self.condition.notify_all()

    def _kill_remote(self):
        if self.pid:
            try:
                subprocess.run(self.prefix + ['python3', '-c', _CLEAN, str(self.pid), self.token],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.TimeoutExpired): pass

    def stop(self):
        self.closed = True
        with self.condition:
            self.condition.wait_for(lambda: self.pid is not None or self.proc.poll() is not None, timeout=2)
        self._kill_remote()
        if self.proc.poll() is None:
            try: os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                self.proc.wait(timeout=2)
        with self.condition: self.condition.notify_all()


class PreviewManager:
    """One producer per run, upgraded for detail and throttled per subscriber."""
    def __init__(self, backend, max_runs=16, max_subscribers=32):
        self.backend, self.max_runs, self.max_subscribers = backend, max_runs, max_subscribers
        self.lock = threading.RLock(); self.entries = {}

    def subscribe(self, run_id, rate):
        if rate not in (1, 24): raise ValueError('Invalid preview rate')
        with self.lock:
            if sum(len(entry['rates']) for entry in self.entries.values()) >= self.max_subscribers:
                raise ValueError('Preview capacity reached')
            entry = self.entries.get(run_id)
            if entry is None:
                if len(self.entries) >= self.max_runs: raise ValueError('Preview capacity reached')
                entry = {'prefix': runtime_prefix(self.backend, run_id), 'rates': [], 'producer': None}
                self.entries[run_id] = entry
            entry['rates'].append(rate)
            try: self._adjust(entry)
            except Exception:
                entry['rates'].remove(rate)
                if not entry['rates']: self.entries.pop(run_id, None)
                raise
        return entry

    def _adjust(self, entry):
        rate = max(entry['rates'], default=0); producer = entry['producer']
        if producer and (rate == 0 or producer.rate != rate or producer.closed):
            producer.stop(); entry['producer'] = None
        if rate and entry['producer'] is None: entry['producer'] = Producer(entry['prefix'], rate)

    def unsubscribe(self, run_id, entry, rate):
        with self.lock:
            if self.entries.get(run_id) is not entry: return
            entry['rates'].remove(rate); self._adjust(entry)
            if not entry['rates']: self.entries.pop(run_id, None)

    def close(self):
        with self.lock:
            for entry in self.entries.values():
                if entry['producer']: entry['producer'].stop()
            self.entries.clear()
