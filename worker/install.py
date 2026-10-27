"""Install pinned immutable game assets separately from frequently changed mod jars."""
import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path


def fleet_module():
    spec = importlib.util.spec_from_file_location('fleet', '/opt/vibrato/scripts/fleet/launch.py')
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    return fleet


def install_game(fleet):
    mll = fleet.require_mll()
    mll.install.install_minecraft_version(fleet.MC_VERSION, str(fleet.MC_DIR))
    mll.fabric.install_fabric(fleet.MC_VERSION, str(fleet.MC_DIR), loader_version=fleet.LOADER_VERSION)
    fleet.MODS_DIR.mkdir(parents=True, exist_ok=True)
    api = fleet.fabric_api_file()
    fleet.download(api['url'], fleet.MODS_DIR / Path(api['filename']).name, 'sha512', api['hashes']['sha512'])
    server = Path('/opt/server')
    server.mkdir()
    # Pin the installer; the version of Minecraft/loader comes only from Vibrato's properties.
    url = 'https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.0.3/fabric-installer-1.0.3.jar'
    fleet.download(url, server / 'installer.jar')
    manifest = json.loads(fleet.fetch(fleet.MANIFEST_URL))
    entry = next(v for v in manifest['versions'] if v['id'] == fleet.MC_VERSION)
    server_download = json.loads(fleet.fetch(entry['url']))['downloads']['server']
    fleet.download(server_download['url'], server / 'server.jar', 'sha1', server_download['sha1'])
    subprocess.run(['java', '-jar', 'installer.jar', 'server', '-mcversion', fleet.MC_VERSION,
                    '-loader', fleet.LOADER_VERSION], cwd=server, check=True, timeout=180)
    (server / 'mods').mkdir(exist_ok=True)
    for jar in fleet.MODS_DIR.glob('fabric-api-*.jar'):
        shutil.copy2(jar, server / 'mods' / jar.name)


def finalize_mods(fleet):
    client = list(Path('/opt/game/mods').glob('vibrato-*.jar'))
    telemetry = list(Path('/opt/server/mods').glob('vibrato-conductor-telemetry-*.jar'))
    if len(client) != 1 or len(telemetry) != 1:
        raise ValueError('Exactly one client mod and one server telemetry mod must be staged')
    def fingerprint(path):
        return {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    Path('/opt/game/versions.json').write_text(json.dumps({
        'minecraft': fleet.MC_VERSION, 'loader': fleet.LOADER_VERSION,
        'fabricApi': fleet.FABRIC_API_VERSION, 'profile': fleet.FABRIC_PROFILE,
        'serverInstaller': '1.0.3', 'optimizationMods': [],
        'clientMod': fingerprint(client[0]),
        'serverTelemetry': [fingerprint(p) for p in telemetry],
    }) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--finalize-mods', action='store_true')
    args = parser.parse_args()
    fleet = fleet_module()
    if args.finalize_mods: finalize_mods(fleet)
    else: install_game(fleet)


if __name__ == '__main__': main()
