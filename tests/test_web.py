import http.cookiejar
from contextlib import contextmanager
import json
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conductor.web import Server, Service, load_token


class FakeFleet:
    def __init__(self, root):
        self.config = SimpleNamespace(artifacts_dir=root)
        self.target=0; self.inhibited=None; self.reconciles=0; self.actions=[]
    def snapshot(self):
        return {'desired': self.target, 'workers': [{'runId':'test-run', 'state':'running'}], 'counts': {}, 'events': []}
    def reconcile(self): self.reconciles+=1; return self.snapshot()
    def set_target(self, n):
        if n<0 or n>4: raise ValueError('Target exceeds capacity')
        self.target=n
    def stop_worker(self, run_id, replace=False): self.actions.append((run_id, replace))
    def resume(self): self.actions.append(('resume',False))
    def set_inhibited(self, reason=None): self.inhibited=reason
    def close(self): pass


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'test-run/logs').mkdir(parents=True)
        (self.root/'test-run/logs/client.log').write_text('first\nsecond\nthird\n')
        (self.root/'secret.txt').write_text('outside-run-private')
        self.fleet=FakeFleet(self.root)
        self.service=Service(self.fleet)
        self.token='operator-token-that-is-at-least-24-chars'
        self.server=Server(('127.0.0.1',0),self.service,self.token)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.shutdown)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.cookies=http.cookiejar.CookieJar()
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
    def shutdown(self): self.server.shutdown(); self.thread.join(); self.server.server_close()
    def request(self,path,data=None,auth=False,headers=None):
        values={'Content-Type':'application/json',**(headers or {})}
        if auth: values['Authorization']='Bearer '+self.token
        request=urllib.request.Request(self.url+path,data=None if data is None else json.dumps(data).encode(),headers=values)
        try:
            with self.client.open(request,timeout=5) as response:
                return response.status,response.headers,response.read()
        except urllib.error.HTTPError as error:
            try: return error.code,error.headers,error.read()
            finally: error.close()
    def test_authentication_cookie_revocation_and_origin_protection(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertEqual(self.request('/api/session',{'token':'wrong'})[0],401)
        self.assertEqual(self.request('/api/session',{'token':'é'*30})[0],401)
        status,headers,_=self.request('/api/session',{'token':self.token})
        self.assertEqual(status,200);self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        self.assertEqual(self.request('/api/state')[0],200)
        self.assertEqual(self.request('/api/fleet',{'desired':1},headers={'Origin':'https://untrusted.example'})[0],403)
        saved_cookie='; '.join(c.name+'='+c.value for c in self.cookies)
        self.assertEqual(self.request('/api/logout',{})[0],200)
        self.assertEqual(self.request('/api/state',headers={'Cookie':saved_cookie})[0],401)
    def test_controls_validate_targets_and_record_replacement_intent(self):
        self.assertEqual(self.request('/api/fleet',{'desired':2},True)[0],200)
        self.assertEqual(self.fleet.target,2)
        self.assertEqual(self.request('/api/fleet',{'desired':True},True)[0],400)
        self.assertEqual(self.request('/api/fleet',{'desired':9},True)[0],400)
        self.assertEqual(self.request('/api/workers/test-run/replace',{},True)[0],202)
        self.assertEqual(self.fleet.actions,[('test-run',True)])
        self.assertEqual(self.request('/api/fleet/resume',{},True)[0],200)
        self.assertEqual(self.fleet.target,2)
    def test_artifact_paths_cannot_escape_run_directory(self):
        self.assertEqual(self.request('/api/artifacts/test-run/logs/client.log',auth=True)[0],200)
        self.assertEqual(self.request('/api/artifacts/test-run/%2e%2e/secret.txt',auth=True)[0],404)
        (self.root/'test-run/leak').symlink_to(self.root/'secret.txt')
        self.assertEqual(self.request('/api/artifacts/test-run/leak',auth=True)[0],404)
        status,_,data=self.request('/api/workers/test-run/logs?component=client&lines=2',auth=True)
        self.assertEqual(status,200);self.assertEqual(json.loads(data)['text'],'second\nthird')
    def test_delivery_failure_pauses_launches_but_not_reconciliation(self):
        class BrokenStore:
            def reaudit_outdated(self,limit): return []
            def snapshot(self): return {}
            def enqueue(self,_): raise OSError('storage unavailable')
            def deliver_pending(self,limit): raise OSError('storage unavailable')
        self.service.artifacts=BrokenStore()
        self.service.tick()
        self.assertEqual(self.fleet.reconciles,1)
        self.assertEqual(self.fleet.inhibited,'artifact_delivery_error')
        self.assertIn('storage unavailable',self.service.snapshot()['controller']['deliveryError'])
    def test_reconcile_wait_requires_auth_and_only_successful_tick_wakes(self):
        self.assertEqual(self.request('/api/reconcile/wait')[0], 401)
        waiting = threading.Event(); result = []
        original = self.service.wait_for_reconcile
        def entered(after):
            waiting.set()
            return original(after)
        with patch.object(self.service, 'wait_for_reconcile', side_effect=entered):
            thread = threading.Thread(target=lambda: result.append(self.request('/api/reconcile/wait?after=0', auth=True)))
            thread.start(); self.assertTrue(waiting.wait(2)); time.sleep(0.05)
            with patch.object(self.fleet, 'reconcile', side_effect=RuntimeError('failed tick')):
                self.service.tick()
            self.assertFalse(result)
            with patch.object(self.service, '_deliver', side_effect=OSError('delivery unavailable')):
                self.service.tick()
            self.assertFalse(result)
            from conductor.artifacts import HistoryBusy
            @contextmanager
            def busy():
                raise HistoryBusy('busy')
                yield
            with patch.object(self.service, 'artifacts', SimpleNamespace(history_guard=busy)):
                self.service.tick()
            self.assertFalse(result)
            self.service.tick(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 200)
        self.assertEqual(json.loads(result[0][2])['generation'], 1)
        status, _, body = self.request('/api/reconcile/wait?after=0', auth=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['generation'], 1)
        self.assertEqual(self.request('/api/reconcile/wait?after=invalid', auth=True)[0], 400)

    def test_reconcile_wait_ends_on_controller_shutdown(self):
        waiting = threading.Event(); result = []
        original = self.service.wait_for_reconcile
        def entered(after):
            waiting.set()
            return original(after)
        with patch.object(self.service, 'wait_for_reconcile', side_effect=entered):
            thread = threading.Thread(target=lambda: result.append(self.request('/api/reconcile/wait?after=0', auth=True)))
            thread.start(); self.assertTrue(waiting.wait(2)); time.sleep(0.05)
            self.service.close(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 503)

    def test_token_free_dashboard_state_controls_and_reconcile_wait(self):
        self.server.token = None
        status, _, html = self.request('/')
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), html)
        status, _, raw = self.request('/api/state')
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)['authRequired'])
        self.assertNotIn(self.token.encode(), raw)
        self.assertEqual(self.request('/api/fleet', {'desired': 2})[0], 200)
        self.assertEqual(self.fleet.target, 2)
        self.assertEqual(self.request('/api/fleet', {'desired': 1}, headers={'Origin': 'https://untrusted.example'})[0], 403)
        self.service.tick()
        status, _, raw = self.request('/api/reconcile/wait?after=0')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['generation'], 1)
        self.assertEqual(self.request('/api/session', {'token': self.token})[0], 404)

    def test_static_console_and_token_file_permissions(self):
        status,headers,data=self.request('/')
        self.assertEqual(status,200);self.assertIn('Live worker gallery',data.decode())
        self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
        self.assertEqual(self.request('/readyz')[0],503)
        self.service.tick();self.assertEqual(self.request('/readyz')[0],200)
        path=self.root/'operator.token';value=load_token(path)
        self.assertGreaterEqual(len(value),24);self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(load_token(path),value)


if __name__=='__main__':unittest.main()

class CanaryServiceConfigurationTests(unittest.TestCase):
    def test_service_config_reaches_scheduler_and_defaults_to_unrestricted(self):
        from conductor.fleet import FleetConfig
        from conductor.web import SERVICE_CONFIG_KEYS, scheduler_from_config
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = {'state_dir': str(root), 'canary_tasks': ['get-oak_log'], 'max_active_canaries': 1}
            config = FleetConfig.from_dict({k: v for k, v in raw.items() if k not in SERVICE_CONFIG_KEYS})
            scheduler = scheduler_from_config(raw, config)
            info = scheduler.snapshot()
            self.assertEqual(info['canaryScheduling'], {'tasks':['get-oak_log'], 'maxActive':1})
            task = next(t for t in info['targets'] if t['id'] == 'task:get-oak_log')
            self.assertTrue(task['canary'])
            default = scheduler_from_config({}, config)
            self.assertEqual(default.snapshot()['canaryScheduling'], {'tasks':[], 'maxActive':0})
