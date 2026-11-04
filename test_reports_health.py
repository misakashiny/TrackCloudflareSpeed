import copy
import unittest
from unittest.mock import patch
import benefit_reports as reports
import site_health
import test_speed as speed_tests
from test_speed import ORIGINAL,CANDIDATE,CURRENT
import speed_engine

class SitePolicyTests(unittest.TestCase):
    # Reuse the same paired-probe harness, with different site scope.
    cfg=speed_tests.SelectionTests.cfg
    run_domain=speed_tests.SelectionTests.run_domain
    def test_site_shadow_cannot_widen_global(self):
        cfg=self.cfg(site_modes={'s':'shadow'});cfg['_domain_meta']={'example.com':{'site_id':'s'}}
        row,_,_=self.run_domain(cfg);cfg['_speed_evidence']['example.com']=row['speed']['evidence']
        row,action,_=self.run_domain(cfg,clock=1061)
        self.assertEqual(action,'keep');self.assertFalse(row['speed']['applied']);self.assertEqual(row['speed']['site_policy'],'shadow')
    def test_site_origin_never_probes_candidates_and_withdraws(self):
        cfg=self.cfg(site_modes={'s':'origin'});cfg['_domain_meta']={'example.com':{'site_id':'s'}}
        cfg['_previous_entries']['example.com']={'ip':CURRENT,'expires_at':5000}
        row,action,_=self.run_domain(cfg)
        self.assertEqual(action,'remove');self.assertEqual(row['action'],'origin_only');self.assertTrue(all(c[1]==ORIGINAL for c in self.calls))
    def test_site_inherit_cannot_override_shadow_global(self):
        cfg=self.cfg(mode='shadow',site_modes={'s':'inherit'});cfg['_domain_meta']={'example.com':{'site_id':'s'}}
        self.assertFalse(speed_engine.can_apply('example.com',cfg))
        with self.assertRaises(ValueError):self.cfg(site_modes={'s':'auto'})

class ReportTests(unittest.TestCase):
    def points(self):
        return [{'at':1000+i*60,'all_clients_ok':True,'up_Bps':900000,'counters':{n:{'up':i*60*100000,'down':0,'source':'cumulative'} for n in ('a','b','c')}} for i in range(6)]
    def test_actual_counter_delta_excludes_gap_reset_and_missing_source(self):
        p=self.points();r=reports.transfer_summary(p,1000,1300);self.assertEqual(r['uploaded_bytes'],90000000);self.assertEqual(r['counter_covered_seconds'],300)
        for change in ({'at':9999},{'counters':None},{'all_clients_ok':False}):
            q=copy.deepcopy(p);q[-1].update(change);self.assertLess(reports.transfer_summary(q,1000,10000)['counter_covered_seconds'],300)
        q=copy.deepcopy(p);q[-1]['counters']['a']['up']=0;self.assertEqual(reports.transfer_summary(q,1000,1300)['counter_resets'],1)
    def test_capacity_requires_counters_not_instant_rate_or_old_samples(self):
        p=self.points();r=reports.capacity_evidence(p,1300);self.assertEqual(r['sustained_upload_lower_bound']['lower_Bps'],300000);self.assertIsNone(r['wan_capacity_Bps']);self.assertFalse(r['budget_applied'])
        for x in p:x.pop('counters')
        self.assertIsNone(reports.capacity_evidence(p,1300)['sustained_upload_lower_bound'])
    def test_no_claimed_speed_gain_from_volume_or_empty_pairs(self):
        r=reports.build([],self.points(),[],1300)
        for w in r['reports']:self.assertIsNone(w['https_median_gain_pct']);self.assertIsNone(w['file_transfer_gain_pct']);self.assertEqual(w['status'],'partial')
    def test_only_compatible_verified_https_pairs_count(self):
        r={'started_at':900,'finished_at':1000,'domains':{'a.example':{'speed':{'original_best_ip':'a','current_ip':'b','metrics':{'a':{'stable':True,'http':200,'median_ms':500},'b':{'stable':True,'http':403,'median_ms':100}}}}}}
        self.assertFalse(reports.compact_round(r)['domains']['a.example']['paired'])
        r['domains']['a.example']['speed']['metrics']['b']['http']=200
        out=reports.build([reports.compact_round(r)],[],[],1001)['reports'][0]
        self.assertEqual(out['https_median_gain_pct'],80);self.assertEqual(out['https_paired_comparisons'],1);self.assertFalse(out['full_window'])
    def test_health_does_not_infer_tracker_success_or_currentness(self):
        s={'id':'s','name':'test','enabled':True,'website_domains':['a.example'],'tracker_domains':['t.example']}
        out=site_health.build([s],{'finished_at':1000,'domains':{'a.example':{'action':'origin_only'},'t.example':{'action':'origin_only'}}},{'at':1000,'clients':{}},{},{},1001)[0]
        self.assertEqual(out['status'],'healthy');self.assertEqual(out['tracker']['records'],0);self.assertIsNone(out['tracker']['last_success_at'])
        self.assertEqual(site_health.build([s],{'finished_at':1000},{},{},{},9999)[0]['status'],'stale')
    def test_recovery_error_and_stale_observer_are_visible(self):
        alerts=reports.alerts({}, {'at':1}, {'status':'recovery_blocked','updated_at':1000},{},1001)
        self.assertIn('acceptance.restore',{a['code'] for a in alerts});self.assertIn('observer.stale',{a['code'] for a in alerts})
