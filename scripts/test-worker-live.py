#!/usr/bin/env python3
"""Run one real worker probe; no simulated clients or artifact promotion shortcuts."""
from __future__ import annotations
import argparse
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conductor.artifacts import quality_report
from conductor.backends import DockerBackend
from conductor.fleet import FleetConfig
from conductor.storage import verify_manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id',required=True)
    parser.add_argument('--task',default='director')
    parser.add_argument('--seconds',type=float,default=60)
    parser.add_argument('--mode',choices=['diagnostic','collection'],default='diagnostic')
    parser.add_argument('--expect',choices=['healthy','eligible','task-timeout'],default='healthy')
    parser.add_argument('--state',type=Path,default=Path('.state/telemetry-live'))
    parser.add_argument('--world-bank',type=Path,default=Path('.state/world-bank'))
    parser.add_argument('--world-id',default='seed-424242')
    parser.add_argument('--cpus',type=float,default=4)
    parser.add_argument('--memory-mb',type=int,default=6144,help='Container memory limit;0 omits the quota')
    parser.add_argument('--image',default='vibrato-worker:dev')
    parser.add_argument('--gamemode',choices=['survival','creative','adventure','spectator'],default='survival')
    parser.add_argument('--operator',action='store_true')
    parser.add_argument('--operator-level',type=int,default=2)
    parser.add_argument('--render-threads',type=int,help='Explicit Mesa llvmpipe thread count; visual settings remain unchanged')
    parser.add_argument('--accept-eula',action='store_true')
    parser.add_argument('--accept-offline',action='store_true')
    args=parser.parse_args()
    if not args.accept_eula or not args.accept_offline: parser.error('Explicit --accept-eula and --accept-offline are required')
    if args.render_threads is not None and not 1 <= args.render_threads <= 64: parser.error('--render-threads must be1..64')
    state=args.state.resolve()
    config=FleetConfig.from_dict({
        'fleet_id':'telemetry-tests','state_dir':str(state/'control'),
        'artifacts_dir':str(state/'artifacts'),'workspace_dir':str(state/'workspaces'),
        'world_bank':str(args.world_bank.resolve()),'allow_world_copy':True,
        'image':args.image,'cpus':args.cpus,'memory_mb':args.memory_mb,'storage_mb':8192,
        'mode':args.mode,'seconds':args.seconds,'accept_eula':True,'accept_offline':True,
        'worker_env':{'GAMEMODE':args.gamemode,'OPERATOR':str(args.operator).lower(),'OP_PERMISSION_LEVEL':str(args.operator_level),'REQUESTED_CPUS':str(args.cpus),
                      **({'LP_NUM_THREADS':str(args.render_threads)} if args.render_threads is not None else {})},
    })
    artifacts=config.artifacts_dir/args.id
    workspace=config.workspace_dir/args.id
    if artifacts.exists() or workspace.exists(): parser.error('Use a fresh --id; existing evidence is never overwritten')
    backend=DockerBackend(config)
    spec={'runId':args.id,'task':args.task,'seconds':args.seconds,'seed':424242,'worldId':args.world_id,'gamemode':args.gamemode,'operator':args.operator,'operatorLevel':args.operator_level if args.operator else 0}
    backend.create_worker(spec)
    print('Started '+backend.name(args.id),flush=True)
    print('Artifacts: '+str(artifacts),flush=True)
    screenshot_times=set()
    deadline=time.monotonic()+args.seconds+4*config.startup_timeout+180
    previous=None
    stopped=False
    try:
        while time.monotonic()<deadline:
            health=None
            try: health=json.loads((artifacts/'health.json').read_text())
            except (OSError,ValueError): pass
            if health:
                if health.get('screenshotWall'): screenshot_times.add(health['screenshotWall'])
                status=(health['phase'],health.get('worldReady'),health.get('warmingUp'),health.get('capacityReady'))
                if status!=previous:
                    print(json.dumps({'status':status,'clientTps':health.get('clientTps'),'serverTps':health.get('serverTps')}),flush=True)
                    previous=status
            found=next((w for w in backend.list_workers() if w['runId']==args.id),None)
            if found and found['state'] in ('completed','failed'):
                stopped=True
                break
            time.sleep(2)
        if not stopped: raise TimeoutError('Worker did not exit within the bounded live-test deadline')
    finally:
        if not stopped: backend.stop_worker(args.id)
    result=verify_manifest(artifacts)
    screenshot=(artifacts/'screenshot.png').read_bytes()
    assert screenshot[:8]==b'\x89PNG\r\n\x1a\n','Missing real PNG screenshot'
    width,height=struct.unpack('>II',screenshot[16:24])
    assert width>=100 and height>=100,'Invalid screenshot dimensions'
    assert len(screenshot_times)>=2,'Periodic screenshot updates were not observed'
    assert result['capture']['complete'],'Capture did not flush completely'
    if args.expect in ('healthy','eligible'):
        assert result['diagnosticPassed'],json.dumps(result)
        assert result['serverHealth']['valid'] and result['serverHealth']['samples']>=10,json.dumps(result)
        assert result['clientHealth']['valid'] and result['clientHealth']['samples']>=10,json.dumps(result)
    if result.get('assignmentContext'):
        assert result['assignmentContext']['valid'],json.dumps(result)
        assert result['assignmentContext']['gamemode']==args.gamemode,json.dumps(result)
    if args.task.startswith('@conductor_scenario '):
        scenario=json.loads((artifacts/'scenario-result.json').read_text())
        assert scenario['status']=='completed' and scenario['evidence']['serverConfirmed'] is True,json.dumps(scenario)
        assert scenario['evidence']['permissionLevel']==(args.operator_level if args.operator else 0),json.dumps(scenario)
    if args.expect=='eligible':
        assert result['taskSuccess'] is True,json.dumps(result)
        assert result['datasetEligible'] is True,json.dumps(result)
        assert quality_report(artifacts)['eligible'],json.dumps(quality_report(artifacts))
        if args.task=='@get dirt 1':
            rows=[json.loads(line) for line in (artifacts/'heartbeats.jsonl').read_text().splitlines()]
            assert any(r.get('inventory',{}).get('minecraft:dirt',0)>=1 for r in rows),'No actual dirt inventory evidence'
    if args.expect=='task-timeout':
        assert result['reason'] in ('task_budget_exceeded','task_reported_failure','task_cancelled'),json.dumps(result)
        assert result['taskSuccess'] is False and not result['datasetEligible'],json.dumps(result)
        script=json.loads((artifacts/'script-result.json').read_text())
        assert any(c.get('action')==args.task and c.get('status')!='error' for c in script['commands']),'Assignment did not dispatch'
    print(json.dumps({'validated':args.expect,'runId':args.id,'result':result,'screenshot':{'width':width,'height':height,'updates':len(screenshot_times)}}),flush=True)


if __name__=='__main__': main()
