from site_registry import is_excluded
"""Bounded CFST probes and exact-domain DNS overrides; no downloader access."""
import argparse
import concurrent.futures
import csv
import errno as errno_module
import http.client
import math
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request

try:  # 生产环境（Linux 容器）使用真实跨进程文件锁
    import fcntl
except ImportError:  # 本地 Windows 复盘与测试：降级为总是成功的进程内锁
    import types

    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 2
    fcntl.LOCK_NB = 4
    fcntl.flock = lambda handle, operation=0: True

from site_registry import load_registry, domain_index, duplicate_domains, RegistryError
from applog import LogFile
import speed_engine
import upstream_dns
import public_dns
import policy_model

VERSION = "1.9"


PublicDNSError = public_dns.PublicDNSError

ROOT = Path(os.environ.get("OPTIMIZER_ROOT", "/service"))
STATE = ROOT / "state"
DNS = ROOT / "dns"
WORK = ROOT / "work"
STOP = threading.Event()
TLS = ssl.create_default_context()
MEASUREMENT_LOCK = threading.Lock()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def config():
    cfg = read_json(ROOT / "config" / "settings.json", {})
    registry = load_registry(ROOT / 'control/sites.json', cfg.get('domains', []))
    cfg['domains'] = list(domain_index(registry, enabled_only=True))
    cfg['registry_revision'] = registry['revision']
    cfg['_domain_meta'] = domain_index(registry, enabled_only=True)
    cfg['speed'] = speed_engine.policy(cfg, read_json(ROOT / 'control/speed-policy.json', {}))
    targets = cfg['domains']
    if len(targets) > 256:
        raise ValueError('Too many managed domains')
    for domain in targets:
        if (not isinstance(domain, str) or is_excluded(domain)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,250}[a-z0-9])?\.[a-z]{2,24}", domain)
                or ".." in domain or domain.startswith(".")
                or len(domain.split(".")) < 2):
            raise ValueError("Invalid or excluded domain in configuration")
    # 站点清单被手工改成跨站点重复时，这里是唯一还能发现的地方：
    # 静默合并会让“UI 拒绝重复”和“运行时接受重复”两条路径不一致。
    repeated = duplicate_domains(registry)
    if repeated:
        raise ValueError("域名在多个站点中重复: " + ", ".join(sorted(repeated)[:5]))
    if len(set(targets)) != len(targets):
        raise ValueError("Duplicate domains")
    cfg.update(policy_model.normalized_settings(cfg))
    return cfg


