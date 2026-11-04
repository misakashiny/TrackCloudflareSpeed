import copy
import importlib.util
import io
import ipaddress
import json
from pathlib import Path
import ssl
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import optimizer
import speed_engine as engine

NETS = [ipaddress.ip_network('104.16.0.0/13')]
ORIGINAL, CANDIDATE, CURRENT = '104.16.1.1', '104.16.2.2', '104.16.3.3'


class SelectionTests(unittest.TestCase):
    def cfg(self, **speed):
        return {'speed': engine.policy({'speed': {'mode': 'auto', **speed}}),
                'registry_revision': 6, 'interval_seconds': 1800, 'max_age_seconds': 21600,
                '_previous_entries': {}, '_speed_evidence': {}}

    def run_domain(self, cfg=None, speeds=None, codes=None, dns=None, clock=1000):
        cfg = cfg or self.cfg()
        speeds = speeds or {ORIGINAL: 800, CANDIDATE: 200, CURRENT: 100}
        codes = codes or {}
        answer = dns or {'eligible': True, 'public': True, 'addresses': [ORIGINAL], 'status': 0}
        self.calls = []
        def probe(domain, ip, **kw):
            self.calls.append((domain, ip, kw))
            if speeds.get(ip) is None:
                return {'ok': False, 'error': 'TimeoutError', 'phase': 'tls'}
            code = codes.get(ip, 200)
            return {'ok': True, 'elapsed_ms': speeds[ip], 'http': code}
        with patch.object(engine.time, 'time', return_value=clock):
            return engine.evaluate('example.com', cfg, NETS, lambda *a: answer, probe, [CANDIDATE])

    def test_healthy_original_still_benchmarks_candidate(self):
        row, action, entry = self.run_domain()
        self.assertIn(CANDIDATE, [c[1] for c in self.calls])
        self.assertEqual(row['speed']['proposed_ip'], CANDIDATE)
        self.assertEqual((action, entry), ('remove', None))
        self.assertEqual(row['action'], 'speed_wait_confirmation')

    def test_independent_windows_enable_faster_candidate(self):
        cfg = self.cfg()
        row, _, _ = self.run_domain(cfg)
        cfg['_speed_evidence']['example.com'] = row['speed']['evidence']
        row, action, entry = self.run_domain(cfg, clock=1061)
        self.assertEqual((action, entry['ip']), ('set', CANDIDATE))
        self.assertEqual(row['speed']['gain_pct'], 75)

    def test_same_window_cannot_double_count(self):
        cfg = self.cfg()
        row, _, _ = self.run_domain(cfg)
        cfg['_speed_evidence']['example.com'] = row['speed']['evidence']
        row, action, _ = self.run_domain(cfg, clock=1010)
        self.assertEqual(row['speed']['confirmed_windows'], 1)
        self.assertNotEqual(action, 'set')

    def test_shadow_never_publishes_even_confirmed(self):
        cfg = self.cfg(mode='shadow')
        row, _, _ = self.run_domain(cfg)
        cfg['_speed_evidence']['example.com'] = row['speed']['evidence']
        row, action, entry = self.run_domain(cfg, clock=1061)
        self.assertEqual(row['speed']['confirmed_windows'], 2)
        self.assertEqual((action, entry), ('keep', None))

    def test_canary_only_allows_named_domain(self):
        cfg = self.cfg(mode='canary', allow_domains=['another.example.com'])
        row, action, _ = self.run_domain(cfg, speeds={ORIGINAL: None, CANDIDATE: 100})
        self.assertEqual(action, 'keep')
        cfg['speed']['allow_domains'] = ['example.com']
        row, action, _ = self.run_domain(cfg, speeds={ORIGINAL: None, CANDIDATE: 100})
        self.assertEqual(action, 'set')

    def test_small_gain_does_not_switch(self):
        row, action, _ = self.run_domain(speeds={ORIGINAL: 800, CANDIDATE: 760})
        self.assertEqual(row['action'], 'original_path_healthy')
        self.assertNotEqual(action, 'set')

    def test_fast_403_cannot_win(self):
        row, action, _ = self.run_domain(codes={CANDIDATE: 403})
        self.assertNotEqual(action, 'set')
        self.assertTrue(row['speed']['metrics'][CANDIDATE]['http_limited'])

    def test_different_status_cannot_win(self):
        row, _, _ = self.run_domain(codes={CANDIDATE: 404})
        self.assertNotIn('proposed_ip', row['speed'])

    def test_200_can_restore_a_path_that_only_returned_blocked_403(self):
        row, action, entry = self.run_domain(codes={ORIGINAL:403})
        self.assertEqual((action, entry['ip']), ('set', CANDIDATE))

    def test_expiry_extension_requires_current_ip_samples(self):
        cfg = self.cfg()
        cfg['_previous_entries'] = {'example.com': {'ip': CURRENT, 'expires_at': 1500, 'switched_at': 0}}
        row, action, entry = self.run_domain(cfg)
        self.assertEqual(action, 'renew')
        self.assertEqual(sum(c[1] == CURRENT for c in self.calls), 3)
        self.assertEqual(entry['validated_at'], 1000)
        self.assertEqual(row['speed']['observed_gain_pct'], 87.5)

    def test_failed_current_not_renewed_and_restores_dns(self):
        cfg = self.cfg()
        cfg['_previous_entries'] = {'example.com': {'ip': CURRENT, 'expires_at': 1500}}
        row, action, entry = self.run_domain(cfg, speeds={ORIGINAL: 300, CURRENT: None, CANDIDATE: 400})
        self.assertEqual((action, entry), ('remove', None))

    def test_hold_does_not_preserve_broken_current(self):
        cfg = self.cfg()
        cfg['_previous_entries'] = {'example.com': {'ip': CURRENT, 'expires_at': 1500, 'switched_at': 999}}
        row, action, entry = self.run_domain(cfg, speeds={ORIGINAL: None, CURRENT: None, CANDIDATE: 200})
        self.assertEqual((action, entry['ip']), ('set', CANDIDATE))

    def test_non_cf_never_tries_candidate(self):
        answer = {'eligible': False, 'public': True, 'addresses': [ORIGINAL], 'status': 0}
        _, action, _ = self.run_domain(dns=answer)
        self.assertEqual(action, 'remove')
        self.assertNotIn(CANDIDATE, [c[1] for c in self.calls])

    def test_private_dns_never_probed(self):
        _, action, _ = self.run_domain(dns={'eligible': False, 'public': False, 'addresses': ['127.0.0.1']})
        self.assertEqual((action, self.calls), ('remove', []))

    def test_non_cf_candidate_never_probed(self):
        cfg = self.cfg();cfg['_warm_candidates'] = ['127.0.0.1', '93.184.215.14']
        self.run_domain(cfg)
        self.assertNotIn('127.0.0.1', [c[1] for c in self.calls])
        self.assertNotIn('93.184.215.14', [c[1] for c in self.calls])

    def test_revision_change_resets_confirmation(self):
        cfg = self.cfg();row, _, _ = self.run_domain(cfg)
        cfg['_speed_evidence']['example.com'] = row['speed']['evidence'];cfg['registry_revision'] = 7
        row, action, _ = self.run_domain(cfg, clock=1061)
        self.assertEqual(row['speed']['confirmed_windows'], 1)
        self.assertNotEqual(action, 'set')

    def test_first_candidate_not_automatically_winner(self):
        cfg = self.cfg();cfg['_warm_candidates'] = [CURRENT]
        row, _, _ = self.run_domain(cfg, speeds={ORIGINAL: 800, CURRENT: 500, CANDIDATE: 200})
        self.assertEqual(row['speed']['proposed_ip'], CANDIDATE)

    def test_get_only_for_website_role(self):
        cfg = self.cfg();cfg['_domain_meta'] = {'example.com': {'roles': ['website']}}
        self.run_domain(cfg)
        self.assertEqual({c[2]['method'] for c in self.calls}, {'GET'})

    def test_incomplete_samples_not_stable(self):
        result = engine.summarize_samples([{'ok': True, 'http': 200, 'elapsed_ms': 5}], 3)
        self.assertFalse(result['stable'])

    def test_http_429_preserves_status_and_rate_limit_classification(self):
        r=engine.summarize_samples([{'ok':False,'http':429,'elapsed_ms':10}],3)
        self.assertTrue(r['http_limited']);self.assertEqual(r['http'],429);self.assertFalse(r['stable'])

    def test_round_budget_defers_without_network(self):
        cfg = self.cfg();cfg['_round_deadline'] = -1
        row, action, _ = self.run_domain(cfg)
        self.assertEqual(row['action'], 'speed_deferred');self.assertEqual(self.calls, [])

    def test_comparison_bypasses_own_published_dns(self):
        cfg = self.cfg();cfg['_previous_entries'] = {'example.com':{'ip':CURRENT,'expires_at':time.time()+1000}}
        queried = []
        def resolve(domain, server, timeout):
            queried.append(server);return {'addresses':[ORIGINAL], 'status':0, 'server':server}
        def probe(domain, ip, **kw):return {'ok':True,'http':200,'elapsed_ms':800 if ip==ORIGINAL else 200}
        row, _, _ = engine.evaluate('example.com',cfg,NETS,
            lambda *a:{'eligible':True,'public':True,'addresses':[CURRENT]},probe,[CANDIDATE],resolve)
        self.assertEqual(row['speed']['original_ips'],[ORIGINAL]);self.assertEqual(queried,['1.1.1.1'])

    def test_missing_true_upstream_defers_instead_of_claiming_gain(self):
        def resolve(*a, **kw):raise TimeoutError
        row, action, _ = engine.evaluate('example.com',self.cfg(),NETS,
            lambda *a:{'eligible':True,'public':True,'addresses':[ORIGINAL]},lambda *a,**k:self.fail('must not probe'),[CANDIDATE],resolve)
        self.assertEqual((row['action'],action),('speed_deferred','keep'))

    def test_faster_original_ipv6_prevents_ipv4_regression(self):
        ipv6='2606:4700::1111'
        cfg=self.cfg()
        calls=[]
        def probe(domain, ip, **kw):
            calls.append(ip)
            return {'ok':True,'http':200,'elapsed_ms':{ORIGINAL:800,CANDIDATE:200,ipv6:70}[ip]}
        args=('example.com',cfg,NETS,lambda *a:{'eligible':True,'public':True,'addresses':[ORIGINAL]},probe,[CANDIDATE])
        row,action,entry=engine.evaluate(*args,resolver6=lambda *a,**k:{'status':0,'addresses':[ipv6]})
        self.assertIn(ipv6,calls);self.assertEqual(row['speed']['original_best_ip'],ipv6)
        self.assertNotEqual(action,'set');self.assertIsNone(entry)

    def test_unconfirmed_ipv6_dns_does_not_publish_or_renew(self):
        for reply in [None,{'status':2,'addresses':[]},{'status':0,'addresses':['fd00::1']}]:
            def resolver6(*a,**kw):
                if reply is None:raise TimeoutError
                return reply
            row,action,entry=engine.evaluate('example.com',self.cfg(),NETS,
                lambda *a:{'eligible':True,'public':True,'addresses':[ORIGINAL]},
                lambda *a,**k:self.fail('must wait for full baseline'),[CANDIDATE],resolver6=resolver6)
            self.assertEqual((row['action'],action,entry),('speed_deferred','keep',None))

    def test_new_candidate_must_beat_original_even_when_current_is_slow(self):
        cfg=self.cfg(hold_seconds=300);cfg['_previous_entries']={'example.com':{'ip':CURRENT,'expires_at':1500,'switched_at':0}}
        row,action,entry=self.run_domain(cfg,speeds={ORIGINAL:100,CURRENT:800,CANDIDATE:300})
        self.assertNotEqual(action,'set');self.assertEqual(row['speed']['proposed_ip'],ORIGINAL)
        cfg['_speed_evidence']['example.com']=row['speed']['evidence']
        row,action,entry=self.run_domain(cfg,speeds={ORIGINAL:100,CURRENT:800,CANDIDATE:300},clock=1061)
        self.assertEqual((row['action'],action,entry),('speed_return_original','remove',None))

    def test_fake_ip_baseline_is_explicit_and_never_published_as_candidate(self):
        cfg = self.cfg();cfg['speed']['allow_fake_ip_baseline'] = True
        calls=[]
        def probe(domain, ip, **kw):
            calls.append(ip);return {'ok':True,'http':200,'elapsed_ms':100 if ip.startswith('198.18.') else 500}
        def resolve(*a,**kw):return {'addresses':['198.18.0.29'],'server':'1.1.1.1','status':0}
        row, action, entry = engine.evaluate('example.com',cfg,NETS,
            lambda *a:{'eligible':True,'public':True,'addresses':[ORIGINAL]},probe,[CANDIDATE],resolve)
        self.assertIn('198.18.0.29',calls)
        self.assertEqual(row['baseline_dns']['kind'],'proxy_fake_ip')
        self.assertNotEqual(action,'set');self.assertIsNone(entry)

    def test_fake_ip_without_explicit_allow_or_other_private_ip_is_not_probed(self):
        for enabled, ip in [(False,'198.18.0.29'),(True,'10.0.0.1'),(True,'127.0.0.1')]:
            cfg=self.cfg();cfg['speed']['allow_fake_ip_baseline']=enabled
            row,action,_=engine.evaluate('example.com',cfg,NETS,
                lambda *a:{'eligible':True,'public':True,'addresses':[ORIGINAL]},
                lambda *a,**k:self.fail('private destination must not be probed'),[CANDIDATE],
                lambda *a,**k:{'addresses':[ip],'server':'1.1.1.1','status':0})
            self.assertEqual((row['action'],action),('speed_deferred','keep'))

    def test_unstable_not_counted_as_healthy(self):
        spec = importlib.util.spec_from_file_location('dashboard_speed', Path(__file__).parent / 'web/server.py')
        ui = importlib.util.module_from_spec(spec);spec.loader.exec_module(ui)
        self.assertFalse(ui.healthy_row({'action': 'intermittent_keep_previous_if_valid'}))
        self.assertFalse(ui.healthy_row({'action': 'speed_shadow', 'baseline_healthy': False}))

    def test_additional_exploration_cannot_starve_verified_core_samples(self):
        cfg=self.cfg();cfg['_warm_candidates']=[CURRENT]
        cfg['_speed_evidence']['example.com']={'ip':CANDIDATE}
        calls=[]
        def probe(domain, ip, **kw):
            calls.append(ip);return {'ok':True,'http':200,'elapsed_ms':100}
        row,_,_=engine.evaluate('example.com',cfg,NETS,
            lambda *a:{'eligible':True,'public':True,'addresses':[ORIGINAL]},probe,[CANDIDATE])
        first_extra=calls.index(CURRENT)
        self.assertEqual(calls[:first_extra].count(ORIGINAL),3)
        self.assertEqual(calls[:first_extra].count(CANDIDATE),3)
        self.assertTrue(row['speed']['baseline_complete'])

    def test_unconfirmed_cdn_classification_preserves_until_expiry_without_renewal(self):
        import public_dns
        def answers(*a):raise public_dns.PublicDNSUnconfirmed
        row,action,entry=engine.evaluate('example.com',self.cfg(),NETS,answers,
            lambda *a,**k:self.fail('unconfirmed source must not authorize probing or publication'),[])
        self.assertEqual((row['action'],action,entry),('speed_deferred','keep',None))

    def test_extra_candidate_timeout_cannot_preserve_confirmed_broken_incumbent(self):
        for mode in ['auto','shadow']:
            cfg=self.cfg(mode=mode,domain_budget_seconds=10)
            cfg['_previous_entries']={'example.com':{'ip':CURRENT,'expires_at':1500}}
            extras=['104.16.4.4','104.16.5.5'];cfg['_warm_candidates']=extras
            cfg['_speed_evidence']={'example.com':{'ip':CANDIDATE}}
            clock=[0]
            def probe(domain, ip, **kw):
                clock[0]+=min(kw['timeout'],1 if ip in [ORIGINAL,CURRENT,CANDIDATE] else 4)
                return {'ok':False,'phase':'tls','error':'TimeoutError'}
            with patch.object(engine.time,'time',return_value=1000),patch.object(engine.time,'monotonic',side_effect=lambda:clock[0]):
                row,action,entry=engine.evaluate('example.com',cfg,NETS,
                    lambda *a:{'eligible':True,'public':True,'addresses':[ORIGINAL]},probe,[CANDIDATE])
            self.assertTrue(row['speed']['budget_exhausted'])
            self.assertEqual((action,entry),('remove',None))

    def test_tracker_root_403_is_not_a_confirmed_transport_failure(self):
        self.assertFalse(engine.confirmed_connection_failure([{'ok':True,'http':403,'phase':'http'}]*2,'HEAD'))

    def test_certificate_mismatch_is_sufficient_to_withdraw_unsafe_override(self):
        self.assertTrue(engine.confirmed_connection_failure([{'ok':False,'error':'SSLCertVerificationError','phase':'tls'}],'HEAD'))


