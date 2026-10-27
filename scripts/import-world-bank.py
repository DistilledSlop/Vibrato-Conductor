#!/usr/bin/env python3
"""Inspect and register stopped Minecraft saves as immutable world-bank templates."""
import argparse
from collections import Counter
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from conductor.storage import atomic_json, world_revision


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('bank',type=Path)
    operation=parser.add_mutually_exclusive_group()
    operation.add_argument('--move',action='store_true',help='Move within the same filesystem without duplicating world bytes')
    operation.add_argument('--copy',action='store_true',help='Explicitly allow full copies into the bank')
    parser.add_argument('--image',default='vibrato-worker:dev')
    parser.add_argument('--report',type=Path,default=ROOT/'.state/world-import/report.json')
    args=parser.parse_args()
    source,bank=args.source.resolve(strict=True),args.bank.resolve()
    if not source.is_dir() or source==bank or source.is_relative_to(bank) or bank.is_relative_to(source):
        parser.error('Source and bank must be separate directories')
    raw=subprocess.check_output(['docker','run','--rm','--network','none','--entrypoint','node',
        '--mount',f'type=bind,src={source},dst=/saves,readonly',
        '--mount',f'type=bind,src={ROOT / "scripts/world-metadata.js"},dst=/world-metadata.js,readonly',
        args.image,'/world-metadata.js'],text=True)
    entries=json.loads(raw)['worlds']
    if not entries:parser.error('No Minecraft saves with level.dat found')
    for item in entries:
        identifier=item['name']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',identifier):parser.error('Unsafe bank identifier: '+identifier)
        if item.get('error') or item.get('minecraftVersion')!='1.21.1':
            parser.error(f'{identifier}: expected Minecraft 1.21.1, found {item.get("minecraftVersion") or item.get("error")}')
        if (bank/identifier).exists():parser.error('Bank entry already exists: '+identifier)
        if (source/identifier).is_symlink():parser.error('Source save must not be a symlink')
    report={'schemaVersion':1,'source':str(source),'bank':str(bank),'operation':'move' if args.move else 'copy' if args.copy else 'inspect',
            'worlds':entries,'templatesImmutable':False,'regionNote':'Stored chunk bounds come from region headers; they do not certify every chunk is fully generated.'}
    if args.move or args.copy:
        bank.mkdir(parents=True,exist_ok=True)
        if args.move and source.stat().st_dev!=bank.stat().st_dev:parser.error('Move requires the same filesystem; explicit --copy permits a full copy')
        for item in entries:
            identifier=item['name'];src=source/identifier;entry=bank/identifier;entry.mkdir()
            # Read and verify all source files before making this entry available.
            revision=world_revision(src)
            if args.move:src.rename(entry/'world')
            else:shutil.copytree(src,entry/'world',copy_function=shutil.copyfile)
            copied=world_revision(entry/'world')
            if copied['revision']!=revision['revision']:raise RuntimeError('World changed while importing: '+identifier)
            metadata={**item,'minecraftVersion':'1.21.1','revision':revision['revision'],
                'tags':[],'gamemodes':['survival','creative','adventure','spectator'],'allowOperator':True,
                'logicalBytes':revision['logicalBytes'],'fileCount':revision['fileCount'],
                'originPath':str(src),'notes':'Imported user template; actual mode and permissions are assigned per isolated worker, independently of sourceGameType.'}
            atomic_json(entry/'world.json',metadata)
            for file in (entry/'world').rglob('*'):
                if file.is_file():file.chmod(0o444)
            for directory in sorted((p for p in (entry/'world').rglob('*') if p.is_dir()),reverse=True):directory.chmod(0o555)
            (entry/'world').chmod(0o555)
            item.update({'revision':revision['revision'],'logicalBytes':revision['logicalBytes'],'fileCount':revision['fileCount'],'location':str(entry/'world')})
        report['templatesImmutable']=True
    args.report.parent.mkdir(parents=True,exist_ok=True);atomic_json(args.report,report)
    print(json.dumps({'worlds':len(entries),'operation':report['operation'],
        'versions':dict(Counter(item['minecraftVersion'] for item in entries)),
        'sourceModes':dict(Counter(item['sourceGameType'] for item in entries)),
        'logicalBytes':sum(item.get('logicalBytes',0) for item in entries),'bank':str(bank),'report':str(args.report)},indent=2))

if __name__=='__main__':main()
