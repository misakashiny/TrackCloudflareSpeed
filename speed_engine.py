"""Paired, bounded, per-domain performance selection; no downloader mutations."""
import hashlib
import ipaddress
import json
import math
import statistics
import time

DEFAULTS = {
    'mode': 'shadow', 'allow_domains': [], 'samples': 3, 'candidates': 3,
    'min_gain_ratio': .20, 'min_gain_ms': 100, 'confirm_windows': 2,
    'window_gap_seconds': 60, 'hold_seconds': 1800,
    'domain_budget_seconds': 35, 'round_budget_seconds': 420,
    'discovery_seconds': 21600, 'candidate_pool': 32,
    'allow_fake_ip_baseline': False, 'site_modes': {},
}
MODES = {'shadow', 'canary', 'auto'}


def policy(settings, control=None):
    value = {**DEFAULTS, **(settings.get('speed') or {})}
    if control:
        value.update({k: control[k] for k in ['mode', 'allow_domains','site_modes'] if k in control})
    if value['mode'] not in MODES:
        raise ValueError('提速模式必须是 shadow、canary 或 auto')
    bounds = {'samples': (3, 5), 'candidates': (1, 4), 'confirm_windows': (2, 4),
              'window_gap_seconds': (60, 3600), 'hold_seconds': (300, 21600),
              'domain_budget_seconds': (10, 60), 'round_budget_seconds': (60, 600),
              'discovery_seconds': (1800, 86400), 'candidate_pool': (8, 64)}
    for key, (low, high) in bounds.items():
        value[key] = min(high, max(low, int(value[key])))
    value['min_gain_ratio'] = min(.8, max(.05, float(value['min_gain_ratio'])))
    value['min_gain_ms'] = min(2000, max(20, float(value['min_gain_ms'])))
    if not math.isfinite(value['min_gain_ratio']) or not math.isfinite(value['min_gain_ms']):
        raise ValueError('提速阈值必须是有限数字')
    hosts = value['allow_domains']
    if not isinstance(hosts, list) or len(hosts) > 256 or any(not isinstance(d, str) for d in hosts):
        raise ValueError('灰度域名清单格式不正确')
    value['allow_domains'] = list(dict.fromkeys(hosts))
    modes=value['site_modes']
    if not isinstance(modes,dict) or len(modes)>128 or any(not isinstance(k,str) or len(k)>80 or v not in ('inherit','shadow','origin') for k,v in modes.items()):
        raise ValueError('站点策略格式不正确')
    value['site_modes']={k:v for k,v in modes.items() if v!='inherit'}
    return value

def site_mode(domain,cfg):
    return cfg.get('speed',{}).get('site_modes',{}).get(cfg.get('_domain_meta',{}).get(domain,{}).get('site_id'),'inherit')

def can_apply(domain,cfg):
    p=cfg['speed']
    return site_mode(domain,cfg)=='inherit' and (p['mode']=='auto' or p['mode']=='canary' and domain in p['allow_domains'])


def summarize_samples(results, expected):
    successful = [r for r in results if r.get('ok')]
    codes = [r.get('http') for r in successful]
    reported_codes = [r.get('http') for r in results if r.get('http') is not None]
    blocked = any(r.get('http') in (401, 403, 407, 429) or r.get('challenge') for r in results)
    latencies = [r['elapsed_ms'] for r in successful
                 if isinstance(r.get('elapsed_ms'), (int, float)) and math.isfinite(r['elapsed_ms'])]
    return {
        'tested': len(results), 'expected': expected, 'successes': len(successful),
        'rate': round(len(successful) / len(results), 3) if results else 0,
        'stable': len(results) == expected and len(successful) == expected
                  and len(latencies) == expected and len(set(codes)) == 1 and not blocked,
        'median_ms': round(statistics.median(latencies), 1) if latencies else None,
        'max_ms': round(max(latencies), 1) if latencies else None,
        'http': reported_codes[-1] if reported_codes else None, 'http_limited': blocked,
    }