class ProbeDeadlineTests(unittest.TestCase):
    def test_tls_timeout_reports_tls(self):
        raw = MagicMock()
        with patch.object(optimizer.socket, 'create_connection', return_value=raw), \
             patch.object(optimizer.TLS, 'wrap_socket', side_effect=TimeoutError):
            self.assertEqual(optimizer.probe('example.com')['phase'], 'tls')

    def test_http_timeout_preserves_tls_success(self):
        wrapped = MagicMock();wrapped.recv.side_effect = TimeoutError
        with patch.object(optimizer.socket, 'create_connection', return_value=MagicMock()), \
             patch.object(optimizer.TLS, 'wrap_socket', return_value=wrapped):
            row = optimizer.probe('example.com')
        self.assertEqual(row['phase'], 'http');self.assertTrue(row['tls_verified'])

    def test_oversize_headers_fail_bounded(self):
        wrapped = MagicMock();wrapped.recv.side_effect = [b'x' * 4096] * 4
        with patch.object(optimizer.socket, 'create_connection', return_value=MagicMock()), \
             patch.object(optimizer.TLS, 'wrap_socket', return_value=wrapped):
            row = optimizer.probe('example.com')
        self.assertFalse(row['ok']);self.assertEqual(wrapped.recv.call_count, 4)


