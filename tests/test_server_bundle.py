import hashlib
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('server_bundle', Path(__file__).resolve().parents[1] / 'worker/preextract_server_bundle.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ServerBundleTests(unittest.TestCase):
    def fixture(self, root, *, name='a/lib.jar', wrong=False, duplicate=False):
        archive = root / 'bundle.jar'
        entries = [('libraries', name, b'library'), ('versions', '1.21.1/server.jar', b'server')]
        with zipfile.ZipFile(archive, 'w') as z:
            for group in ('libraries', 'versions'):
                lines = []
                for g, path, data in entries:
                    if g != group: continue
                    digest = '0' * 64 if wrong and g == 'libraries' else hashlib.sha256(data).hexdigest()
                    lines.append(f'{digest}\tid\t{path}')
                    z.writestr(f'META-INF/{group}/{path}', data)
                if duplicate and group == 'libraries': lines.append(lines[0])
                z.writestr(f'META-INF/{group}.list', '\n'.join(lines))
        return archive

    def test_extract_verified_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bundle = self.fixture(root); dest = root / 'server'
            module.preextract(bundle, dest)
            module.preextract(bundle, dest)
            self.assertEqual((dest / 'libraries/a/lib.jar').read_bytes(), b'library')
            self.assertEqual((dest / 'versions/1.21.1/server.jar').read_bytes(), b'server')

    def test_reject_existing_conflict_and_symlink(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); bundle = self.fixture(root); dest = root / 'server'
                (dest / 'libraries').mkdir(parents=True)
                if symlink:
                    (dest / 'libraries/a').symlink_to(root, target_is_directory=True)
                else:
                    (dest / 'libraries/a').mkdir()
                    (dest / 'libraries/a/lib.jar').write_bytes(b'other')
                with self.assertRaises(ValueError): module.preextract(bundle, dest)

    def test_all_manifest_rows_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bundle = root / 'bundle.jar'
            with zipfile.ZipFile(bundle, 'w') as z:
                for group, count in (('libraries', 30), ('versions', 1)):
                    rows = []
                    for i in range(count):
                        data = f'{group}-{i}'.encode(); name = f'path/{i}.jar'
                        rows.append(f'{hashlib.sha256(data).hexdigest()}\tid\t{name}')
                        z.writestr(f'META-INF/{group}/{name}', data)
                    z.writestr(f'META-INF/{group}.list', '\n'.join(rows))
            module.preextract(bundle, root / 'server')
            self.assertEqual(len(list((root / 'server').rglob('*.jar'))), 31)

    def test_reject_invalid_inputs(self):
        for kwargs in ({'wrong': True}, {'name': '../escape'}, {'duplicate': True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(ValueError): module.preextract(self.fixture(root, **kwargs), root / 'server')
