import io
import os
import tempfile
import threading
import time
import urllib.request
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from conductor.preview import PreviewManager, _CLEAN, runtime_prefix


class FakeBackend:
    def __init__(self): self.config = SimpleNamespace(fleet_id='fleet')
    def name(self, run_id): return 'vibrato-fleet-' + run_id
    def list_workers(self): return [{'runId': 'one', 'state': 'running'}]
    def _inspect(self, name):
        return {'State': {'Running': True}, 'Config': {'Labels': {'dev.vibrato.conductor.run': 'one', 'dev.vibrato.conductor.worker': 'true', 'dev.vibrato.conductor.fleet': 'fleet'}}}


class FakeProducer:
    instances = []
    def __init__(self, prefix, rate):
        self.prefix, self.rate, self.closed = prefix, rate, False
        self.instances.append(self)
    def stop(self): self.closed = True


class PreviewTests(unittest.TestCase):
    def test_fixed_runtime_identity_and_active_state(self):
        backend = FakeBackend()
        self.assertEqual(runtime_prefix(backend, 'one'), ['docker', 'exec', 'vibrato-fleet-one'])
        for invalid in ('../one', 'unknown', 'one;touch'):
            with self.assertRaises(ValueError): runtime_prefix(backend, invalid)
        with patch.object(backend, 'list_workers', return_value=[{'runId':'one','state':'completed'}]):
            with self.assertRaises(ValueError): runtime_prefix(backend, 'one')
        with patch.object(backend, '_inspect', return_value={'State': {'Running': True}, 'Config': {'Labels': {}}}):
            with self.assertRaises(ValueError): runtime_prefix(backend, 'one')

    def test_shared_producer_upgrades_and_releases_on_last_subscriber(self):
        FakeProducer.instances = []
        with patch('conductor.preview.Producer', FakeProducer):
            manager = PreviewManager(FakeBackend(), max_runs=1)
            grid = manager.subscribe('one', 1); same = manager.subscribe('one', 1)
            self.assertIs(grid, same); self.assertEqual(len(FakeProducer.instances), 1)
            detail = manager.subscribe('one', 24)
            self.assertIs(detail, grid); self.assertTrue(FakeProducer.instances[0].closed)
            self.assertEqual(detail['producer'].rate, 24)
            manager.unsubscribe('one', detail, 24)
            self.assertEqual(grid['producer'].rate, 1)
            manager.unsubscribe('one', grid, 1); self.assertFalse(grid['producer'].closed)
            last = grid['producer']; manager.unsubscribe('one', same, 1)
            self.assertTrue(last.closed); self.assertEqual(manager.entries, {})

    def test_capacity_and_failed_launch_release_slots(self):
        with patch('conductor.preview.Producer', side_effect=OSError('launch failed')):
            manager = PreviewManager(FakeBackend())
            with self.assertRaises(OSError): manager.subscribe('one', 1)
            self.assertEqual(manager.entries, {})
        with patch('conductor.preview.Producer', FakeProducer):
            manager = PreviewManager(FakeBackend(), max_subscribers=1)
            entry = manager.subscribe('one', 1)
            with self.assertRaisesRegex(ValueError, 'capacity'): manager.subscribe('one', 24)
            manager.unsubscribe('one', entry, 1)

    def test_kubernetes_exec_is_scoped_and_fixed(self):
        class PodBackend:
            config = SimpleNamespace(kubectl='kubectl', kubeconfig=None, kube_context=None, namespace='ns', fleet_id='fleet')
            def name(self, run_id): return 'pod-' + run_id
            def list_workers(self): return [{'runId':'one', 'state':'running'}]
            def _owned(self, run_id): return {'metadata': {'labels': {
                'dev.vibrato.conductor.run': run_id, 'dev.vibrato.conductor.worker': 'true', 'dev.vibrato.conductor.fleet': 'fleet'}}}
        self.assertEqual(runtime_prefix(PodBackend(), 'one'), ['kubectl', '--namespace', 'ns', 'exec', 'pod-one', '-c', 'worker', '--'])

    def test_http_stream_disconnect_releases_producer(self):
        from conductor.web import Server, Service
        class Fleet:
            backend = FakeBackend()
            def snapshot(self): return {'workers': []}
            def close(self): pass
        class Frames:
            def __init__(self, prefix, rate):
                self.rate = rate; self.closed = False; self.frame = None; self.sequence = 0
                self.condition = threading.Condition()
                threading.Thread(target=self.produce, daemon=True).start()
            def produce(self):
                while not self.closed:
                    with self.condition:
                        self.frame = b'\xff\xd8sample\xff\xd9'; self.sequence += 1; self.condition.notify_all()
                    time.sleep(.03)
            def stop(self):
                self.closed = True
                with self.condition: self.condition.notify_all()
        service = Service(Fleet()); server = Server(('127.0.0.1', 0), service, None)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch('conductor.preview.Producer', Frames):
                response = urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/workers/one/preview?fps=24', timeout=3)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.readline(), b'--frame\r\n')
                self.assertEqual(response.readline(), b'Content-Type: image/jpeg\r\n')
                count = int(response.readline().split(b':', 1)[1]); response.readline()
                self.assertEqual(response.read(count), b'\xff\xd8sample\xff\xd9'); response.close()
                for _ in range(50):
                    if not service.previews.entries: break
                    time.sleep(.05)
                self.assertEqual(service.previews.entries, {})
        finally:
            server.shutdown(); thread.join(); server.server_close(); service.close()

    def test_http_grid_survives_detail_upgrade_and_downgrade(self):
        from conductor.web import Server, Service
        class Fleet:
            backend = FakeBackend()
            def snapshot(self): return {'workers': []}
            def close(self): pass
        class Frames:
            def __init__(self, prefix, rate):
                self.rate = rate; self.closed = False; self.frame = None; self.sequence = 0
                self.condition = threading.Condition()
                threading.Thread(target=self.produce, daemon=True).start()
            def produce(self):
                while not self.closed:
                    with self.condition:
                        self.frame = b'\xff\xd8sample\xff\xd9'; self.sequence += 1; self.condition.notify_all()
                    time.sleep(.03)
            def stop(self):
                self.closed = True
                with self.condition: self.condition.notify_all()
        service = Service(Fleet()); server = Server(('127.0.0.1', 0), service, None)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def frame(stream):
            self.assertEqual(stream.readline(), b'--frame\r\n')
            self.assertEqual(stream.readline(), b'Content-Type: image/jpeg\r\n')
            count = int(stream.readline().split(b':', 1)[1]); stream.readline()
            data = stream.read(count); stream.readline(); return data
        try:
            with patch('conductor.preview.Producer', Frames):
                base = f'http://127.0.0.1:{server.server_port}/api/workers/one/preview?fps='
                grid = urllib.request.urlopen(base + '1', timeout=5)
                self.assertTrue(frame(grid))
                detail = urllib.request.urlopen(base + '24', timeout=5)
                self.assertTrue(frame(detail)); self.assertTrue(frame(grid))
                detail.close()
                for _ in range(80):
                    if service.previews.entries['one']['producer'].rate == 1: break
                    time.sleep(.05)
                self.assertEqual(service.previews.entries['one']['producer'].rate, 1)
                self.assertTrue(frame(grid)); grid.close()
        finally:
            server.shutdown(); thread.join(); server.server_close(); service.close()

    def test_remote_cleanup_only_signals_exact_ffmpeg_marker(self):
        for args, expected in [(b'ffmpeg\0-metadata\0comment=preview:abc\0', True),
                               (b'ffmpeg\0-metadata\0comment=preview:other\0', False),
                               (b'python3\0comment=preview:abc\0', False)]:
            with self.subTest(args=args), patch('builtins.open', return_value=io.BytesIO(args)), \
                 patch('os.kill') as kill, patch('sys.argv', ['helper', '123', 'abc']):
                exec(_CLEAN, {})
                self.assertEqual(kill.called, expected)
        with patch('builtins.open', return_value=io.BytesIO(b'ffmpeg\0comment=preview:abc\0')), \
             patch('os.kill') as kill, patch('sys.argv', ['helper', '1', 'abc']):
            with self.assertRaises(SystemExit): exec(_CLEAN, {})
            kill.assert_not_called()


if __name__ == '__main__': unittest.main()
