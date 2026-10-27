"""Install the pinned client and a Fabric dedicated server, without accepting the EULA."""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location('fleet', '/opt/vibrato/scripts/fleet/launch.py')
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)
fleet.cmd_install(type('Args', (), {'dry_run': False})())
server = Path('/opt/server')
server.mkdir()
# Pin the installer as well as game/loader; all game versions come from the mod's properties.
url = 'https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.0.3/fabric-installer-1.0.3.jar'
fleet.download(url, server / 'installer.jar')
manifest = json.loads(fleet.fetch(fleet.MANIFEST_URL))
entry = next(v for v in manifest['versions'] if v['id'] == fleet.MC_VERSION)
server_download = json.loads(fleet.fetch(entry['url']))['downloads']['server']
fleet.download(server_download['url'], server / 'server.jar', 'sha1', server_download['sha1'])
subprocess.run(['java', '-jar', 'installer.jar', 'server', '-mcversion', fleet.MC_VERSION,
                '-loader', fleet.LOADER_VERSION], cwd=server, check=True, timeout=180)
(server / 'mods').mkdir(exist_ok=True)
for jar in Path('/opt/game/mods').glob('fabric-api-*.jar'):
    shutil.copy2(jar, server / 'mods' / jar.name)
Path('/opt/game/versions.json').write_text(json.dumps({
    'minecraft': fleet.MC_VERSION, 'loader': fleet.LOADER_VERSION,
    'fabricApi': fleet.FABRIC_API_VERSION, 'profile': fleet.FABRIC_PROFILE,
    'serverInstaller': '1.0.3', 'optimizationMods': [],
}) + '\n')
