import json
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

from conductor.storage import atomic_json
from conductor.worker import Config, Worker, capture_evidence


class WorkerTests(unittest.TestCase):
    def test_rejects_paths_and_unbounded_settings(self):
        for values in ({'RUN_ID': '../escape'}, {'RUN_ID': 'x', 'RUN_SECONDS': 'nan'},
                       {'RUN_ID': 'x', 'WORLD_ID': '../escape'}, {'RUN_ID': 'x', 'SEED': str(2**63)},
                       {'RUN_ID': 'x', 'TASK': '@nether\n0 exit'}, {'RUN_ID': 'x', 'CLIENT_HEAP': '-bad'}):
            with self.subTest(values=values), self.assertRaises(ValueError): Config.read(values)
        self.assertEqual(Config.read({'RUN_ID': 'x', 'TASK': '@get oak_log 1'}).task, '@get oak_log 1')

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
