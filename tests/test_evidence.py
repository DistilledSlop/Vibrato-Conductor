import tarfile
import tempfile
import unittest
from pathlib import Path
from conductor.evidence import snapshot_world


class EvidenceTests(unittest.TestCase):
    def test_snapshot_preserves_regular_world_files_and_bounds_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); world=root/'world'; world.mkdir()
            (world/'level.dat').write_bytes(b'minecraft-state')
            archive=root/'snapshot.tar.gz'
            result=snapshot_world(world,archive,max_bytes=4096)
            self.assertEqual(result['status'],'captured')
            with tarfile.open(archive,'r:gz') as tar:
                self.assertEqual(tar.extractfile('world/level.dat').read(),b'minecraft-state')
            rejected=snapshot_world(world,root/'too-big.tar.gz',max_bytes=5)
            self.assertEqual(rejected['reason'],'logical_size_limit')
            self.assertFalse((root/'too-big.tar.gz').exists())

    def test_snapshot_rejects_symlink_and_exhausted_time_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); world=root/'world'; world.mkdir()
            (world/'level.dat').write_bytes(b'state')
            (world/'outside').symlink_to('/etc/passwd')
            self.assertEqual(snapshot_world(world,root/'a.gz')['reason'],'non_regular_file')
            (world/'outside').unlink()
            self.assertEqual(snapshot_world(world,root/'b.gz',seconds=-1)['reason'],'time_budget')
            self.assertFalse((root/'b.gz.tmp').exists())


if __name__=='__main__': unittest.main()