class PublishRaceTests(unittest.TestCase):
    def test_disabling_apply_mid_round_prevents_pending_publication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ['state', 'dns', 'work']:(root/name).mkdir()
            cfg = {'domains':['example.com'], 'registry_revision':6, 'interval_seconds':1800,
                   'max_age_seconds':21600, 'speed':engine.policy({'speed':{'mode':'auto'}})}
            cfg_end = copy.deepcopy(cfg);cfg_end['speed']['mode'] = 'shadow'
            row = {'action':'speed_override','speed':{'applied':True,'evidence':{'streak':2}},
                   'classification':'cloudflare','selected_ip':CANDIDATE}
            with patch.object(optimizer,'ROOT',root),patch.object(optimizer,'STATE',root/'state'), \
                 patch.object(optimizer,'DNS',root/'dns'),patch.object(optimizer,'WORK',root/'work'), \
                 patch.object(optimizer,'config',side_effect=[cfg,cfg_end]),patch.object(optimizer,'ranges',return_value=NETS), \
                 patch.object(optimizer,'rank_candidates',return_value=[CANDIDATE]), \
                 patch.object(optimizer,'check_domain',return_value=(row,'set',{'ip':CANDIDATE,'expires_at':int(time.time())+1000})), \
                 patch('sys.stdout',new_callable=io.StringIO):
                report = optimizer.round_once()
            self.assertEqual(report['active_overrides'],{})
            self.assertNotIn('example.com',(root/'dns/active.hosts').read_text())
            self.assertFalse(report['domains']['example.com']['speed']['applied'])


if __name__ == '__main__':
    unittest.main()
