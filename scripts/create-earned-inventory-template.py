#!/usr/bin/env python3
"""Create a verified inventory-sort world template from one stopped, earned run."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conductor.artifacts import QUALITY_POLICY_VERSION, quality_report
from conductor.storage import IDENTIFIER, atomic_json, digest, verify_manifest, world_revision, inspect_world_bank


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--source-world', type=Path, required=True)
    parser.add_argument('--admission-receipt', type=Path, required=True)
    parser.add_argument('--bank', type=Path, required=True)
    parser.add_argument('--world-id', required=True)
    parser.add_argument('--runtime-image', required=True, help='Local worker image containing Node and prismarine-nbt')
    args = parser.parse_args()
    if not IDENTIFIER.fullmatch(args.world_id): parser.error('invalid world id')
    run = args.source_run.resolve(strict=True)
    world = args.source_world.resolve(strict=True)
    result = verify_manifest(run)
    quality = quality_report(run)
    meta = json.loads((run/'metadata.json').read_text())
    if (not quality['eligible'] or meta.get('assignedGamemode') != 'survival' or meta.get('operator') is not False
            or result.get('exitCodes', {}).get('server') != 0):
        raise ValueError('Source run is not independently admitted and cleanly stopped non-operator survival')
    source_uuid = (meta.get('identity') or {}).get('uuid')
    if not isinstance(source_uuid, str): raise ValueError('Source player identity missing')
    source_hash = digest(run/'manifest.json')
    receipt_path = args.admission_receipt.resolve(strict=True)
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if (receipt.get('runId') != run.name or receipt.get('manifestSha256') != source_hash
            or receipt.get('quality', {}).get('policyVersion') != QUALITY_POLICY_VERSION
            or receipt.get('quality', {}).get('eligible') is not True):
        raise ValueError('Source admission receipt is stale, excluded, or inconsistent')
    source_revision = world_revision(world)
    world_metadata_path = world.parent/'world.json'
    if world_metadata_path.is_symlink() or not world_metadata_path.is_file():
        raise ValueError('Source bank world metadata missing')
    world_metadata_hash = digest(world_metadata_path)
    source_world_info = meta.get('world') or {}
    if (run.name != meta.get('runId') or meta.get('assignment') != '@activity organize_inventory'
            or source_revision['revision'] != source_world_info.get('revision')
            or world_metadata_hash != source_world_info.get('sourceMetadataSha256')):
        raise ValueError('Source bank world is not the admitted run starting state')
    activity = json.loads((run/'activity-result.json').read_text())
    start = activity.get('goalStartedWall')
    if not isinstance(start, (int,float)) or isinstance(start, bool): raise ValueError('Source goal start missing')
    # The first template is intentionally narrow; the real retained world must satisfy this exact sort plan.
    proof = {'sourceSlot': 8, 'hotbarSlot': 1, 'item': 'minecraft:stone_pickaxe', 'count': 1, 'hotbarEmpty': True}
    helper = Path(__file__).resolve().parents[1]/'conductor/migrate_player.js'
    if ',' in str(world) or ',' in str(helper): raise ValueError('Docker mount path cannot contain comma')
    command = ('mkdir -p /tmp/context/playerdata && cp -a /source/playerdata/. /tmp/context/playerdata/ '
               '&& exec node /migrate.js /tmp/context "$@"')
    synthetic = '00000000-0000-3000-8000-000000000001'
    checked = subprocess.run(['docker','run','--rm','--network','none','--entrypoint','/bin/sh',
        '--mount',f'type=bind,src={world},dst=/source,readonly',
        '--mount',f'type=bind,src={helper},dst=/migrate.js,readonly',args.runtime_image,'-c',command,
        'check',source_uuid,synthetic,json.dumps(proof,separators=(',',':'))], check=True, capture_output=True, text=True)
    observed = json.loads(checked.stdout).get('sourcePlayer') or {}
    latest = None
    with (run/'server-heartbeats.jsonl').open() as stream:
        for line in stream:
            sample = json.loads(line)
            if (sample.get('source') != 'fabric_end_server_tick' or not isinstance(sample.get('wall'), (int,float))
                    or not start - 5 <= sample['wall'] <= start): continue
            for player in sample.get('playerSnapshots', []):
                if player.get('uuid') == source_uuid: latest = player
    if (latest is None or latest.get('mode') != 'survival' or latest.get('permissionLevel') != 0
            or latest.get('inventory') != observed.get('inventory') or latest.get('position') != observed.get('position')):
        raise ValueError('Saved player inventory or position does not match source server observer')
    bank = args.bank.resolve(); bank.mkdir(parents=True, exist_ok=True)
    dest = bank/args.world_id
    if dest.exists(): raise ValueError('Template world id already exists')
    with tempfile.TemporaryDirectory(prefix='.template-', dir=bank) as tmp:
        entry = Path(tmp)/args.world_id; entry.mkdir(); shutil.copytree(world,entry/'world')
        if (world_revision(world) != source_revision or world_revision(entry/'world') != source_revision
                or digest(world_metadata_path) != world_metadata_hash):
            raise ValueError('Source changed or template copy differs')
        if digest(run/'manifest.json') != source_hash or receipt_path.read_bytes() != receipt_bytes or not quality_report(run)['eligible']:
            raise ValueError('Source run changed or admission failed during creation')
        config = {'name': args.world_id, 'minecraftVersion': '1.21.1',
            'seed': (meta.get('world') or {}).get('sourceSeed'), 'revision': source_revision['revision'],
            'sourceGameType': 0, 'tags': ['inventory-sort-kit'], 'gamemodes': ['survival'],
            'allowOperator': False, 'allowedTasks': ['mechanic-organize-inventory'],
            'earnedContextTemplate': {'kind': 'earned_inventory_sort_v1', 'sourceUuid': source_uuid,
                'sourceRun': run.name, 'sourceManifestSha256': source_hash, 'sourceWorldPath': str(world),
                'sourceWorldRevision': source_revision['revision'], 'sourceWorldMetadataSha256': world_metadata_hash,
                'sourceAdmissionSha256': hashlib.sha256(receipt_bytes).hexdigest(), 'sortProof': proof}}
        if config['seed'] is None: raise ValueError('Source world seed missing')
        atomic_json(entry/'world.json',config)
        shutil.copyfile(run/'manifest.json',entry/'source-run-manifest.json')
        (entry/'source-admission-receipt.json').write_bytes(receipt_bytes)
        shutil.copyfile(world_metadata_path,entry/'source-original-world.json')
        # Validate using the public bank inspector before making the entry visible.
        inspection = inspect_world_bank(Path(tmp))
        if len(inspection) != 1 or not inspection[0]['supported']: raise ValueError('Template bank validation failed')
        if dest.exists(): raise ValueError('Template world id appeared during creation')
        os.rename(entry,dest)
    print(json.dumps({'worldId': args.world_id, 'sourceRun': run.name, 'revision': source_revision['revision'],
                      'sourceManifestSha256': source_hash, 'bank': str(bank)}, indent=2))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error: raise SystemExit(str(error))
