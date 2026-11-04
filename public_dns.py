"""Fresh public DNS confirmation with bounded failover; never serves stale answers."""
import ipaddress
import json
import math
import threading
import time
import urllib.request
import upstream_dns

PROVIDERS = {
    'alidns': ('https://dns.alidns.com/dns-query', 'direct'),
    'cloudflare': ('https://cloudflare-dns.com/dns-query?type=A&name=', 'configured'),
    'google': ('https://dns.google/resolve?type=A&name=', 'configured'),
}


class PublicDNSError(RuntimeError):
    pass


class PublicDNSUnconfirmed(PublicDNSError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate(value, domain):
    if not isinstance(value, dict) or type(value.get('Status')) is not int or value['Status'] not in (0, 3):
        raise ValueError('DNS response is not a completed answer')
    question = value.get('Question')
    if (value.get('TC') or not isinstance(question, list) or len(question) != 1 or not isinstance(question[0], dict)
            or question[0].get('name', '').rstrip('.').lower() != domain
            or question[0].get('type') != 1):
        raise ValueError('DNS response question does not match')
    rows = value.get('Answer', [])
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('DNS answer list is invalid')
    addresses = []
    for row in rows:
        if not isinstance(row, dict):raise ValueError('DNS answer record is invalid')
        if row.get('type') == 1:
            address = ipaddress.ip_address(row.get('data'))
            if address.version != 4 or not address.is_global:
                raise ValueError('Public DNS returned a private or Fake-IP address')
            addresses.append(str(address))
    if value['Status'] == 3 and addresses:
        raise ValueError('NXDOMAIN has contradictory A records')
    return {'status': value['Status'], 'addresses': list(dict.fromkeys(addresses))}


class Resolver:
    def __init__(self, proxy='', direct_enabled=False, health=None, fetch=None):
        self.proxy = proxy
        self.providers = ['alidns', 'cloudflare', 'google'] if direct_enabled else ['cloudflare', 'google']
        self.lock = threading.Lock()
        self.health = {}
        now = time.time()
        def number(value, default=0):
            return value if isinstance(value,(int,float)) and math.isfinite(value) else default
        for name in self.providers:
            old = health.get(name, {}) if isinstance(health,dict) else {}
            if not isinstance(old,dict):old={}
            self.health[name] = {
                'successes': 0, 'failures': 0, 'inflight': 0,
                'consecutive_failures': 0,
                'last_error': None,
                'latency_ms': max(0,number(old.get('latency_ms'))) if old.get('latency_ms') is not None else None,
                'last_success': min(now, max(0, number(old.get('last_success')))),
                'open_until': min(now + 120, max(0, number(old.get('open_until')))),
            }
        self.fetch = fetch or self._fetch

    def _fetch(self, provider, domain, timeout):
        endpoint, route = PROVIDERS[provider]
        proxy = self.proxy if route == 'configured' else ''
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'https': proxy} if proxy else {}), NoRedirect())
        if provider == 'alidns':
            ident, question, packet = upstream_dns.encode_query(domain, transaction=0)
            request = urllib.request.Request(endpoint, data=packet,
                headers={'Accept':'application/dns-message','Content-Type':'application/dns-message'})
        else:
            request = urllib.request.Request(endpoint + domain, headers={'Accept': 'application/dns-json'})
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(65537)
        if len(payload) > 65536:raise ValueError('DNS response exceeds limit')
        if provider == 'alidns':
            result = upstream_dns.parse(payload, ident, question)
            return {'Status':result['status'],'Question':[{'name':domain,'type':1}],
                    'Answer':[{'type':1,'data':ip} for ip in result['addresses']]}
        return json.loads(payload)

    def resolve(self, domain, deadline=float('inf'), exclude=()):
        # Health controls ordering only. It contains no reusable domain answers.
        with self.lock:
            order = sorted(self.providers, key=lambda n: (
                self.health[n]['open_until'] > time.time(),
                0 if time.time() - self.health[n]['last_success'] < 3600 else 1,
                self.health[n]['latency_ms'] if self.health[n]['latency_ms'] is not None else 400 + self.providers.index(n) * 300))
        for provider in order:
            if provider in exclude:continue
            remaining = deadline - time.monotonic()
            if remaining <= .1:break
            with self.lock:
                state = self.health[provider]
                if state['open_until'] > time.time():continue
                state['inflight'] += 1
            started = time.monotonic()
            try:
                result = validate(self.fetch(provider, domain, min(2.5, remaining)), domain)
                elapsed = round((time.monotonic() - started) * 1000, 1)
                with self.lock:
                    state['successes'] += 1
                    state['latency_ms'] = elapsed if state['latency_ms'] is None else round(.7 * state['latency_ms'] + .3 * elapsed, 1)
                    state['last_success'] = time.time();state['open_until'] = 0;state['consecutive_failures'] = 0;state['last_error'] = None
                return {**result, 'provider': provider,
                        'transport': 'proxy' if PROVIDERS[provider][1] == 'configured' and self.proxy else 'direct',
                        'elapsed_ms': elapsed}
            except Exception as exc:
                with self.lock:
                    state['failures'] += 1;state['consecutive_failures'] += 1
                    state['last_error'] = type(getattr(exc,'reason',None) or exc).__name__
                    if state['consecutive_failures'] >= 3:state['open_until'] = time.time() + 120
            finally:
                with self.lock:state['inflight'] -= 1
        # Do not include provider response text or proxy configuration in logs/UI.
        raise PublicDNSError('公开解析源暂时不可用；保留原覆盖至到期，不据此发布或续期')

    def snapshot(self):
        with self.lock:
            return {name: {k: v for k, v in state.items() if k != 'inflight'}
                    for name, state in self.health.items()}
