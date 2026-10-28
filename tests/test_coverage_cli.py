import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from conductor.coverage import CoverageInventory
from tests.test_coverage import admit_run, capture_run


class CoverageCliTests(unittest.TestCase):
    def test_report_reads_current_receipts_and_leaves_database_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / 'coverage.sqlite'
            inventory = CoverageInventory(db)
            run = capture_run(root / 'runs', success=True, include_script=True)
            admit_run(inventory, run)
            inventory.ingest(run)
            before = db.read_bytes()
            base = [sys.executable, '-m', 'conductor', 'coverage-report', '--db', str(db)]
            admitted = json.loads(subprocess.check_output(base + ['--central', str(run.parent)], text=True))
            self.assertEqual(admitted['eligibleRuns'], 1)
            self.assertEqual(admitted['categories']['packet']['observed'], 2)
            raw_only = json.loads(subprocess.check_output(base, text=True))
            self.assertEqual(raw_only['eligibleRuns'], 0)
            packet = next(t for t in raw_only['targets'] if t['id'] == 'packet:play:clientbound:0')
            self.assertEqual((packet['observedRuns'], packet['rawObservedRuns']), (0, 1))
            self.assertEqual(db.read_bytes(), before)


if __name__ == '__main__': unittest.main()
