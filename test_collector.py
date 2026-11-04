import json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
import collector
from state_io import read,atomic
import benefit_reports

class Fake(BaseHTTPRequestHandler):
    seen=[]
    def log_message(self,*args):pass
    def reply(self,value,code=200,headers=None):
        b=value if isinstance(value,bytes) else json.dumps(value).encode()
        self.send_response(code)
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def do_GET(self):
        Fake.seen.append(('GET',self.path))
        p=urlsplit(self.path).path
        if p.startswith('/redirect'):
            return self.reply(b'',302,{'Location':'http://127.0.0.1:1/should-not-follow'})
        values={
          '/qb/api/v2/transfer/info':{'dl_info_speed':100,'up_info_speed':200,'up_rate_limit':1000},
          '/qb/api/v2/torrents/info':[{'hash':'a'*40,'name':'PRIVATE_TASK_SENTINEL','upspeed':200,'dlspeed':100,'up_limit':0,'dl_limit':0}],
          '/qb/api/v2/torrents/trackers':[{'url':'https://tracker.example.org/announce?passkey=PRIVATE_KEY_SENTINEL','status':4,'msg':'PRIVATE_REMOTE_ERROR_SENTINEL'}],
          '/qb/api/v2/sync/maindata':{'full_update':True,'rid':1,'server_state':{'alltime_ul':10000,'alltime_dl':5000}}}
        return self.reply(values[p]) if p in values else self.reply({},404)
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length',0)))
        Fake.seen.append(('POST',self.path))
        if self.path=='/qb/api/v2/auth/login':
            assert parse_qs(body.decode())['password']==['test-only-value']
            return self.reply(b'Ok.',headers={'Set-Cookie':'test=1; Path=/'})
        if self.path!='/tr/transmission/rpc':return self.reply({},404)
        if self.headers.get('X-Transmission-Session-Id')!='test-session':return self.reply({},409,{'X-Transmission-Session-Id':'test-session'})
        req=json.loads(body);method=req['method'];Fake.seen.append(('RPC',method))
        assert method in collector.TR.METHODS
        values={'session-stats':{'downloadSpeed':300,'uploadSpeed':400,'cumulative-stats':{'uploadedBytes':20000,'downloadedBytes':15000}},
          'session-get':{'units':{'speed-bytes':1024},'speed-limit-up-enabled':False,'speed-limit-down-enabled':False,'alt-speed-enabled':False},
          'torrent-get':{'torrents':[{'rateUpload':400,'rateDownload':300,'trackerStats':[{'announce':'https://tracker.example.org/announce?passkey=PRIVATE_KEY_SENTINEL','lastAnnounceTime':100,'lastAnnounceSucceeded':True}]}]}}
        return self.reply({'result':'success','arguments':values[method]})

class CollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Fake);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.url='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):Fake.seen=[]
    def config(self,kind,ident=None):return {'id':ident or kind,'type':kind,'name':'Example '+kind,'url':self.url+('/qb' if kind=='qb' else '/tr/transmission/rpc'),'username_env':'TEST_COLLECTOR_USER','password_env':'TEST_COLLECTOR_PASSWORD'}
    def test_end_to_end_two_clients_reports_and_privacy(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,TEST_COLLECTOR_USER='test-user',TEST_COLLECTOR_PASSWORD='test-only-value'):
            root=Path(temp);(root/'runtime/state').mkdir(parents=True);(root/'config').mkdir()
            atomic(root/'config/settings.json',{'domains':['tracker.example.org']})
            atomic(root/'downloaders.json',{'clients':[self.config('qb'),self.config('tr')]})
            result=collector.run_once(root,root/'downloaders.json')
            self.assertTrue(all(x['ok'] for x in result['clients'].values()));self.assertEqual(result['total_up_Bps'],600)
            self.assertEqual(result['clients']['qb']['trackers']['tracker.example.org']['failed'],1)
            self.assertEqual(result['clients']['tr']['trackers']['tracker.example.org']['working'],1)
            self.assertTrue(read(root/'runtime/state/reports/transfer-points.json',[])[0]['all_clients_ok'])
            self.assertEqual(len(read(root/'runtime/state/reports/transfer-points.json',[])[0]['counters']),2)
            for p in (root/'runtime').rglob('*.json'):
                text=p.read_text()
                for secret in ['PRIVATE_KEY_SENTINEL','PRIVATE_TASK_SENTINEL','PRIVATE_REMOTE_ERROR_SENTINEL','test-only-value',self.url]:self.assertNotIn(secret,text)
            atomic(root/'downloaders.json',{'clients':[self.config('tr')]})
            result=collector.run_once(root,root/'downloaders.json');self.assertEqual(set(result['clients']),{'tr'})
    def test_setters_rejected_before_network(self):
        qb=collector.QB({**self.config('qb'),'auth':'none'});tr=collector.TR({**self.config('tr'),'auth':'none'})
        for path in ['/api/v2/torrents/reannounce','/api/v2/torrents/start','/api/v2/app/setPreferences']:
            with self.assertRaises(ValueError):qb.get(path)
        for method in ['torrent-start','torrent-set','torrent-remove','torrent-verify','torrent-reannounce','session-set']:
            with self.assertRaises(ValueError):tr.rpc(method)
        self.assertFalse(Fake.seen)
    def test_config_rejects_plaintext_duplicate_url_and_unsafe_url(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json'
            for clients in [[{**self.config('qb'),'password':'do-not-store'}],[self.config('qb'),self.config('qb','qb2')],[{**self.config('qb'),'url':'http://user:secret@example.org'}],[{**self.config('qb'),'url':'https://example.org/?token=value'}]]:
                atomic(path,{'clients':clients})
                with self.assertRaises(ValueError):collector.settings(path)
    def test_secret_file_and_missing_env(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'secret';p.write_text('test-only-value\n')
            self.assertEqual(collector.credential({'password_file':str(p)},'password'),'test-only-value')
            with self.assertRaises(KeyError):collector.credential({'password_env':'ABSENT_COLLECTOR_TEST_ENV'},'password')
    def test_failed_client_does_not_hide_other_client(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,TEST_COLLECTOR_USER='test-user',TEST_COLLECTOR_PASSWORD='test-only-value'):
            root=Path(temp);(root/'runtime/state').mkdir(parents=True);(root/'config').mkdir()
            bad={**self.config('qb'),'password_env':'ABSENT_COLLECTOR_TEST_ENV'}
            atomic(root/'downloaders.json',{'clients':[bad,self.config('tr')]})
            result=collector.run_once(root,root/'downloaders.json')
            self.assertFalse(result['clients']['qb']['ok']);self.assertTrue(result['clients']['tr']['ok'])
            self.assertFalse(read(root/'runtime/state/reports/transfer-points.json',[])[0]['all_clients_ok'])
    def test_tracker_whitelist_and_unknown_success(self):
        rows=[{'announce':'https://not-managed.example.org/a','lastAnnounceTime':1,'lastAnnounceSucceeded':True},{'announce':'https://tracker.example.org/a','lastAnnounceTime':1}]
        value=collector.tracker_rows(rows,'tr',{'tracker.example.org'})
        self.assertEqual(set(value),{'tracker.example.org'});self.assertEqual(value['tracker.example.org']['pending'],1)
    def test_redirects_are_not_followed(self):
        c={**self.config('qb'),'auth':'none','url':self.url+'/redirect'}
        with self.assertRaises(collector.urllib.error.HTTPError):collector.QB(c).get('/api/v2/transfer/info')
    def test_source_change_resets_and_partial_windows_not_counted(self):
        a={'at':100,'all_clients_ok':True,'counters':{'one':{'source':'a','up':10,'down':0}}}
        b={'at':160,'all_clients_ok':True,'counters':{'one':{'source':'b','up':100,'down':0}}}
        self.assertEqual(benefit_reports.transfer_summary([a,b],0,200)['counter_covered_seconds'],0)
    def test_real_entry_process_one_shot_and_invalid_config_clears_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'runtime/state').mkdir(parents=True);(root/'config').mkdir()
            cfg=root/'downloaders.json';atomic(cfg,{'clients':[self.config('tr')]})
            env=dict(os.environ,COLLECTOR_ROOT=str(root),COLLECTOR_CONFIG=str(cfg),TEST_COLLECTOR_USER='test-user',TEST_COLLECTOR_PASSWORD='test-only-value')
            process=subprocess.run([sys.executable,str(Path(__file__).parent/'entry.py'),'collector','--once'],env=env,capture_output=True,timeout=10)
            self.assertEqual(process.returncode,0);self.assertTrue(read(root/'runtime/state/transfer/latest.json',{})['clients']['tr']['ok'])
            atomic(cfg,{'clients':[{'password':'invalid-private-config'}]})
            process=subprocess.run([sys.executable,str(Path(__file__).parent/'entry.py'),'collector','--once'],env=env,capture_output=True,timeout=10)
            self.assertEqual(process.returncode,0);self.assertFalse(read(root/'runtime/state/transfer/latest.json',{})['clients'])
            self.assertFalse(read(root/'runtime/state/transfer/collector.json',{})['ok'])
    def test_slow_or_oversized_body_is_rejected(self):
        from unittest.mock import MagicMock
        api=collector.Transport({'url':self.url});response=MagicMock();response.__enter__.return_value=response;response.read1.return_value=b'x'*65536
        with patch.object(api.op,'open',return_value=response),patch.object(collector,'MAX_RESPONSE',64):
            with self.assertRaises(ValueError):api.request(self.url)
        api.deadline=0
        with self.assertRaises(TimeoutError):api.request(self.url)

if __name__=='__main__':unittest.main()
