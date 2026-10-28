"""Verify and pre-extract the vanilla server bundler's immutable inputs."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import zipfile


def preextract(bundle, server):
    server = Path(server)
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('duplicate ZIP entries')
        for group in ('libraries', 'versions'):
            seen = set()
            rows = archive.read(f'META-INF/{group}.list').decode('utf-8').splitlines()
            if not rows:
                raise ValueError('empty bundler manifest')
            for row in rows:
                parts = row.split('\t')
                if len(parts) != 3 or not re.fullmatch(r'[0-9a-f]{64}', parts[0]):
                    raise ValueError('invalid bundler manifest row')
                expected, _, name = parts
                path = PurePosixPath(name)
                if (not name or '\\' in name or path.is_absolute() or
                        any(part in ('', '.', '..') for part in name.split('/')) or name in seen):
                    raise ValueError('unsafe or duplicate bundler path')
                seen.add(name)
                data = archive.read(f'META-INF/{group}/{name}')
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError('bundler entry hash mismatch: ' + name)
                target = server / group / Path(*path.parts)
                # The build directory must not redirect any output outside the image tree.
                if any(p.is_symlink() for p in (target, *target.parents) if p != server.parent):
                    raise ValueError('symlink in bundler destination')
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != data:
                    raise ValueError('conflicting existing bundler destination')
                target.write_bytes(data)


if __name__ == '__main__':
    preextract('/opt/server/server.jar', '/opt/server')
