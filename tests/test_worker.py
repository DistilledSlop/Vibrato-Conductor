import json
import subprocess
import tempfile
import sys
import time
from unittest.mock import patch
from types import SimpleNamespace
import unittest
from pathlib import Path

from conductor.storage import atomic_json, artifact_manifest
from conductor.worker import Config, Worker, capture_evidence
from conductor.diagnostics import Watchdog


class WorkerTests(unittest.TestCase):
    def test_shared_counter_pause_recovers_without_termination_but_excludes_both_streams(self):
        worker=Worker(Config.read({'RUN_ID':'shared-pause','MODE':'collection'}))
        dog=Watchdog()
        ticks=0
        def observe(now):
            return dog.observe({'seconds':now,'rootTask':'Nether','leafClass':'MineTask','tps':20,
                        'rollingTps':worker.client_rate.rolling,'rollingSamples':len(worker.client_rate.intervals)},
                       {'ticks':ticks,'tps':20,'rollingTps':worker.server_rate.rolling,
                        'rollingSamples':len(worker.server_rate.intervals)}, {}, now)
        for now in range(0,62,2):
            ticks=now*20
            worker.client_rate.update(ticks,now); worker.server_rate.update(ticks,now)
            self.assertIsNone(observe(now))
        # A shared 3-second scheduler pause widens one normally 2-second interval to 5 seconds.
        now=65; ticks+=40
        worker.client_rate.update(ticks,now); worker.server_rate.update(ticks,now)
        self.assertLess(worker.client_rate.rolling,18)
        self.assertIsNone(observe(now))
        for now in range(67,107,2):
            ticks+=40
            worker.client_rate.update(ticks,now); worker.server_rate.update(ticks,now)
            self.assertIsNone(observe(now))
        self.assertEqual(worker.client_rate.rolling,20)
        self.assertEqual(worker.server_rate.rolling,20)
        self.assertEqual(dog.overload_since,{'client':None,'server':None})
        for summary in (worker.client_rate.summary(),worker.server_rate.summary()):
            self.assertGreater(summary['meanTps'],18)
            self.assertGreater(summary['degradedWindows'],0)
            self.assertFalse(summary['valid'])

    def test_sustained_counter_degradation_terminates_and_stays_ineligible_after_recovery(self):
        worker=Worker(Config.read({'RUN_ID':'sustained-overload','MODE':'collection'}))
        dog=Watchdog()
        ticks=0; first_bad=None; assessment=None
        for now in range(0,182,2):
            if now: ticks+=40 if now<=60 else 20
            for rate in (worker.client_rate,worker.server_rate): rate.update(ticks,now)
            bad=worker.client_rate.rolling is not None and worker.client_rate.rolling<18
            if bad and first_bad is None: first_bad=now
            assessment=dog.observe({'seconds':now,'rootTask':'Nether','leafClass':'MineTask','tps':20,
                'rollingTps':worker.client_rate.rolling,'rollingSamples':len(worker.client_rate.intervals)},
                {'ticks':ticks,'tps':20,'rollingTps':worker.server_rate.rolling,
                 'rollingSamples':len(worker.server_rate.intervals)}, {}, now)
            if assessment: break
            if first_bad is not None: self.assertLess(now-first_bad,30)
        self.assertEqual(assessment['reason'],'server_tick_overload')
        self.assertEqual(now-first_bad,30)
        # Later high lifetime means cannot erase measured bad segments from final quality.
        for recover in range(now+2,now+402,2):
            ticks+=40
            for rate in (worker.client_rate,worker.server_rate): rate.update(ticks,recover)
        for rate in (worker.client_rate,worker.server_rate):
            self.assertGreater(rate.summary()['meanTps'],18)
            self.assertFalse(rate.summary()['valid'])

    def test_rejects_paths_and_unbounded_settings(self):
        for values in ({'RUN_ID': '../escape'}, {'RUN_ID': 'x', 'RUN_SECONDS': 'nan'},
                       {'RUN_ID': 'x', 'WORLD_ID': '../escape'}, {'RUN_ID': 'x', 'SEED': str(2**63)},
                       {'RUN_ID': 'x', 'TASK': '@nether\n0 exit'}, {'RUN_ID': 'x', 'CLIENT_HEAP': '-bad'}):
            with self.subTest(values=values), self.assertRaises(ValueError): Config.read(values)
        self.assertEqual(Config.read({'RUN_ID': 'x', 'TASK': '@get oak_log 1'}).task, '@get oak_log 1')
        for extra in [{'GAMEMODE':'admin'},{'OPERATOR':'yes'},{'OPERATOR':'true','OP_PERMISSION_LEVEL':'1.5'}]:
            with self.assertRaises(ValueError): Config.read({'RUN_ID':'x',**extra})
        self.assertEqual(Config.read({'RUN_ID':'x','COLLECTION_SETTLE_SECONDS':'180'}).collection_settle_seconds,180)
        self.assertEqual(Config.read({'RUN_ID':'x'}).operator_level,0)
        self.assertEqual(Config.read({'RUN_ID':'x','GAMEMODE':'creative'}).operator_level,0)
        self.assertEqual(Config.read({'RUN_ID':'x','OPERATOR':'true'}).operator_level,2)

    def test_jvm_pool_sizing_and_pid_telemetry_capture_native_spawn_failures(self):
        worker=Worker(Config.read({'RUN_ID':'thread-test'}))
        self.assertEqual(worker.jvm_options(), ['-XX:ActiveProcessorCount=8',
            '-Djava.util.concurrent.ForkJoinPool.common.parallelism=8',
            '-Djava.util.concurrent.ForkJoinPool.common.maximumSpares=64', '-XX:ParallelGCThreads=4', '-XX:ConcGCThreads=2'])
        self.assertIn('-XX:ActiveProcessorCount=4',Worker(Config.read({'RUN_ID':'x','JVM_ACTIVE_PROCESSORS':'4'})).jvm_options())
        for invalid in ['0','65','1.5','nan']:
            with self.assertRaises(ValueError): Config.read({'RUN_ID':'x','JVM_ACTIVE_PROCESSORS':invalid})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            worker.artifacts=root/'artifacts'
            logs=worker.artifacts/'logs';logs.mkdir(parents=True)
            (root/'pids.current').write_text('512')
            (root/'pids.max').write_text('512')
            (root/'pids.events').write_text('max 184\n')
            log=logs/'client.log'
            log.write_text('normal startup\n')
            self.assertIsNone(worker.sample_resources(root)['nativeThreadFailure'])
            # A partially written line spanning polls must still be detected.
            with log.open('a') as stream:stream.write('java.lang.OutOfMemoryError: unable to create native')
            self.assertIsNone(worker.sample_resources(root)['nativeThreadFailure'])
            with log.open('a') as stream:stream.write(' thread: resource limits reached\n')
            resources=worker.sample_resources(root)
            self.assertEqual((resources['pidsCurrent'],resources['pidsMax'],resources['pidsLimitEvents']),(512,512,184))
            self.assertEqual(resources['nativeThreadFailure']['process'],'client')
            self.assertEqual(worker.infrastructure_failure(),'worker_native_thread_exhausted')
            log.write_text('rotated log\n')
            self.assertEqual(worker.sample_resources(root)['nativeThreadFailure'],resources['nativeThreadFailure'])
            (root/'pids.max').write_text('max')
            self.assertIsNone(worker.sample_resources(root)['pidsMax'])

    def test_capture_proof_requires_both_directions_and_exact_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'session'
            base.with_suffix('.bin').write_bytes(b'abcd')
            rows = [{'t': 0, 'off': 0, 'len': 2, 'dir': 'C2S', 'state': 'play', 'name': 'a'},
                    {'t': 1, 'off': 2, 'len': 2, 'dir': 'S2C', 'state': 'play', 'name': 'b'}]
            base.with_suffix('.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            atomic_json(base.with_suffix('.status.json'), {'complete': True, 'bytes': 4})
            self.assertTrue(capture_evidence(root)['complete'])
            (root / 'orphan.bin').write_bytes(b'x')
            self.assertFalse(capture_evidence(root)['complete'])
            (root / 'orphan.bin').unlink()
            base.with_suffix('.bin').write_bytes(b'abc')
            self.assertFalse(capture_evidence(root)['complete'])
            base.with_suffix('.bin').write_bytes(b'abcd')
            rows[1]['off'] = 3
            base.with_suffix('.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            self.assertFalse(capture_evidence(root)['complete'])
            self.assertFalse(capture_evidence(root / 'missing')['complete'])

    def test_world_loading_and_warmup_are_not_capacity_samples(self):
        worker = Worker(Config.read({'RUN_ID':'warmup'}))
        self.assertFalse(worker.measurement_ready({'worldReady':False,'worldReadySeconds':100}))
        self.assertFalse(worker.measurement_ready({'worldReady':True,'worldReadySeconds':19.9}))
        self.assertTrue(worker.measurement_ready({'worldReady':True,'worldReadySeconds':20}))
        self.assertGreaterEqual(Config.read({'RUN_ID':'collection','MODE':'collection'}).collection_settle_seconds,
                                worker.config.warmup_seconds + 20)

    def test_final_health_is_hashed_and_artifacts_freeze_at_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            worker = Worker(Config.read({'RUN_ID':'finalize'}))
            worker.root = base / 'work'
            worker.artifacts = base / 'artifacts'
            worker.health_path = base / 'health.json'
            def prepare():
                worker.root.mkdir()
                (worker.root / 'client').mkdir()
                worker.artifacts.mkdir()
                (worker.artifacts / 'captures').mkdir()
                capture = worker.artifacts / 'captures/session'
                capture.with_suffix('.bin').write_bytes(b'ab')
                capture.with_suffix('.jsonl').write_text(json.dumps({'off':0,'len':1,'dir':'C2S','state':'play'})+'\n'+
                    json.dumps({'off':1,'len':1,'dir':'S2C','state':'play'})+'\n')
                atomic_json(capture.with_suffix('.status.json'), {'complete':True,'bytes':2})
                atomic_json(worker.root / 'client/script-result.json', {'status':'exit','commands':[{'action':'exit','status':'exit'}]})
                return worker.root/'client', worker.root/'server', None, {}
            def observe(_):
                worker.heartbeat_count=3
                worker.last_heartbeat=time.monotonic()
                worker.capture_at=time.monotonic()
                for second in range(11):
                    worker.server_rate.update(second*20,second)
                    worker.client_rate.update(second*20,second)
                return 'client_finished'
            worker.prepare=prepare
            worker.launch=lambda *_: None
            worker.observe=observe
            worker.shutdown=lambda: None
            worker.sample_resources=lambda: {}
            worker.children={name:SimpleNamespace(returncode=0) for name in ['client','proxy','server']}
            with patch.dict('os.environ', {'ACCEPT_EULA':'true','ACCEPT_OFFLINE':'true'}):
                self.assertEqual(worker.run(),0)
            manifest=json.loads((worker.artifacts/'manifest.json').read_text())
            self.assertEqual(json.loads((worker.artifacts/'health.json').read_text())['phase'],'completed')
            self.assertIn('health.json',[record['path'] for record in manifest['files']])
            self.assertEqual(artifact_manifest(worker.artifacts,manifest['result']),manifest)
            worker.health()
            self.assertEqual(artifact_manifest(worker.artifacts,manifest['result']),manifest)

    def test_server_receives_console_stop_before_signals(self):
        worker = Worker(Config.read({'RUN_ID': 'shutdown-test'}))
        proc = subprocess.Popen([sys.executable, '-c',
            "import sys; sys.exit(0 if sys.stdin.readline().strip() == 'stop' else 1)"],
            stdin=subprocess.PIPE, start_new_session=True)
        worker.children['server'] = proc
        try:
            worker.shutdown()
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdin.close()



if __name__ == '__main__': unittest.main()