def ranges():
    return [ipaddress.ip_network(line) for line in
            (ROOT / "config" / "cloudflare-ipv4.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def historical_candidates(networks):
    """Reuse bounded recent successful addresses as candidates, never as validation."""
    result = []
    for path in sorted((STATE / 'history').glob('*.json'), reverse=True)[:32]:
        try:
            report = read_json(path, {})
            if not 0 <= time.time() - report.get('finished_at', 0) < 21600:
                continue
            for ip in (report.get('active_overrides') or {}).values():
                if allowed_ip(ip, networks) and ip not in result:result.append(ip)
                if len(result) >= 5:return result
        except (OSError, ValueError, TypeError):
            continue
    return result


def allowed_ip(value, networks):
    try:
        addr = ipaddress.ip_address(value)
        return addr.version == 4 and addr.is_global and any(addr in net for net in networks)
    except ValueError:
        return False


# 失败原因分类：把异常翻译成“阶段 + 中文原因”，供前端直接展示。
# 阶段取值固定为 tcp / tls / http，便于统计与筛选。
_ERROR_REASONS = {
    "ConnectionRefusedError": ("tcp", "443 端口主动拒绝连接，通常是站点没有开 HTTPS 或端口被拦"),
    "ConnectionResetError": ("tcp", "连接被对方重置，可能是中间设备限速或站点侧断流"),
    "ConnectionAbortedError": ("tcp", "连接被中断，通常是本机或中间设备主动断开"),
    "BrokenPipeError": ("tcp", "连接被对方关闭"),
    "TimeoutError": ("tcp", "连接超时，4 秒内没有建成 TCP 连接"),
    "gaierror": ("tcp", "本机解析域名失败"),
    "SSLCertVerificationError": ("tls", "TLS 证书校验不通过（域名不匹配、过期或证书链不完整）"),
    "SSLEOFError": ("tls", "TLS 握手期间连接被关闭"),
    "SSLZeroReturnError": ("tls", "TLS 握手期间对方主动关闭连接"),
    "SSLError": ("tls", "TLS 握手失败"),
    "RemoteDisconnected": ("http", "TLS 已建立但服务器没有返回 HTTP 响应"),
    "BadStatusLine": ("http", "返回内容不是合法 HTTP 响应"),
    "IncompleteRead": ("http", "HTTP 响应被截断"),
}


def describe_error(exc):
    """把探测异常翻译成前端可读的失败原因，保留原始类名以便排查。"""
    name = type(exc).__name__
    phase, reason = _ERROR_REASONS.get(name, ("unknown", "未能归类的连接失败"))
    detail = None
    if isinstance(exc, ssl.SSLCertVerificationError):
        # 证书类失败能拿到更精确的信息，直接透出，便于判断是域名不匹配还是过期。
        verify_message = str(getattr(exc, "verify_message", "") or exc)
        if "hostname mismatch" in verify_message.lower() or "not valid for" in verify_message.lower():
            reason = "证书与域名不匹配，站点换了证书或该 IP 不对应此域名"
        elif "expired" in verify_message.lower():
            reason = "证书已过期"
        elif "self-signed" in verify_message.lower() or "self signed" in verify_message.lower():
            reason = "自签名证书，不在系统信任链中"
        elif "unable to get local issuer" in verify_message.lower():
            reason = "证书链不完整，缺少中间证书"
        detail = verify_message[:200]
    elif isinstance(exc, OSError) and getattr(exc, "errno", None):
        # strerror 比 errno 数字更可读；两者都保留，便于跨平台排查。
        text = getattr(exc, "strerror", None) or ""
        detail = "errno={} {}".format(exc.errno, text).strip()
    return {"error": name, "phase": phase, "reason": reason, "detail": detail}


def peer_certificate(wrapped, domain):
    """尽力读取对方证书主体与有效期；失败不影响连通性判定。"""
    try:
        record = wrapped.getpeercert() or {}
        issuer = {k: v for item in record.get("issuer", []) for k, v in item}
        return {"not_after": record.get("notAfter"), "issuer": issuer.get("organizationName") or issuer.get("commonName")}
    except Exception:
        return None


def probe(domain, address=None, timeout=4, method='HEAD'):
    """对单个域名（或指定地址）做一次 TLS + HEAD 探测。

    失败时除异常类名外，回传 phase / reason / detail，供前端解释“为什么没通过”。
    """
    start = time.monotonic()
    raw = wrapped = None
    deadline = start + timeout
    phase = 'tcp'
    tls_verified = False
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('probe budget exhausted')
        return value
    try:
        raw = socket.create_connection((address or domain, 443), timeout=remaining())
        phase = 'tls'
        raw.settimeout(remaining())
        wrapped = TLS.wrap_socket(raw, server_hostname=domain)
        tls_verified = True
        phase = 'http'
        wrapped.settimeout(remaining())
        certificate = peer_certificate(wrapped, domain)
        if method not in ('HEAD', 'GET'):
            raise ValueError('Only bounded read-only root requests are supported')
        wrapped.sendall((method + " / HTTP/1.1\r\nHost: " + domain +
                         "\r\nUser-Agent: NAS-Domain-Optimizer/1.5\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n").encode())
        payload = b''
        while b'\r\n\r\n' not in payload:
            wrapped.settimeout(remaining())
            chunk = wrapped.recv(min(4096, 16384 - len(payload)))
            if not chunk:
                raise http.client.RemoteDisconnected('no complete HTTP headers')
            payload += chunk
            if len(payload) >= 16384 and b'\r\n\r\n' not in payload:
                raise http.client.HTTPException('HTTP headers exceed 16 KiB')
        header, body = payload.split(b'\r\n\r\n', 1)
        first, *lines = header.decode('iso-8859-1').split('\r\n')
        match = re.match(r'^HTTP/1\.[01] ([1-5][0-9]{2})(?: |$)', first)
        if not match:
            raise http.client.BadStatusLine(first[:80])
        status = int(match.group(1))
        headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                   for line in lines if ':' in line}
        if method == 'GET' and not body and headers.get('content-length') != '0' and status not in (204, 304):
            wrapped.settimeout(remaining())
            body = wrapped.recv(4096)
        ok = status in (200, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405)
        result = {"ok": ok, "http": status, "tls_verified": True, "certificate": certificate,
                # 判定口径不变（ok 仍是原有规则）；这里额外说明“TLS 通了但根路径 4xx”的情况，
                # 因为对 Tracker 主机名来说，404 只能证明服务器活着，不能说明 announce 可用。
                "tls_ok": True,
                "http_recognized": status in (200, 204, 301, 302, 303, 307, 308),
                'method': method, 'bytes_sampled': len(header) + len(body),
                'challenge': headers.get('cf-mitigated') == 'challenge',
                'colo': (headers.get('cf-ray', '').split('-')[-1])[:8] or None,
                "elapsed_ms": round((time.monotonic() - start) * 1000)}
        if not ok:
            result.update(phase='http', reason='HTTP 返回状态 {}'.format(status), error='HTTPStatusError')
        return result
    except Exception as exc:
        result = describe_error(exc)
        result['phase'] = phase
        if isinstance(exc, TimeoutError):
            result['reason'] = {'tcp': 'TCP 建连超时', 'tls': 'TLS 握手超时', 'http': 'HTTP 响应超时'}[phase]
        result.update({"ok": False, "tls_verified": tls_verified, 'tls_ok': tls_verified,
                       "elapsed_ms": round((time.monotonic() - start) * 1000)})
        return result
    finally:
        if wrapped:
            wrapped.close()
        elif raw:
            raw.close()


def public_answers(domain, cfg, networks):
    resolver = cfg.get('_dns_resolver') or public_dns.Resolver(
        cfg.get('metadata_proxy', ''), cfg.get('public_dns_direct_fallback') is True)
    deadline = cfg.get('_dns_deadline', float('inf'))
    result = resolver.resolve(domain, deadline)
    def is_cf(record):
        return record['status'] == 0 and bool(record['addresses']) and all(allowed_ip(ip,networks) for ip in record['addresses'])
    if not is_cf(result):
        # A poisoned/filtered origin or NXDOMAIN reply must not revoke a working CF mapping.
        try:second = resolver.resolve(domain, deadline, exclude=[result['provider']])
        except PublicDNSError:
            raise public_dns.PublicDNSUnconfirmed('公开解析的 CDN 归属未完成交叉确认') from None
        if is_cf(second):
            result = {**second, 'classification_disagreement':True,'initial_provider':result['provider']}
        elif bool(result['addresses']) != bool(second['addresses']):
            raise public_dns.PublicDNSUnconfirmed('公开解析源的有效记录不一致')
        else:
            result['confirmation_provider'] = second['provider']
    answers = result['addresses']
    return {**result, 'eligible': is_cf(result), 'public': bool(answers)}


def rank_candidates(answers, cfg, networks):
    # Use bounded candidate addresses and TCP attempts; no bulk downloads.
    candidates = list(dict.fromkeys(answers))
    rng = random.Random(os.urandom(16))
    while len(candidates) < cfg["candidate_limit"]:
        network = rng.choice(networks)
        value = str(network.network_address + rng.randrange(1, network.num_addresses - 1))
        if value not in candidates:
            candidates.append(value)
    atomic(WORK / "candidates.txt", "\n".join(candidates) + "\n")
    output = WORK / "results.csv"
    if output.exists():
        output.unlink()
    command = ["/usr/local/bin/cfst", "-f", str(WORK / "candidates.txt"),
               "-o", str(output), "-n", str(policy_model.CANDIDATE_SCREEN['threads']), "-t", str(policy_model.CANDIDATE_SCREEN['tcp_attempts']), "-tp", "443", "-dd", "-p", "0", "-tl", "2000", '-tlr', '0']
    maximum=policy_model.CANDIDATE_SCREEN['max_seconds']
    budget = min(maximum, cfg.get('_round_deadline', time.monotonic() + maximum) - time.monotonic())
    if budget <= 1:
        return []
    result = subprocess.run(command, capture_output=True, timeout=budget)
    if result.returncode or not output.exists():
        return []
    ranked = []
    stats = []
    with output.open(encoding='utf-8-sig') as stream:
        for row in csv.reader(stream):
            if len(row) >= 5 and row[0] in candidates and allowed_ip(row[0], networks):
                try:
                    sent, received, loss, delay = int(row[1]), int(row[2]), float(row[3]), float(row[4])
                    if sent < 4 or received != sent or loss != 0 or not math.isfinite(delay):
                        continue
                except ValueError:
                    continue
                ranked.append(row[0])
                stats.append({'ip': row[0], 'sent': sent, 'received': received, 'loss': loss, 'avg_ms': delay})
    cfg['_candidate_stats'] = stats
    return ranked[:policy_model.CANDIDATE_SCREEN['ranked_limit']]


def publish(entries, cfg, now):
    networks = ranges()
    live = {domain: row for domain, row in entries.items()
            if domain in cfg["domains"] and not is_excluded(domain) and speed_engine.site_mode(domain,cfg)!='origin'
            and now < row.get("expires_at", 0) and allowed_ip(row.get("ip", ""), networks)}
    # Only a single active file is exposed. Atomic files live outside hostsdir:
    # dnsmasq must not read a transient or stale second hosts file.
    content = "# NAS Domain Optimizer: exact domains; no Tracker URLs or credentials\n"
    content += "".join(row["ip"] + " " + domain + "\n" for domain, row in sorted(live.items()))
    staging = STATE / "active.hosts.next"
    target = DNS / "active.hosts"
    publisher=STATE/'publish.lock'
    with publisher.open('r' if publisher.exists() else 'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not target.exists() or target.read_text() != content:
            atomic(staging, content)
            os.replace(staging, target)
        atomic(STATE / "entries.json", json.dumps(live, indent=2) + "\n")
    return live


def baseline_probe(domain, addresses, limit):
    """对前若干个 DNS 地址各探测两次，返回 “地址 → 两次结果” 的完整证据。

    旧实现只看 addresses[0]：第一个地址不通会误判“原路径故障”，
    第一个地址通则忽略其余地址。Cloudflare 普遍返回多个地址，这里改为逐个覆盖。
    """
    sampled = list(dict.fromkeys(addresses))[:limit]
    evidence = []
    for address in sampled:
        results = [probe(domain, address), probe(domain, address)]
        evidence.append({'ip': address, 'results': results})
        if all(x['ok'] for x in results):
            # 已有一个地址连续两次通过，原路径即可判定健康，不必再测其余地址。
            break
    return evidence


def _first_failure(evidence):
    """取第一条失败原因，用于向用户解释“为什么没通过”。"""
    for item in evidence:
        for result in item['results']:
            if not result.get('ok'):
                return {'ip': item['ip'], 'phase': result.get('phase'), 'reason': result.get('reason'),
                        'error': result.get('error'), 'http': result.get('http')}
    return None


def check_domain(domain, cfg, networks):
    started = time.monotonic()
    row, action, entry = _check_domain(domain, cfg, networks)
    # 记录本域名本轮耗时，用于"检测耗时排行"和趋势对比。
    row['duration_ms'] = round((time.monotonic() - started) * 1000)
    return row, action, entry


def _check_domain(domain, cfg, networks):
    if 'speed' in cfg:
        return speed_engine.evaluate(domain, cfg, networks, public_answers, probe, cfg.get('_round_candidates', []), upstream_dns.resolve_a,
                                     upstream_dns.resolve_aaaa if cfg.get('_global_ipv6') else None)
    row = {}
    try:
        dns = row['public_dns'] = public_answers(domain, cfg, networks)
        if not dns['addresses']:
            row.update(action='dns_unresolved', classification='unresolved',
                       reason='公开 DNS 没有返回 IPv4 地址（Status={}），可能是域名停用或只有 AAAA 记录'.format(dns.get('status')))
            return row, 'remove', None
        if not dns['public']:
            row.update(action='non_public_address_excluded', classification='excluded',
                       reason='DNS 返回了非公网地址，按规则不探测也不发布')
            return row, 'remove', None
        row['classification'] = 'cloudflare' if dns['eligible'] else 'origin'
        # Connect only to the public address returned by the fixed DNS providers.
        # SNI/Host still use the original domain; arbitrary private destinations are never probed.
        evidence = row['baseline'] = baseline_probe(domain, dns['addresses'], cfg.get('baseline_addresses', 3))
        row['baseline_healthy'] = any(all(x['ok'] for x in item['results']) for item in evidence)
        row['baseline_failure'] = None if row['baseline_healthy'] else _first_failure(evidence)
        if not dns['eligible']:
            if row['baseline_healthy']:
                row['action'] = 'origin_only'
            else:
                row['action'] = 'origin_connection_failed'
                row['reason'] = (row['baseline_failure'] or {}).get('reason') or '所有地址都没有通过 TLS 与 HTTP 检查'
            return row, 'remove', None
        if row['baseline_healthy']:
            row['action'] = 'original_path_healthy'
            return row, 'remove', None
        if any(x['ok'] for item in evidence for x in item['results']):
            # 有成功也有失败：连接在波动。旧覆盖如果还在有效期内就顺延，
            # 否则会出现“这轮明明通过，几小时后优选地址却悄悄过期消失”。
            row['action'] = 'intermittent_keep_previous_if_valid'
            previous = cfg.get('_previous_entries', {}).get(domain)
            checked = [probe(domain, previous['ip']), probe(domain, previous['ip'])] if previous else []
            if previous and all(x['ok'] for x in checked) and cfg.get('_now', time.time()) < previous.get('expires_at', 0):
                row['renewed_ip'] = previous['ip']
                return row, 'renew', {'ip': previous['ip'], 'validated_at': int(time.time()),
                                      'expires_at': int(time.time()) + cfg['max_age_seconds'],
                                      'source': 'intermittent_renew'}
            row['reason'] = '本次连接时通时断，且没有仍在有效期内的优选地址可顺延'
            return row, 'keep', None
        with MEASUREMENT_LOCK:
            # One TCP ranking per round is shared; every domain still performs its own
            # two TLS/HTTP validations before any address can be published for that domain.
            if '_round_candidates' not in cfg:
                cfg['_round_candidates'] = rank_candidates(dns['addresses'], cfg, networks)
            candidates = list(dict.fromkeys(cfg.get('_warm_candidates', []) + dns['addresses'][1:] + cfg['_round_candidates']))[:6]
        row['ranked_candidates'] = candidates
        row['candidate_probes'] = []
        # 总时间预算：旧实现最坏 14 次探测 × 8 秒 = 112 秒/域名，会把整轮拖很久。
        deadline = time.monotonic() + cfg.get('candidate_budget_seconds', 25)
        for address in candidates:
            if time.monotonic() >= deadline:
                row['budget_exhausted'] = True
                row['reason'] = '候选地址较多，验证达到 {} 秒预算上限，本轮停止继续验证'.format(
                    cfg.get('candidate_budget_seconds', 25))
                break
            results = [probe(domain, address), probe(domain, address)]
            row['candidate_probes'].append({'ip': address, 'results': results})
            if all(x['ok'] for x in results):
                row.update(action='validated_override', selected_ip=address)
                return row, 'set', {'ip': address, 'validated_at': int(time.time()),
                                    'expires_at': int(time.time()) + cfg['max_age_seconds']}
        row['action'] = 'no_verified_candidate_keep_previous_until_expiry'
        if row.get('reason') is None:
            last = _first_failure([{'ip': p['ip'], 'results': p['results']} for p in row['candidate_probes']])
            row['reason'] = '验证了 {} 个候选地址，都没有连续两次通过'.format(len(row['candidate_probes']))
            if last:
                row['reason'] += '；最后一次失败：' + str(last.get('reason') or last.get('error'))
        return row, 'keep', None
    except Exception as exc:
        detail = describe_error(exc)
        row.update(action='probe_error_keep_previous_until_expiry', classification='pending',
                   error=detail['error'], phase=detail['phase'], reason=detail['reason'])
        return row, 'keep', None


def round_once(dry_run=False):
    cfg = config()
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime_policy=policy_model.capture(cfg,cfg['speed'],cfg['registry_revision'],VERSION,time.time())
        if not dry_run:atomic(STATE/'active-policy.json',json.dumps(runtime_policy))
        networks = ranges()
        cfg['_round_deadline'] = time.monotonic() + cfg['speed']['round_budget_seconds']
        cfg['_global_ipv6'] = upstream_dns.has_global_ipv6()
        cfg['_dns_resolver'] = public_dns.Resolver(cfg.get('metadata_proxy', ''),
            cfg.get('public_dns_direct_fallback') is True,
            read_json(STATE / 'dns-source-health.json', {}))
        entries = read_json(STATE / "entries.json", {})
        cfg['_warm_candidates'] = list(dict.fromkeys(row['ip'] for row in entries.values()
            if row.get('expires_at', 0) > time.time() and allowed_ip(row.get('ip', ''), networks)))[:3]
        cfg['_warm_candidates'] = list(dict.fromkeys(cfg['_warm_candidates'] + historical_candidates(networks)))[:5]
        # 供“连接波动时顺延有效期”使用：需要看到本轮开始前的覆盖状态。
        cfg['_previous_entries'] = dict(entries)
        cfg['_speed_evidence'] = read_json(STATE / 'speed-evidence.json', {})
        cache = read_json(STATE / 'candidate-cache.json', {})
        cache_ttl = min(1800, cfg['speed']['discovery_seconds']) if not cache.get('ips') or cache.get('deferred') else cfg['speed']['discovery_seconds']
        if 0 <= time.time() - cache.get('at', 0) < cache_ttl:
            cfg['_round_candidates'] = [ip for ip in cache.get('ips', []) if allowed_ip(ip, networks)]
            cfg['_candidate_stats'] = cache.get('metrics', [])
        else:
            try:
                discovery_cfg = {**cfg, 'candidate_limit': cfg['speed']['candidate_pool']}
                ranked = rank_candidates(cfg['_warm_candidates'], discovery_cfg, networks)
                metrics = discovery_cfg.get('_candidate_stats', [])
                cache = {'at': int(time.time()), 'ips': ranked, 'metrics': metrics}
                if metrics and sum(x['avg_ms'] < 2 for x in metrics) >= len(metrics) * .8:
                    cache['route_hint'] = '多数 TCP 建连低于 2ms，可能存在上游接管；仅作初筛，提速结论以实际域名 TLS/HTTP 对照为准'
                if not dry_run:
                    atomic(STATE / 'candidate-cache.json', json.dumps(cache))
            except (OSError, ValueError, subprocess.SubprocessError):
                cache = {'at': int(time.time()), 'ips': cfg['_warm_candidates'], 'metrics': [], 'deferred': True}
                if not dry_run:
                    atomic(STATE / 'candidate-cache.json', json.dumps(cache))
            cfg['_round_candidates'] = cache['ips']
            cfg['_candidate_stats'] = cache['metrics']
        cfg['_now'] = time.time()
        report = {"started_at": int(time.time()), "dry_run": dry_run, "domains": {},
                  'registry_revision': cfg['registry_revision'], 'version': VERSION,
                  'speed_policy': cfg['speed'], 'runtime_policy':runtime_policy,'candidate_pool': cache,
                  'ipv6_baseline_enabled': cfg['_global_ipv6']}
        evidence = {}
        progress = {'started_at': report['started_at'], 'total': len(cfg['domains']), 'completed': 0,
                    'running': True, 'current': []}
        if not dry_run:
            atomic(STATE / 'progress.json', json.dumps(progress))
        previous_rows = read_json(STATE / 'last-run.json', {}).get('domains', {})
        targets = sorted(cfg['domains'], key=lambda d: (
            0 if cfg['speed']['mode'] == 'canary' and d in cfg['speed']['allow_domains'] else 1,
            0 if d not in previous_rows or previous_rows[d].get('action') == 'speed_deferred' else 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=policy_model.PARALLEL_DOMAINS) as pool:
            futures = {pool.submit(check_domain, d, cfg, networks): d for d in targets}
            for future in concurrent.futures.as_completed(futures):
                domain = futures[future]
                row, action, entry = future.result()
                report['domains'][domain] = row
                if row.get('speed', {}).get('evidence'):
                    evidence[domain] = row['speed']['evidence']
                if action in ('set', 'renew'):
                    entry['source'] = entry.get('source', 'candidate_validated')
                    entries[domain] = entry
                elif action == 'remove':
                    entries.pop(domain, None)
                progress['completed'] += 1
                if not dry_run:
                    atomic(STATE / 'progress.json', json.dumps(progress))
        report["finished_at"] = int(time.time())
        report["duration_seconds"] = report["finished_at"] - report["started_at"]
        report["summary"] = summarize(report['domains'])
        report['dns_sources'] = cfg['_dns_resolver'].snapshot()
        if not dry_run:
            atomic(STATE / 'dns-source-health.json', json.dumps(report['dns_sources']))
            latest_cfg = config()
            latest_speed = latest_cfg['speed']
            for domain, row in report['domains'].items():
                can_apply_now = speed_engine.can_apply(domain,latest_cfg)
                if not can_apply_now and row.get('speed', {}).get('applied'):
                    previous = cfg['_previous_entries'].get(domain)
                    if previous:
                        entries[domain] = previous
                    else:
                        entries.pop(domain, None)
                    row['speed']['applied'] = False
                    row['action'] = 'speed_shadow'
                    row['reason'] = '本轮期间应用范围已收紧，撤回尚未发布的切换或续期'
            entries = publish(entries, latest_cfg, report["finished_at"])
            atomic(STATE / 'speed-evidence.json', json.dumps(evidence))
            report["active_overrides"] = {domain: row["ip"] for domain, row in entries.items()}
            atomic(STATE / "last-run.json", json.dumps(report, indent=2) + "\n")
            atomic(STATE / "history" / (str(report["started_at"]) + ".json"),
                   json.dumps(report, indent=2) + "\n")
            rotate_history(cfg.get('history_rounds', 240))
            progress.update(running=False, finished_at=report['finished_at'])
            atomic(STATE / 'progress.json', json.dumps(progress))
            log_round(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return report


def log_round(report):
    """把一轮检测的结论写进运行日志，便于事后按级别/关键字排查。

    只记录摘要与异常域名，不逐条记录每个命中端口的结果，避免日志被淹。
    """
    try:
        log = LogFile(STATE / 'logs')
        summary = report.get('summary') or {}
        counts = summary.get('classifications') or {}
        log.write('info', 'ROUND_DONE',
                  '完成一轮检测：{} 个域名，用时 {} 秒，应用覆盖 {} 个'.format(
                      len(report.get('domains') or {}), report.get('duration_seconds', '—'),
                      len(report.get('active_overrides') or {})),
                  unresolved=counts.get('unresolved'), cloudflare=counts.get('cloudflare'),
                  origin=counts.get('origin'), revision=report.get('registry_revision'))
        # 需要关注的域名单独记一条 warn，排查时直接按 level=warn 就能看到。
        problems = []
        for domain, row in (report.get('domains') or {}).items():
            action = row.get('action') or ''
            if action in ('origin_connection_failed', 'dns_unresolved', 'non_public_address_excluded',
                          'probe_error_keep_previous_until_expiry'):
                problems.append({'domain': domain, 'action': action,
                                 'phase': row.get('phase'), 'reason': row.get('reason')})
        if problems:
            log.write('warn', 'ROUND_PROBLEMS', '本轮有 {} 个域名需要关注'.format(len(problems)),
                      domains=problems[:20])
        if report.get('duration_seconds', 0) > max(60, len(report.get('domains') or {}) * 8):
            log.write('warn', 'ROUND_SLOW', '本轮耗时偏长：{} 秒'.format(report.get('duration_seconds')))
    except Exception:
        pass  # 日志失败不影响检测


def summarize(domains):
    """一轮结束时的计数摘要，供总览页与迭代日志核对。"""
    counts = {'cloudflare': 0, 'origin': 0, 'unresolved': 0, 'excluded': 0, 'pending': 0}
    actions = {}
    reasons = {}
    for row in domains.values():
        classification = row.get('classification') or 'pending'
        counts[classification] = counts.get(classification, 0) + 1
        action = row.get('action') or 'unknown'
        actions[action] = actions.get(action, 0) + 1
        if row.get('reason'):
            # 原因按“阶段 + 类别”聚合，避免把每个域名的原文都堆进摘要。
            phase = row.get('phase') or 'http'
            key = '{}:{}'.format(phase, str(row['reason'])[:40])
            reasons[key] = reasons.get(key, 0) + 1
    return {'classifications': counts, 'actions': actions,
            'top_reasons': dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5])}


def rotate_history(keep):
    """历史文件保留最近 keep 轮，避免长期运行无限增长（旧实现从不清理）。"""
    try:
        files = sorted((STATE / "history").glob("*.json"), key=lambda p: p.name, reverse=True)
        for path in files[keep:]:
            path.unlink()
    except OSError as exc:
        print(json.dumps({"history_rotation_error": type(exc).__name__}), flush=True)


def daemon():
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: STOP.set())
    next_run = 0
    thread = None
    while not STOP.is_set():
        now = int(time.time())
        try:
            cfg = config()
            if thread is None or not thread.is_alive():
                # Remove expired entries even between the 30-minute probe rounds.
                with (STATE / "update.lock").open("a") as maintenance_lock:
                    try:
                        fcntl.flock(maintenance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        publish(read_json(STATE / "entries.json", {}), cfg, now)
                    except BlockingIOError:
                        pass
                request_path = ROOT / "control" / "request.json"
                request = read_json(request_path, {})
                manual = (isinstance(request, dict)
                          and re.fullmatch(r"[a-f0-9]{32}", str(request.get("id", "")))
                          and 0 <= now - request.get("requested_at", 0) <= 300)
                if manual or now >= next_run:
                    if request_path.exists():
                        request_path.unlink()
                    if manual:
                        atomic(STATE / "manual-request.json", json.dumps({
                            "id": request["id"], "started_at": now, "status": "running"}))
                    def job(manual=manual, request=dict(request), started=now):
                        try:
                            round_once()
                            if manual:
                                atomic(STATE / "manual-request.json", json.dumps({
                                    "id": request["id"], "started_at": started,
                                    "finished_at": int(time.time()), "status": "complete"}))
                        except Exception as exc:
                            detail = describe_error(exc) if not isinstance(exc, (ValueError, RegistryError)) else {
                                'error': type(exc).__name__, 'phase': 'config', 'reason': str(exc), 'detail': None}
                            print(json.dumps({"round_error": detail['error'], "reason": detail['reason']}), flush=True)
                            if manual:
                                atomic(STATE / "manual-request.json", json.dumps({
                                    "id": request["id"], "started_at": started,
                                    "finished_at": int(time.time()), "status": "failed",
                                    "reason": detail['reason']}))
                    thread = threading.Thread(target=job, daemon=True)
                    thread.start()
                    next_run = now + cfg["interval_seconds"]
            atomic(STATE / "heartbeat.json", json.dumps({"at": now, "next_run": next_run,
                                                         "probing": bool(thread and thread.is_alive())}) + "\n")
        except Exception as exc:
            print(json.dumps({"service_error": type(exc).__name__,
                              "reason": str(exc)[:200]}, ensure_ascii=False), flush=True)
        STOP.wait(15)


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--once", action="store_true")
    args.add_argument("--dry-run", action="store_true")
    args.add_argument("--health", action="store_true")
    opts = args.parse_args()
    if opts.health:
        heartbeat = read_json(STATE / "heartbeat.json", {})
        raise SystemExit(0 if time.time() - heartbeat.get("at", 0) < 120 else 1)
    if opts.once or opts.dry_run:
        round_once(opts.dry_run)
    else:
        daemon()
