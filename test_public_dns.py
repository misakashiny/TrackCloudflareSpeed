import copy
import time
import socket
import struct
import ipaddress
import optimizer
import unittest
from unittest.mock import patch
import public_dns as dns


def answer(ip='104.16.1.1'):
    return {'Status':0,'Question':[{'name':'example.com.','type':1}],
            'Answer':[{'type':1,'data':ip}]}


class PublicDnsTests(unittest.TestCase):
    def test_default_provider_starts_with_cloudflare_not_google(self):
        calls=[]
        r=dns.Resolver(fetch=lambda n,*a:(calls.append(n) or answer()))
        self.assertEqual(r.resolve('example.com')['provider'],'cloudflare')
        self.assertEqual(calls,['cloudflare'])

    def test_explicit_direct_path_survives_configured_proxy_failure(self):
        calls=[]
        def fetch(n,*a):
            calls.append(n)
            if n!='alidns':raise TimeoutError('private proxy details')
            return answer()
        r=dns.Resolver(proxy='http://127.0.0.1:9',direct_enabled=True,fetch=fetch)
        self.assertEqual(r.resolve('example.com')['transport'],'direct')
        self.assertEqual(calls,['alidns'])

    def test_servfail_falls_through_instead_of_becoming_no_records(self):
        calls=[]
        def fetch(n,*a):
            calls.append(n);return {'Status':2} if n=='cloudflare' else answer()
        self.assertEqual(dns.Resolver(fetch=fetch).resolve('example.com')['provider'],'google')
        self.assertEqual(calls,['cloudflare','google'])

    def test_dead_source_is_skipped_for_other_domains_in_same_window(self):
        calls=[]
        def fetch(n,*a):calls.append(n);raise TimeoutError('FAKE_SECRET')
        r=dns.Resolver(fetch=fetch)
        for _ in range(4):
            with self.assertRaises(dns.PublicDNSError) as e:r.resolve('example.com')
            self.assertNotIn('FAKE_SECRET',str(e.exception))
        self.assertEqual(calls,['cloudflare','google']*3)

    def test_one_transient_failure_does_not_open_global_circuit(self):
        def fetch(n,*a):
            if n=='cloudflare':raise TimeoutError
            return answer()
        r=dns.Resolver(fetch=fetch);r.resolve('example.com')
        self.assertEqual(r.snapshot()['cloudflare']['open_until'],0)

    def test_alidns_wire_reply_must_match_actual_question(self):
        _,question,_=dns.upstream_dns.encode_query('example.com',transaction=0)
        payload=struct.pack('!6H',0,0x8180,1,1,0,0)+question+b'\xc0\x0c'+struct.pack('!HHIH',1,1,60,4)+socket.inet_aton('104.16.1.1')
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return payload
        class Opener:
            def open(self,request,timeout):
                self_request=request
                assert request.get_header('Content-type')=='application/dns-message'
                return Response()
        with patch.object(dns.urllib.request,'build_opener',return_value=Opener()):
            self.assertEqual(dns.Resolver(direct_enabled=True).resolve('example.com')['addresses'],['104.16.1.1'])
            with self.assertRaises(ValueError):dns.Resolver()._fetch('alidns','other.example.com',1)

    def test_each_query_is_fresh_not_a_reused_success(self):
        values=iter([answer(),answer('104.16.2.2')])
        r=dns.Resolver(fetch=lambda *a:next(values))
        self.assertNotEqual(r.resolve('example.com')['addresses'],r.resolve('example.com')['addresses'])

    def test_mismatched_or_truncated_question_is_rejected(self):
        values=[answer(),answer(),answer()]
        values[0]['Question'][0]['name']='other.example.com'
        values[1]['Question'][0]['type']=28
        values[2]['TC']=True
        for v in values:
            with self.assertRaises(ValueError):dns.validate(v,'example.com')

    def test_fake_private_or_ipv6_never_becomes_public_ipv4(self):
        for ip in ['198.18.0.1','127.0.0.1','10.0.0.1','2606:4700::1111']:
            with self.assertRaises(ValueError):dns.validate(answer(ip),'example.com')

    def test_valid_nxdomain_remains_distinct_from_transport_failure(self):
        v=answer();v['Status']=3;v['Answer']=[]
        self.assertEqual(dns.validate(v,'example.com'),{'status':3,'addresses':[]})
        v['Answer']=[{'type':1,'data':'104.16.1.1'}]
        with self.assertRaises(ValueError):dns.validate(v,'example.com')

    def test_deadline_does_not_start_new_network_calls(self):
        r=dns.Resolver(fetch=lambda *a:self.fail('must not request after deadline'))
        with self.assertRaises(dns.PublicDNSError):r.resolve('example.com',time.monotonic()-1)

    def test_health_snapshot_has_no_proxy_credentials_or_domain_answers(self):
        r=dns.Resolver(proxy='http://user:FAKE_SECRET@127.0.0.1:9',fetch=lambda *a:answer())
        r.resolve('example.com');s=str(r.snapshot())
        for text in ['FAKE_SECRET','example.com','104.16.1.1','127.0.0.1']:self.assertNotIn(text,s)

    def test_corrupt_health_cache_cannot_crash_resolver(self):
        for health in [[],{'cloudflare':None},{'cloudflare':{'last_success':'bad','open_until':None,'latency_ms':float('nan')}}]:
            self.assertTrue(dns.Resolver(health=health,fetch=lambda *a:answer()).resolve('example.com')['addresses'])

    def test_redirect_to_unrecognized_endpoint_is_not_followed(self):
        self.assertIsNone(dns.NoRedirect().redirect_request(None,None,302,'',{},'https://other.example.com'))

    def test_origin_poisoning_cannot_hide_fresh_cloudflare_confirmation(self):
        resolver=dns.Resolver(direct_enabled=True,fetch=lambda n,*a:answer('104.244.46.244' if n=='alidns' else '104.16.1.1'))
        r=optimizer.public_answers('example.com',{'_dns_resolver':resolver},[ipaddress.ip_network('104.16.0.0/13')])
        self.assertTrue(r['eligible']);self.assertEqual(r['provider'],'cloudflare')
        self.assertTrue(r['classification_disagreement'])

    def test_non_cf_classification_requires_two_fresh_agreeing_sources(self):
        calls=[]
        resolver=dns.Resolver(direct_enabled=True,fetch=lambda n,*a:(calls.append(n) or answer('93.184.215.14')))
        r=optimizer.public_answers('example.com',{'_dns_resolver':resolver},[ipaddress.ip_network('104.16.0.0/13')])
        self.assertFalse(r['eligible']);self.assertTrue(r['public']);self.assertEqual(len(calls),2)

    def test_missing_secondary_confirmation_cannot_revoke_existing_mapping(self):
        def fetch(n,*a):
            if n=='alidns':return answer('104.244.46.244')
            raise TimeoutError
        resolver=dns.Resolver(direct_enabled=True,fetch=fetch)
        with self.assertRaises(dns.PublicDNSUnconfirmed):
            optimizer.public_answers('example.com',{'_dns_resolver':resolver},[ipaddress.ip_network('104.16.0.0/13')])

    def test_mixed_nxdomain_and_real_origin_is_unconfirmed(self):
        def fetch(n,*a):
            v=answer('93.184.215.14')
            if n=='alidns':v['Status']=3;v['Answer']=[]
            return v
        resolver=dns.Resolver(direct_enabled=True,fetch=fetch)
        with self.assertRaises(dns.PublicDNSUnconfirmed):
            optimizer.public_answers('example.com',{'_dns_resolver':resolver},[ipaddress.ip_network('104.16.0.0/13')])


if __name__=='__main__':unittest.main()
