"""Bounded snapshots of a stopped worker world for reproducing diagnostic failures."""
from __future__ import annotations
import gzip
import io
import os
import stat
import tarfile
import time
from pathlib import Path


class SnapshotLimit(RuntimeError): pass


def snapshot_world(source: Path, destination: Path, max_bytes=256*1024**2, seconds=10) -> dict:
    started=time.monotonic()
    temporary=destination.with_name(destination.name+'.tmp')
    def check():
        if time.monotonic()-started > seconds: raise SnapshotLimit('time_budget')
    if not source.is_dir() or source.is_symlink(): return {'status':'skipped','reason':'world_missing_or_symlinked'}
    try:
        files=[]
        logical=0
        for path in source.rglob('*'):
            check()
            mode=path.lstat().st_mode
            if stat.S_ISDIR(mode): continue
            if not stat.S_ISREG(mode): raise SnapshotLimit('non_regular_file')
            logical+=path.stat().st_size
            if logical>max_bytes: raise SnapshotLimit('logical_size_limit')
            files.append(path)
            if len(files)>20000: raise SnapshotLimit('file_count_limit')
        if not files: raise SnapshotLimit('world_empty')
        class BoundedWriter(io.RawIOBase):
            def __init__(self, stream): self.stream=stream; self.written=0
            def writable(self): return True
            def write(self, data):
                check()
                if self.written+len(data)>max_bytes: raise SnapshotLimit('archive_size_limit')
                count=self.stream.write(data); self.written+=count; return count
        class TimedReader:
            def __init__(self, stream): self.stream=stream
            def read(self, size=-1): check(); return self.stream.read(size)
        with temporary.open('wb') as raw:
            writer=BoundedWriter(raw)
            with gzip.GzipFile(filename='',mode='wb',fileobj=writer,compresslevel=1,mtime=0) as compressed:
                with tarfile.open(mode='w|',fileobj=compressed) as archive:
                    for path in sorted(files):
                        check()
                        info=archive.gettarinfo(str(path),arcname='world/'+path.relative_to(source).as_posix())
                        with path.open('rb') as content: archive.addfile(info,TimedReader(content))
            raw.flush(); os.fsync(raw.fileno())
        temporary.replace(destination)
        return {'status':'captured','path':destination.name,'logicalBytes':logical,'archiveBytes':destination.stat().st_size,
                'files':len(files),'elapsedSeconds':time.monotonic()-started,'compression':'gzip_level_1'}
    except (SnapshotLimit,OSError,ValueError) as error:
        temporary.unlink(missing_ok=True)
        return {'status':'skipped','reason':str(error),'elapsedSeconds':time.monotonic()-started}
