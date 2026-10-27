#!/usr/bin/env python3
"""Exercise real Docker fleet recovery through the authenticated controller API."""
import argparse
import json
import subprocess
import signal
import sys
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ACTIVE={'pending','created','starting','running','unhealthy','stopping','missing'}

class Test:
    def __init__(self, config_path, port):
        self.config_path=config_path.resolve(); self.config=json.loads(config_path.read_text())
        self.state=Path(self.config['state_dir']).resolve()
        if not self.state.is_relative_to(ROOT/'.state') or self.config['fleet_id']!='gallery':
            raise ValueError('This integration test only operates the isolated gallery fleet under .state')
        self.base=f'http://127.0.0.1:{port}';self.port=port;self.process=None;self.log=None;self.checks=[]
    def start(self):
        self.log=(self.state.parent/'controller.log').open('ab')
        self.process=subprocess.Popen([sys.executable,'-m','conductor','serve','--config',str(self.config_path),
            '--port',str(self.port)],cwd=ROOT,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        (self.state.parent/'controller.pid').write_text(str(self.process.pid)+'\n')
        self.wait(lambda:self.api('/readyz',auth=False).get('ok'),'controller ready',60)
    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:self.process.wait(timeout=110)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            self.process=None
        if self.log:self.log.close();self.log=None
    def api(self,path='/api/state',data=None,auth=True):
        headers={'Content-Type':'application/json'}
        if auth:headers['Authorization']='Bearer '+(self.state/'dashboard.token').read_text().strip()
        request=urllib.request.Request(self.base+path,headers=headers,
            data=None if data is None else json.dumps(data).encode())
        with urllib.request.urlopen(request,timeout=110) as response:return json.load(response)
    def wait(self,predicate,label,timeout=360):
        end=time.monotonic()+timeout;next_notice=0;last=None
        while time.monotonic()<end:
            try:
                value=predicate()
                if value:return value
            except (OSError,ValueError) as e:last=str(e)
            if time.monotonic()>=next_notice:
                print('Waiting:',label,flush=True);next_notice=time.monotonic()+20
            time.sleep(2)
        raise TimeoutError(f'{label}: {last}')
    def active(self):return [w for w in self.api()['workers'] if w['state'] in ACTIVE]
    def healthy(self):return [w for w in self.active() if w.get('healthy')]
    def record(self,check,**details):
        self.checks.append({'check':check,'passed':True,**details})
        print('Passed:',check,flush=True)
        self.save()
    def save(self):
        profile={k:self.config[k] for k in ('cpus','memory_mb','storage_mb','max_workers','stagger_seconds')}
        (self.state.parent/'live-results.json').write_text(json.dumps({'checks':self.checks,'wall':time.time(),'profile':profile},indent=2)+'\n')
    def run(self):
        self.start()
        if self.api()['desired']!=0:raise ValueError('Test requires initially drained fleet')
        self.api('/api/fleet/resume',{})
        self.api('/api/fleet',{'desired':2})
        self.wait(lambda:len(self.healthy())==2,'two real workers healthy')
        initial={w['runId'] for w in self.active()}
        self.record('two_workers_measured_healthy',runIds=sorted(initial),health=[w['liveHealth'] for w in self.healthy()])
        self.stop();self.start()
        self.wait(lambda:{w['runId'] for w in self.active()}==initial,'controller restart adopts original workers',60)
        self.record('restart_adopts_without_duplicates',runIds=sorted(initial))
        victim=self.healthy()[0]
        inspected=json.loads(subprocess.check_output(['docker','inspect',victim['backendId']],text=True))[0]
        if inspected['Config']['Labels'].get('dev.vibrato.conductor.fleet')!='gallery':raise ValueError('Unowned runtime')
        subprocess.run(['docker','kill',victim['backendId']],check=True,stdout=subprocess.DEVNULL)
        self.wait(lambda:any(w['runId'] not in initial for w in self.active()),'replacement after forced container death')
        self.wait(lambda:len(self.healthy())==2,'replacement reaches healthy gameplay')
        replacements={w['runId'] for w in self.active()}
        self.record('crash_replaced_with_fresh_run',killed=victim['runId'],newRunIds=sorted(replacements-initial))
        central=Path(self.config['central_dir'])/victim['runId']
        self.wait(lambda:(central/'manifest.json').is_file(),'interrupted evidence centrally quarantined')
        sys.path.insert(0,str(ROOT));from conductor.storage import verify_manifest
        result=verify_manifest(central)
        assert result['datasetEligible'] is False and result['capture']['complete'] is False
        self.record('crash_evidence_verified_and_excluded',runId=victim['runId'])
        manual=self.healthy()[0]['runId']
        before={w['runId'] for w in self.active()}
        self.api(f'/api/workers/{manual}/replace',{})
        self.wait(lambda:any(w['runId'] not in before for w in self.active()),'manual replacement gets fresh run')
        self.wait(lambda:len(self.healthy())==2,'manual replacement reaches healthy gameplay')
        assert self.api()['desired']==2
        self.record('manual_replace_preserves_target',replaced=manual,runIds=sorted(w['runId'] for w in self.active()))
        # Stop targets through persisted intent; preserve all raw evidence from the tests.
        self.api('/api/fleet',{'desired':0})
        self.wait(lambda:self.api()['counts']['active']==0,'target zero drains every owned worker',180)
        self.record('scale_zero_drains_owned_workers')
        self.wait(lambda:all((Path(self.config['central_dir'])/w['runId']/'manifest.json').is_file()
            for w in self.api()['workers']),'all interrupted and stopped runs delivered',120)
        self.record('every_test_run_delivered',runs=len(self.api()['workers']))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'.state/gallery/config.json')
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--keep-dashboard',action='store_true')
    parser.add_argument('--exercise',action='store_true',required=True)
    args=parser.parse_args();test=Test(args.config,args.port)
    def interrupted(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    success=False
    try:test.run();success=True
    finally:
        if test.process is not None:
            try:test.api('/api/fleet',{'desired':0})
            except Exception:pass
        if not success or not args.keep_dashboard:test.stop()
        test.save()
    print('Docker fleet integration passed',flush=True)

if __name__=='__main__':main()