def compatible(reference, candidate):
    if not candidate.get('stable'):
        return False
    # A fast login/challenge/error page must not win a speed comparison.
    if reference and reference.get('http') is not None:
        return reference['http'] == candidate.get('http')
    return True


def improved(reference, candidate, p):
    if not reference or not compatible(reference, candidate):
        return False
    before, after = reference.get('median_ms'), candidate.get('median_ms')
    return (before is not None and after is not None and before > 0
            and before - after >= p['min_gain_ms']
            and (before - after) / before >= p['min_gain_ratio'])


def confirmed_connection_failure(results, method):
    if any(x.get('error') == 'SSLCertVerificationError' for x in results):
        return True
    return sum(not x.get('ok') and (x.get('phase') in ('tcp','tls')
               or (method == 'GET' and x.get('http') is None)) for x in results) >= 2


def next_evidence(domain, winner, cfg, now):
    p = cfg['speed']
    key = hashlib.sha256(json.dumps({
        'engine': '1.7',
        'domain': domain, 'revision': cfg.get('registry_revision'),
        'ratio': p['min_gain_ratio'], 'ms': p['min_gain_ms'], 'samples': p['samples'],
    }, sort_keys=True).encode()).hexdigest()[:16]
    old = cfg.get('_speed_evidence', {}).get(domain, {})
    streak = 1
    at = now
    if old.get('ip') == winner and old.get('key') == key:
        gap = now - old.get('at', 0)
        if 0 <= gap < p['window_gap_seconds']:
            return dict(old)
        if p['window_gap_seconds'] <= gap <= max(7200, cfg.get('interval_seconds', 1800) * 3):
            streak = min(p['confirm_windows'], old.get('streak', 0) + 1)
    return {'ip': winner, 'key': key, 'streak': streak, 'at': at}


