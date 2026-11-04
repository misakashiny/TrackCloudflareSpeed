import unittest
import policy_model as p
import speed_engine

class ExecutionPolicyTests(unittest.TestCase):
 def capture(self,mode='auto',at=100):return p.capture({},speed_engine.policy({'speed':{'mode':mode}}),6,'1.9',at)
 def test_shared_bounds_match_executor_defaults(self):
  cfg=p.normalized_settings({'interval_seconds':1,'baseline_addresses':100})
  self.assertEqual(cfg['interval_seconds'],900);self.assertEqual(cfg['baseline_addresses'],4)
  self.assertEqual(cfg['candidate_budget_seconds'],25)
 def test_snapshots_never_expose_unrelated_configuration(self):
  v=p.capture({'password':'SECRET','metadata_proxy':'SECRET'},speed_engine.policy({}),6,'1.9',100)
  self.assertNotIn('SECRET',str(v));self.assertTrue(p.verified(v))
 def test_missing_execution_is_not_replaced_by_desired(self):
  v=p.execution_view({}, {'mode':'auto'},6,'1.9',200,{}, {},True)
  self.assertIsNone(v['current']);self.assertFalse(v['execution_reported']);self.assertEqual(v['desired']['speed']['samples'],3)
 def test_saved_change_does_not_rewrite_running_snapshot(self):
  actual=self.capture('auto');v=p.execution_view({}, {'mode':'shadow'},6,'1.9',200,actual,{},True)
  self.assertEqual(v['current']['speed']['mode'],'auto');self.assertEqual(v['desired']['speed']['mode'],'shadow');self.assertTrue(v['pending_next_round'])
 def test_complete_snapshot_is_used_only_when_idle(self):
  v=p.execution_view({}, {'mode':'auto'},6,'1.9',200,{}, {'runtime_policy':self.capture()},False)
  self.assertEqual(v['source'],'last_completed');self.assertFalse(v['pending_next_round'])
 def test_mutated_snapshot_is_not_trusted(self):
  actual=self.capture();actual['speed']['samples']=1
  self.assertFalse(p.verified(actual));self.assertIsNone(p.execution_view({}, {},6,'1.9',200,actual,{},True)['current'])
 def test_timestamp_does_not_create_false_pending_configuration(self):
  self.assertEqual(self.capture(at=100)['fingerprint'],self.capture(at=200)['fingerprint'])
 def test_extra_private_fields_make_a_snapshot_invalid(self):
  v=self.capture();v['password']='PRIVATE';self.assertFalse(p.verified(v))

if __name__=='__main__':unittest.main()