def evaluate(domain, cfg, networks, public_answers, probe, ranked, resolver=None, resolver6=None):
    started = time.monotonic()
    mode=site_mode(domain,cfg)
    p = {**cfg['speed'],**({'mode':'shadow'} if mode!='inherit' else {})}
    deadline = min(started + p['domain_budget_seconds'], cfg.get('_round_deadline', float('inf')))
    now = int(time.time())
    row = {'version': '1.7', 'speed': {'mode': p['mode'], 'metrics': {},
            'required_windows': p['confirm_windows'], 'scope': 'HTTPS 根路径请求；不代表真实 Tracker 汇报',
            'applied': False, 'gain_ms': None, 'gain_pct': None}}
    s = row['speed']
    s['site_policy']=mode
    try:
        if time.monotonic() >= deadline:
            row.update(action='speed_deferred', classification='pending', reason='本轮时间预算已用完，下轮继续')
            return row, 'keep', None
        dns_cfg = {**cfg, '_dns_deadline': deadline}
        dns = row['public_dns'] = public_answers(domain, dns_cfg, networks)
        if not dns['addresses']:
            row.update(action='dns_unresolved', classification='unresolved', reason='公开 DNS 没有返回 IPv4 地址')
            return row, 'remove', None
        if not dns['public']:
            row.update(action='non_public_address_excluded', classification='excluded', reason='解析包含非公网地址，不进行探测')
            return row, 'remove', None
        row['classification'] = 'cloudflare' if dns['eligible'] else 'origin'
        original_addresses = dns['addresses']
        if resolver is not None:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= .15:raise TimeoutError('upstream DNS budget exhausted')
                baseline = row['baseline_dns'] = resolver(domain, cfg.get('baseline_dns_server', '1.1.1.1'), timeout=min(2, remaining))
                original_addresses = baseline['addresses']
                fake_pool = ipaddress.ip_network('198.18.0.0/15')
                fake_baseline = bool(p.get('allow_fake_ip_baseline') is True
                    and baseline.get('server') == cfg.get('baseline_dns_server', '1.1.1.1') and original_addresses
                    and all(ipaddress.ip_address(x) in fake_pool for x in original_addresses))
                if not original_addresses or (not fake_baseline and not all(ipaddress.ip_address(x).is_global for x in original_addresses)):
                    row.update(action='speed_deferred', reason='真实上游解析无公网 IPv4，暂不应用提速覆盖')
                    return row, 'keep', None
                if dns['eligible'] and not fake_baseline and not all(allowed(x, networks) for x in original_addresses):
                    # Geographic/multi-CDN answers can both be legitimate. Compare the
                    # actual public native route against confirmed CF candidates, each
                    # using the same hostname, TLS verification and HTTP acceptance.
                    row['classification'] = 'multi_cdn'
                    s['multi_cdn'] = True
                baseline['kind'] = 'proxy_fake_ip' if fake_baseline else 'multi_cdn_upstream' if s.get('multi_cdn') else 'public_upstream'
            except (OSError, ValueError):
                row.update(action='speed_deferred', reason='无法取得绕过本工具的真实上游解析，等待重试')
                return row, 'keep', None
        originals = list(dict.fromkeys(original_addresses))[:cfg.get('baseline_addresses', 3)]
        if resolver6 is not None:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= .15:raise TimeoutError('IPv6 DNS budget exhausted')
                baseline6 = row['baseline_dns6'] = resolver6(domain, cfg.get('baseline_dns_server', '1.1.1.1'), timeout=min(2, remaining))
                addresses6 = baseline6['addresses']
                if baseline6['status'] not in (0, 3) or any(
                        ipaddress.ip_address(x).version != 6 or not ipaddress.ip_address(x).is_global
                        for x in addresses6):
                    raise ValueError('incomplete or non-public IPv6 baseline')
                originals += list(dict.fromkeys(addresses6))[:2]
                s['ipv6_baseline'] = 'included' if addresses6 else 'no_upstream_aaaa'
            except (OSError, ValueError):
                row.update(action='speed_deferred', reason='IPv6 原路径对照未确认，暂不切换或续期')
                return row, 'keep', None
        previous = cfg.get('_previous_entries', {}).get(domain, {})
        current = previous.get('ip') if previous.get('expires_at', 0) > now else None
        if current and not allowed(current, networks):
            current = None
        if mode=='origin':current=None
        candidates = []
        if dns['eligible'] and mode!='origin':
            warm = cfg.get('_warm_candidates', [])
            ordered = []
            # Preserve the previous per-domain winner for an independent confirmation window.
            prior_winner = cfg.get('_speed_evidence', {}).get(domain, {}).get('ip')
            for i in range(max(len(warm), len(ranked))):
                if i < len(warm):ordered.append(warm[i])
                if i < len(ranked):ordered.append(ranked[i])
            available = [ip for ip in list(dict.fromkeys(ordered))
                if allowed(ip, networks) and ip not in originals and ip != current]
            if available:
                offset = (now // p['window_gap_seconds'] + int(hashlib.sha256(domain.encode()).hexdigest()[:4],16)) % len(available)
                available = available[offset:] + available[:offset]
            if prior_winner in available:
                available.remove(prior_winner);available.insert(0,prior_winner)
            candidates = available[:p['candidates']]
        targets = list(dict.fromkeys(originals + ([current] if current else []) + candidates))
        results = {ip: [] for ip in targets}
        roles = cfg.get('_domain_meta', {}).get(domain, {}).get('roles', [])
        method = 'GET' if 'website' in roles else 'HEAD'
        s.update(method=method, current_ip=current, original_ips=originals,
                 candidate_ips=candidates, baseline_source='代理 Fake-IP 原路径（仅对照，不发布此 IP）' if row.get('baseline_dns', {}).get('kind') == 'proxy_fake_ip' else ('局域网上游 DNS，绕过本工具覆盖' if resolver else '隔离测试解析'))
        # Finish a paired comparison before spending time on additional exploration.
        core = list(dict.fromkeys(originals + ([current] if current else []) + candidates[:1]))
        groups = [core] + [[ip] for ip in candidates[1:]]
        for group in groups:
            for i in range(p['samples']):
                order = group if i % 2 == 0 else list(reversed(group))
                for ip in order:
                    prior = results[ip]
                    if (sum(not x.get('ok') for x in prior) >= 2
                            or any(x.get('error') == 'SSLCertVerificationError' or x.get('http') in (401,403,407,429)
                                   or x.get('challenge') for x in prior)):
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining < .15:
                        s['budget_exhausted'] = True
                        break
                    results[ip].append(probe(domain, ip, timeout=min(4, remaining), method=method))
                if s.get('budget_exhausted'):
                    break
            if s.get('budget_exhausted'):
                break
        metrics = s['metrics'] = {ip: summarize_samples(v, p['samples']) for ip, v in results.items()}
        row['baseline'] = [{'ip': ip, 'results': results[ip]} for ip in originals]
        row['candidate_probes'] = [{'ip': ip, 'results': results[ip]} for ip in candidates]
        row['ranked_candidates'] = candidates
        stable_originals = [ip for ip in originals if metrics[ip]['stable']]
        original_best = min(stable_originals, key=lambda ip: metrics[ip]['median_ms']) if stable_originals else None
        s['original_best_ip'] = original_best
        s['original_ms'] = metrics[original_best]['median_ms'] if original_best else None
        s['current_ms'] = metrics.get(current, {}).get('median_ms')
        if original_best and current and metrics.get(current, {}).get('stable'):
            before = metrics[original_best]['median_ms']
            s['observed_gain_ms'] = round(before - metrics[current]['median_ms'], 1)
            s['observed_gain_pct'] = round(100 * s['observed_gain_ms'] / before, 1) if before else None
        row['baseline_healthy'] = bool(original_best)
        s['baseline_complete'] = all(len(results[ip]) == p['samples']
            or sum(not x.get('ok') for x in results[ip]) >= 2
            or any(x.get('error') == 'SSLCertVerificationError' or x.get('http') in (401,403,407,429)
                   or x.get('challenge') for x in results[ip]) for ip in originals)
        row['baseline_failure'] = next(({'ip': ip, **r} for ip in originals for r in results[ip] if not r.get('ok')), None)
        if not dns['eligible'] or mode=='origin':
            row['action'] = 'origin_only' if original_best else 'origin_connection_failed'
            if mode=='origin':row['reason']='站点策略为原解析，仅监控，不发布优选地址'
            if not original_best:
                row['reason'] = '普通域名未稳定通过；不套用 Cloudflare IP'
            return row, 'remove', None
        current_stats = metrics.get(current, {})
        current_stable = bool(current_stats.get('stable'))
        current_failed = bool(current and confirmed_connection_failure(results[current], method))
        reference_ip = current if current_stable else original_best
        reference = metrics.get(reference_ip)
        s['reference_ip'] = reference_ip
        s['reference_ms'] = reference.get('median_ms') if reference else None
        # With 4xx-only evidence, speed cannot be used to infer business success.
        reference_code = reference
        eligible = [ip for ip in targets if compatible(reference_code, metrics[ip])]
        best = min(eligible, key=lambda ip: metrics[ip]['median_ms']) if eligible else None
        s['best_ip'] = best
        s['best_ms'] = metrics[best]['median_ms'] if best else None
        if best and reference and reference['median_ms']:
            s['gain_ms'] = round(reference['median_ms'] - metrics[best]['median_ms'], 1)
            s['gain_pct'] = round(100 * s['gain_ms'] / reference['median_ms'], 1)
        recovery = bool(best and not reference and s['baseline_complete'])
        # A new IPv4 address must also beat the fastest original path, including IPv6.
        faster = bool(best and s['baseline_complete'] and best != reference_ip and improved(reference, metrics[best], p)
                      and (best in originals or not original_best
                           or improved(metrics[original_best], metrics[best], p)))
        # Returning to DNS requires all sampled A/AAAA paths to work, not only the fastest.
        revert = bool(current and best in originals and len(stable_originals) == len(originals)
                      and faster and all(compatible(current_stats, metrics[ip]) for ip in originals)
                      and all(improved(current_stats, metrics[ip], p) for ip in originals))
        if best in originals and not current:
            # Pinning an already healthy DNS member has no demonstrated speed benefit.
            faster = False
        if faster or recovery:
            ev = next_evidence(domain, best, cfg, now)
            s.update(evidence=ev, confirmed_windows=ev['streak'], proposed_ip=best,
                     decision='recovery' if recovery else 'faster')
        else:
            ev = {'ip': None, 'streak': 0, 'at': now}
            s.update(evidence=ev, confirmed_windows=0, decision='retain' if current_stable else 'original')
        applicable = can_apply(domain,cfg)
        held = current_stable and now - previous.get('switched_at', 0) < p['hold_seconds']
        confirmed = recovery or ev.get('streak', 0) >= p['confirm_windows']
        if not applicable:
            row.update(action='speed_failed_removed' if current_failed else 'speed_shadow',
                       reason='当前优选连接或证书已确认未通过，只测模式也撤回失效覆盖' if current_failed else '仅对照测量，不发布新地址')
            # A confirmed-broken active override must not survive just because rollout is shadow.
            return row, ('remove' if current_failed else 'keep'), None
        if (faster or recovery) and confirmed and not held:
            if revert:
                s['applied'] = True
                row.update(action='speed_return_original', reason='原解析各地址均稳定且更快，撤回覆盖')
                return row, 'remove', None
            if best != current and allowed(best, networks) and (best not in originals or recovery):
                s['applied'] = True
                row.update(action='speed_override', selected_ip=best,
                           reason='候选稳定通过，恢复连接' if recovery else '跨窗口对照确认更快，应用优选')
                return row, 'set', {'ip': best, 'validated_at': now, 'expires_at': now + cfg['max_age_seconds'],
                                   'switched_at': now, 'source': 'speed_recovery' if recovery else 'speed_comparison',
                                   'gain_ms': s['gain_ms'], 'gain_pct': s['gain_pct']}
        if current_stable:
            row.update(action='speed_keep_verified', renewed_ip=current,
                       reason='当前地址已重新验证；' + ('冷却期内保持' if held else '暂未发现足够明确的新收益'))
            s['applied'] = True
            return row, 'renew', {**previous, 'validated_at': now,
                                 'expires_at': now + cfg['max_age_seconds'], 'source': 'speed_revalidated'}
        if current_failed:
            row.update(action='speed_failed_removed', reason='当前优选的连接或证书已确认未通过，撤回覆盖恢复原解析；候选仍需独立验证')
            return row, 'remove', None
        if s.get('budget_exhausted'):
            row.update(action='speed_deferred', reason='对照样本未完成，不据此切换或续期')
            return row, 'keep', None
        if original_best:
            row.update(action='speed_wait_confirmation' if faster else 'original_path_healthy',
                       reason='候选收益等待下一独立窗口确认' if faster else '原解析可用，尚无明确更快的候选')
            return row, 'remove', None
        row.update(action='speed_unstable', reason='没有稳定通过的地址；不以偶然成功续期')
        return row, 'remove', None
    except Exception as exc:
        if type(exc).__name__ == 'PublicDNSUnconfirmed':
            row.update(action='speed_deferred',classification='pending',phase='dns',
                       error='PublicDNSUnconfirmed',reason='公开解析归属或有效记录未完成交叉确认，暂不发布、撤回或续期')
            return row, 'keep', None
        row.update(action='probe_error_keep_previous_until_expiry', classification='pending',
                   phase='dns' if 'public_dns' not in row else 'probe', error=type(exc).__name__,
                   reason='公开 DNS 查询失败' if 'public_dns' not in row else '本次对照未完成，不发布或续期')
        return row, 'keep', None
    finally:
        row['duration_ms'] = round((time.monotonic() - started) * 1000)


def allowed(ip, networks):
    try:
        address = ipaddress.ip_address(ip)
        return address.version == 4 and address.is_global and any(address in net for net in networks)
    except (ValueError, TypeError):
        return False
