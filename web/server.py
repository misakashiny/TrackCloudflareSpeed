"""LAN-only site manager: stores validated public domains and queues bounded checks."""
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, '/app')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from site_registry import (load_registry, domain_index, validate_site, duplicate_domains,
                           RegistryError, MAX_SITES, MAX_DOMAINS, VERSION)
from applog import LogFile, summary as log_summary
from diagnostics import build_diagnostic_zip
import updater
import speed_engine
import policy_model
import bottleneck
import acceptance
import site_health
import benefit_reports

updater.CURRENT_VERSION = VERSION

STATE = Path(os.environ.get('UI_STATE', '/state'))
CONTROL = Path(os.environ.get('UI_CONTROL', '/control'))
SETTINGS = Path(os.environ.get('UI_SETTINGS', '/settings/settings.json'))
CONFIG_DIR = SETTINGS.parent
DNS = Path(os.environ.get('UI_DNS', '/published/active.hosts'))
WEB = Path(__file__).parent
BIND = os.environ.get('UI_BIND', '127.0.0.1')
PORT = int(os.environ.get('UI_PORT', '9078'))
HOST = os.environ.get('UI_PUBLIC_HOST', f'{BIND}:{PORT}')
NETWORKS = [ipaddress.ip_network(x.strip()) for x in os.environ.get('UI_CLIENT_NETWORK', '127.0.0.0/8').split(',')]
SECRET = secrets.token_bytes(32)
LOCK = threading.Lock()


def read(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def published():
    result = {}
    try:
        for line in DNS.read_text().splitlines():
            cols = line.split()
            if len(cols) == 2 and not cols[0].startswith('#'):
                result[cols[1]] = cols[0]
    except OSError:
        pass
    return result


def safe_report(report, domains, compact=False):
    # Project reports contain no credentials. Still expose exact allowlisted keys only.
    out = {key: report.get(key) for key in ['started_at', 'finished_at', 'active_overrides',
                                            'duration_seconds', 'summary', 'version', 'dns_sources', 'ipv6_baseline_enabled','runtime_policy']}
    out['domains'] = {}
    for domain in domains:
        row = report.get('domains', {}).get(domain)
        if isinstance(row, dict):
            # 新增 reason / phase / baseline_failure / budget_exhausted：
            # 让前端能解释“为什么没通过”，而不是只显示一个异常类名。
            out['domains'][domain] = {k: row[k] for k in ['action', 'error', 'phase', 'reason', 'public_dns',
                'baseline', 'baseline_healthy', 'baseline_failure', 'selected_ip', 'renewed_ip',
                'ranked_candidates', 'candidate_probes', 'classification', 'budget_exhausted',
                'duration_ms', 'tls_ok', 'http_status', 'speed', 'baseline_dns', 'baseline_dns6'] if k in row}
            if compact:
                out['domains'][domain] = {k: row[k] for k in ['action','classification','reason','phase',
                    'duration_ms','baseline_healthy','selected_ip','renewed_ip'] if k in row}
    out['active_overrides'] = {d: ip for d, ip in (out['active_overrides'] or {}).items() if d in domains}
    if not policy_model.verified(out.get('runtime_policy')):out['runtime_policy']=None
    return out


# 视为“可达”的动作（不含判定为失败或异常的那些）。
HEALTHY_ACTIONS = {'original_path_healthy', 'origin_only', 'validated_override',
                   'speed_override', 'speed_keep_verified', 'speed_return_original'}


def healthy_row(row):
    if row.get('action') in HEALTHY_ACTIONS:
        return True
    if row.get('action') in ('speed_shadow', 'speed_wait_confirmation'):
        return bool(row.get('baseline_healthy'))
    return False


def domain_trends(history, domains, rounds=10):
    """按域名汇总最近 N 轮表现。

    单轮结果看不出“一直稳定偶尔抖”和“反复失败”的区别，趋势可以。
    只用已有的历史文件，不额外探测。
    """
    recent = history[:rounds]
    trends = {}
    for d in domains:
        runs = [(r, (r.get('domains') or {}).get(d)) for r in recent]
        runs = [(r, row) for r, row in runs if isinstance(row, dict)]
        if not runs:
            trends[d] = {'rounds': 0}
            continue
        healthy = sum(1 for _, row in runs if healthy_row(row))
        durations = [row.get('duration_ms') for _, row in runs if isinstance(row.get('duration_ms'), (int, float))]
        transitions = 0
        previous_ip = None
        for _, row in reversed(runs):  # 从旧到新数切换
            current = row.get('selected_ip') or row.get('renewed_ip')
            if previous_ip is not None and current != previous_ip:
                transitions += 1
            previous_ip = current
        reasons = {}
        for _, row in runs:
            if row.get('reason'):
                reasons[row['reason']] = reasons.get(row['reason'], 0) + 1
        top_reason = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else None
        trends[d] = {
            'rounds': len(runs),
            'healthy': healthy,
            'rate': round(healthy / len(runs), 2),
            'avg_ms': round(sum(durations) / len(durations)) if durations else None,
            'max_ms': max(durations) if durations else None,
            'transitions': transitions,
            'last_action': runs[0][1].get('action'),
            'last_at': runs[0][0].get('finished_at'),
            'top_reason': top_reason,
            'streak_bad': _bad_streak(runs),
        }
    return trends


def _bad_streak(runs):
    """从最新一轮往回数，连续多少轮不健康。用于提示“可能该换域名了”。"""
    streak = 0
    for _, row in runs:
        if healthy_row(row):
            break
        streak += 1
    return streak


def slowest_domains(latest, limit=10):
    """本轮耗时排行：谁在拖慢整轮检测。"""
    rows = []
    for d, row in (latest.get('domains') or {}).items():
        if isinstance(row, dict) and isinstance(row.get('duration_ms'), (int, float)):
            rows.append({'domain': d, 'ms': row['duration_ms'],
                         'action': row.get('action'), 'phase': row.get('phase'),
                         'reason': row.get('reason')})
    rows.sort(key=lambda r: -r['ms'])
    return rows[:limit]


def build_timeline(history, changes, limit=60):
    """把配置变更和检测轮次放进同一条时间线，便于回答“改了之后发生了什么”。"""
    events = []
    for r in history[:40]:
        summary = r.get('summary') or {}
        actions = summary.get('actions') or {}
        events.append({'at': r.get('finished_at'), 'kind': 'run',
                       'title': '完成一轮检测',
                       'detail': '用时 {} 秒；{} 个域名；切换 {} 个'.format(
                           r.get('duration_seconds', '—'),
                           len(r.get('domains') or {}),
                           actions.get('validated_override', 0))})
    for c in changes:
        label = '保存站点' if c.get('operation') == 'save' else '移除站点' if c.get('operation') == 'remove' else '站点操作'
        if c.get('result') != 'ok':
            label += '（被拒绝）'
        events.append({'at': c.get('at'), 'kind': 'change', 'title': label,
                       'detail': (c.get('name') or '') + (('：' + c['reason']) if c.get('reason') else '')})
    events = [e for e in events if e.get('at')]
    events.sort(key=lambda e: e['at'], reverse=True)
    return events[:limit]


def recent_changes(limit=20):
    """最近的操作审计，含被拒绝的请求，供“站点管理”页展示。"""
    path = CONTROL / 'site-changes.jsonl'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[-limit * 3:]
    except OSError:
        return []
    records = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append({k: item.get(k) for k in ['at', 'operation', 'result', 'name', 'revision', 'reason']})
        if len(records) >= limit:
            break
    return records


def capacity_assessment_status():
    raw = read(STATE / 'capacity/assessment.json', {})
    value = {key: raw[key] for key in ('id', 'status', 'reason', 'created_at', 'updated_at', 'deadline',
             'next_check_at', 'finished_at', 'sample_count', 'observed_path_lower_bound_Bps',
             'max_payload_bytes', 'max_seconds_per_upload', 'check_interval_seconds') if key in raw}
    value['stoppable'] = raw.get('status') in ('waiting', 'running')
    request = read(CONTROL / 'upload-assessment-stop.json', {})
    value['stop_requested'] = bool(raw.get('id') and request.get('id') == raw['id'] and request.get('op') == 'stop')
    return value


def snapshot():
    cfg = read(SETTINGS, {})
    registry = load_registry(CONTROL / 'sites.json', cfg.get('domains', []))
    indexed = domain_index(registry)
    domains = list(indexed)
    beat = read(STATE / 'heartbeat.json', {})
    reports = []
    for p in sorted((STATE / 'history').glob('*.json'), reverse=True)[:96]:
        value = read(p, {})
        if value.get('finished_at'):
            reports.append(safe_report(value, domains, compact=True))
    raw_latest=read(STATE/'last-run.json',{})
    latest = safe_report(raw_latest, domains)
    records = {r['started_at']: r for r in reports}
    if latest['finished_at']:
        records[latest['started_at']] = latest
    history = sorted(records.values(), key=lambda r: r['started_at'], reverse=True)[:96]
    dns = {d: ip for d, ip in published().items() if d in domains}
    request = read(CONTROL / 'request.json', {})
    try:
        history_files = len(list((STATE / 'history').glob('*.json')))
    except OSError:
        history_files = 0
    changes = recent_changes()
    speed_control = read(CONTROL / 'speed-policy.json', {})
    transfer = read(STATE / 'transfer/latest.json', {})
    raw_acceptance = read(STATE / 'acceptance/latest.json', {})
    reports = read(STATE / 'reports/latest.json', {})
    # Judge freshness after reading the inputs. A coordinator update that lands
    # while history is being assembled must not appear to come from the future.
    now = int(time.time())
    healthy = 0 <= now - beat.get('at', 0) < 120
    pending = bool(request and 0 <= now - request.get('requested_at', 0) <= 300)
    runtime_policy=policy_model.execution_view(cfg,speed_control,registry['revision'],VERSION,now,
        read(STATE/'active-policy.json',{}),raw_latest,bool(beat.get('probing')) and healthy)
    speed=runtime_policy['desired']['speed'] if runtime_policy['desired'] else {'mode':'unavailable'}
    transfer['fresh'] = bool(0 <= now - transfer.get('at', 0) < 180)
    acceptance_status=acceptance.safe_status(raw_acceptance,now)
    reports['fresh']=bool(0<=now-reports.get('at',0)<180)
    native_budget=native_budget_status();budget_acceptance=budget_acceptance_status()
    return {'now': now, 'worker_healthy': healthy, 'heartbeat_at': beat.get('at'),
            'speed_policy': speed, 'speed_revision': speed_control.get('revision', 0), 'transfer': transfer,'runtime_policy':runtime_policy,
            'bottleneck':bottleneck.explain(transfer,now,counter_points=read(STATE/'reports/transfer-points.json',[])),
            'capacity_assessment':capacity_assessment_status(), 'native_budget':native_budget,
            'budget_acceptance':budget_acceptance,
            'acceptance':acceptance_status,'acceptance_history':read(STATE/'acceptance/history.json',[]),
            'reports':reports,'report_error':read(STATE/'reports/error.json',{}),
            'site_health':site_health.build(registry['sites'],raw_latest,transfer,speed,dns,now),
            'alerts':benefit_reports.alerts(raw_latest,transfer,acceptance_status,{},now,native_budget=native_budget,budget_acceptance=budget_acceptance),
            'candidate_pool': read(STATE / 'candidate-cache.json', {}),
            'probing': bool(beat.get('probing')) and healthy, 'next_run': beat.get('next_run'),
            'pending': pending, 'manual': read(STATE / 'manual-request.json', {}),
            'domains': domains, 'domain_meta': indexed, 'sites': [s for s in registry['sites'] if not s.get('removed')],
            'registry_revision': registry['revision'], 'progress': read(STATE / 'progress.json', {}),
            'limits': {'sites': MAX_SITES, 'domains': MAX_DOMAINS},
            'published': dns, 'entries': {d: v for d, v in read(STATE / 'entries.json', {}).items() if d in domains},
            'latest': latest, 'history': history, 'history_files': history_files,
            'changes': changes,
            # 三项分析：只看已有历史，不额外探测，不改变任何判定。
            'trends': domain_trends(history, domains),
            'slowest': slowest_domains(latest),
            'timeline': build_timeline(history, changes),
            'version': VERSION,
            'policy': {k: cfg.get(k) for k in ['interval_seconds', 'max_age_seconds', 'candidate_limit',
                                               'candidate_budget_seconds', 'baseline_addresses', 'history_rounds']},
            'last_requested_at': read(CONTROL / 'last-request.json', {}).get('requested_at', 0)}


def budget_acceptance_status():
    raw = read(STATE / 'acceptance/budget.json', {})
    keys = ('id', 'status', 'scope', 'created_at', 'deadline', 'updated_at', 'next_check_at', 'reason', 'budget',
            'evidence', 'lease_id', 'lease_expires_at', 'restored_at', 'finished_at', 'result', 'completed_stages',
            'recovery_check_until', 'next_recovery_at')
    value = {key: raw[key] for key in keys if key in raw}
    ident = raw.get('id', '')
    valid_id = isinstance(ident, str) and len(ident) == 32 and all(c in '0123456789abcdef' for c in ident)
    value['fresh'] = 0 <= time.time() - raw.get('updated_at', 0) < 180
    value['stoppable'] = bool(valid_id and raw.get('status') in acceptance.ACTIVE)
    pending = read(CONTROL / 'budget-acceptance-stop.json', {})
    value['stop_requested'] = value['stoppable'] and pending.get('op') == 'stop' and pending.get('id') == ident
    return value


def native_budget_status():
    now = int(time.time() * 1000)
    records = [read(STATE / 'native-budget' / name, {}) for name in ('latest.json', 'functional.json')]
    records = [row for row in records if isinstance(row, dict) and isinstance(row.get('observedAt'), (int, float))
               and 0 <= now - row['observedAt']]
    if not records:
        return {'state': 'unavailable', 'fresh': False, 'enforced': False, 'globalEnforced': False, 'stoppable': False}
    raw = max(records, key=lambda row: row['observedAt'])
    keys = ('id', 'state', 'scope', 'purpose', 'budget', 'started', 'expires', 'appliedAt', 'lastConfirmedAt',
            'restoredAt', 'stopRequestedAt', 'error', 'nextRecoveryAt', 'observedAt')
    value = {key: raw[key] for key in keys if key in raw}
    ident = value.get('id', '')
    valid_id = isinstance(ident, str) and len(ident) == 43 and ident.startswith('nas-budget-') and all(c in '0123456789abcdef' for c in ident[11:])
    confirmed = raw.get('lastConfirmedAt', 0)
    proof_fresh = isinstance(confirmed, (int, float)) and 0 <= now - confirmed < 30000
    fresh = now - raw['observedAt'] < 180000
    value.update(fresh=fresh, enforced=bool(raw.get('enforced') and fresh and proof_fresh and raw.get('state') == 'active'),
                 stoppable=bool(valid_id and raw.get('state') not in ('restored', 'none')))
    value['globalEnforced'] = value['enforced'] and raw.get('scope') == 'all'
    value['clients'] = [{key: row[key] for key in ('id', 'name', 'originalUploadBps', 'lastConfirmedUploadBps') if key in row}
                        for row in raw.get('clients', [])[:3] if isinstance(row, dict)]
    request = read(CONTROL / 'native-budget-stop.json', {})
    value['stop_requested'] = bool(value['stoppable'] and request.get('id') == ident and request.get('op') == 'stop')
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = 'NASOptimizerUI/' + VERSION

    def log_message(self, *_):
        pass

    def allowed(self):
        try:
            address = ipaddress.ip_address(self.client_address[0])
            return any(address in net for net in NETWORKS) or address.is_loopback
        except ValueError:
            return False

    def csrf_cookie_ok(self, expected):
        """精确比对 CSRF Cookie。

        旧实现用 `"optimizer_csrf=<token>" in Cookie.split('; ')` 做子串判断，
        `other=1; optimizer_csrf=<token>` 这类组合会被误判为通过。这里改成解析成字典后逐项等值比较。
        """
        for part in self.headers.get('Cookie', '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == 'optimizer_csrf' and hmac.compare_digest(value, expected):
                return True
        return False

    def send(self, code, body, content_type='application/json; charset=utf-8', headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        for key, value in {'Content-Type': content_type, 'Content-Length': str(len(body)),
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'", **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed() or self.headers.get('Host') != HOST:
            return self.send(403, {'error': '仅允许从 NAS 局域网地址访问'})
        path = urlsplit(self.path).path
        if path == '/healthz':
            return self.send(200, {'ok': True})
        if path in ('/api/log', '/api/logs'):
            return self.logs()
        if path == '/api/diagnostics':
            return self.diagnostics()
        if path == '/api/update':
            return self.update_status()
        if path == '/api/status':
            try:
                data = snapshot()
            except (ValueError, KeyError, TypeError):
                return self.send(503, {'error': '站点清单暂时不可用，请检查配置后刷新'})
            token = hmac.new(SECRET, self.client_address[0].encode(), hashlib.sha256).hexdigest()
            data['portable'] = True
            data['csrf'] = token
            return self.send(200, data, headers={'Set-Cookie': f'optimizer_csrf={token}; HttpOnly; SameSite=Strict; Path=/'})
        if path not in ['/', '/index.html', '/app.js', '/speed.js', '/acceptance.js', '/style.css', '/Geist.woff2', '/favicon.svg']:
            return self.send(404, {'error': '页面不存在'})
        p = WEB / ('index.html' if path == '/' else path[1:])
        try:
            self.send(200, p.read_bytes(), mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
        except OSError:
            self.send(404, {'error': '文件不存在'})

    # ---- 日志 ----
    def logs(self):
        query = urlsplit(self.path).query
        params = dict(part.split('=', 1) for part in query.split('&') if '=' in part)
        from urllib.parse import unquote_plus
        def value(key, default=''):
            return unquote_plus(params.get(key, default))
        try:
            limit = min(500, max(1, int(value('limit', '200'))))
        except ValueError:
            limit = 200
        log = LogFile(STATE / 'logs')
        records = log.tail(limit=limit, level=value('level'), keyword=value('q'), code=value('code'))
        if value('download') == '1':
            # 导出为可读文本：给排查的人直接看，也给粘贴到聊天/issue 用。
            lines = ['# NAS 域名优选 日志导出',
                     '# 时间: {}  版本: {}  共 {} 条'.format(
                         time.strftime('%Y-%m-%d %H:%M:%S'), VERSION, len(records)), '']
            for item in records:
                stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(item.get('at', 0)))
                lines.append('{} [{}] {} {}'.format(
                    stamp, str(item.get('level', 'info')).upper(), item.get('code', ''), item.get('message', '')))
                if item.get('detail'):
                    lines.append('    ' + json.dumps(item['detail'], ensure_ascii=False))
            payload = '\n'.join(lines).encode('utf-8')
            name = 'logs-{}.txt'.format(time.strftime('%Y%m%d-%H%M%S'))
            return self.send(200, payload, content_type='text/plain; charset=utf-8',
                             headers={'Content-Disposition': 'attachment; filename="{}"'.format(name)})
        return self.send(200, {'records': records, 'summary': log_summary(records),
                               'count': len(records), 'version': VERSION})

    # ---- 诊断包 ----
    def diagnostics(self):
        name, payload = build_diagnostic_zip(
            state=STATE, control=CONTROL, settings=SETTINGS,
            logfile=LogFile(STATE / 'logs'), version=VERSION)
        self.audit('diagnostics', result='ok', reason='导出了诊断包')
        return self.send(200, payload, content_type='application/zip',
                         headers={'Content-Disposition': 'attachment; filename="{}"'.format(name)})

    # ---- 版本更新（检查 + 下载校验，重启由用户在 NAS 界面完成）----
    def update_status(self):
        return self.send(200, updater.status(CONFIG_DIR))

    def download_update(self):
        """下载新版本到可写目录并校验。绝不自动替换运行中文件、绝不自动重启。"""
        try:
            length = int(self.headers.get('Content-Length', '0'))
            payload = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else {}
        except (ValueError, OSError):
            payload = {}
        expected = payload.get('sha256') if isinstance(payload, dict) else None
        # control/ 是 dashboard 容器唯一可写挂载（/state 是只读），下载放这里。
        result = updater.download(CONFIG_DIR, CONTROL / 'updates', expected_sha256=expected)
        self.audit('update', result='ok' if result.get('ok') else 'rejected',
                   reason=(result.get('message') or '')[:120])
        log = LogFile(STATE / 'logs')
        log.write('info' if result.get('ok') else 'warn', 'UPDATE_DOWNLOAD', result.get('message') or '',
                  version=result.get('version'), sha256=result.get('sha256'))
        return self.send(200 if result.get('ok') else 400, result)

    def do_POST(self):
        if not self.allowed() or self.headers.get('Host') != HOST or self.headers.get('Origin') != 'http://' + HOST:
            return self.send(403, {'error': '请求来源不匹配，请从管理页面操作'})
        expected = hmac.new(SECRET, self.client_address[0].encode(), hashlib.sha256).hexdigest()
        header_ok = hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), expected)
        if not header_ok or not self.csrf_cookie_ok(expected):
            return self.send(403, {'error': '页面已过期，请刷新后重试'})
        if self.path == '/api/sites':
            return self.save_site()
        if self.path == '/api/speed':
            return self.save_speed_policy()
        if self.path in ('/api/capacity-assessment','/api/acceptance','/api/native-budget','/api/budget-acceptance'):
            return self.send(501, {'error': '此独立部署未配置下载器试验执行器；不会修改下载器'})
        if self.path == '/api/capacity-assessment':
            return self.stop_capacity_assessment()
        if self.path == '/api/acceptance':
            return self.acceptance_request()
        if self.path == '/api/native-budget':
            return self.stop_native_budget()
        if self.path == '/api/budget-acceptance':
            return self.stop_budget_acceptance()
        if self.path == '/api/update':
            return self.download_update()
        if self.path != '/api/probe':
            return self.send(404, {'error': '操作不存在'})
        if self.headers.get('Content-Length', '0') != '0':
            return self.send(400, {'error': '检测不接受自定义域名或其他参数'})
        with LOCK:
            data = snapshot()
            if not data['worker_healthy']:
                return self.send(503, {'error': '检测服务失联，请先检查容器状态'})
            if data['probing'] or data['pending']:
                return self.send(409, {'error': '已有检测正在进行，请等待结果'})
            if data['now'] - data['last_requested_at'] < 60:
                return self.send(429, {'error': '检测间隔至少 60 秒，请稍后重试'})
            request = {'id': secrets.token_hex(16), 'requested_at': data['now']}
            CONTROL.mkdir(exist_ok=True)
            temp = CONTROL / ('.request-' + request['id'])
            temp.write_text(json.dumps(request))
            os.chmod(temp, 0o600)
            os.replace(temp, CONTROL / 'request.json')
            (CONTROL / 'last-request.json').write_text(json.dumps(request))
        self.send(202, {'message': '已提交检测，通常在 15 秒内开始', 'id': request['id']})

    def save_speed_policy(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 8192:
                raise ValueError('提速设置大小不正确')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) - {'mode', 'allow_domains', 'site_modes','revision', 'policy_revision'}:
                raise ValueError('仅接受提速模式与灰度域名')
            with LOCK:
                cfg = read(SETTINGS, {})
                registry = load_registry(CONTROL / 'sites.json', cfg.get('domains', []))
                if payload.get('revision') != registry['revision']:
                    return self.send(409, {'error': '站点清单已变化，请刷新后重试'})
                old = read(CONTROL / 'speed-policy.json', {})
                if payload.get('policy_revision') != old.get('revision', 0):
                    return self.send(409, {'error': '提速模式已在别处更新，请刷新后重试'})
                p = speed_engine.policy(cfg, {**old,**payload})
                ids={s['id'] for s in registry['sites'] if not s.get('removed')}
                if not set(p['site_modes']).issubset(ids):raise ValueError('站点策略必须来自当前站点清单')
                allowed = set(domain_index(registry, enabled_only=True))
                if not set(p['allow_domains']).issubset(allowed):
                    raise ValueError('灰度域名必须来自当前已启用的站点清单')
                CONTROL.mkdir(exist_ok=True)
                temporary = CONTROL / ('.speed-' + secrets.token_hex(8))
                try:
                    temporary.write_text(json.dumps({'mode': p['mode'], 'allow_domains': p['allow_domains'],'site_modes':p['site_modes'],
                                                     'updated_at': int(time.time()), 'revision': old.get('revision', 0) + 1}), encoding='utf-8')
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, CONTROL / 'speed-policy.json')
                finally:
                    temporary.unlink(missing_ok=True)
                self.audit('speed_policy', reason='提速模式改为 ' + p['mode'], revision=registry['revision'])
            return self.send(200, {'message': '提速模式已保存，下轮检测生效', 'speed_policy': p})
        except (ValueError, TypeError, KeyError) as exc:
            return self.send(400, {'error': str(exc)[:160]})

    def stop_capacity_assessment(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 256:
                raise ValueError('停止请求大小不正确')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {'op', 'id'} or payload['op'] != 'stop':
                raise ValueError('这里只接受停止当前测量')
            with LOCK:
                current = capacity_assessment_status()
                if not current.get('stoppable') or payload['id'] != current.get('id'):
                    return self.send(409, {'error': '当前没有对应的可停止测量，请刷新状态'})
                value = {'id': current['id'], 'op': 'stop', 'requested_at': int(time.time())}
                temp = CONTROL / ('.upload-stop-' + secrets.token_hex(8))
                temp.write_text(json.dumps(value))
                os.chmod(temp, 0o600)
                os.replace(temp, CONTROL / 'upload-assessment-stop.json')
                self.audit('capacity-assessment', reason='已请求停止有界上行测量')
            return self.send(202, {'message': '已请求停止；等待中的测量将在下次检查确认，执行中的测量会终止本轮'})
        except (ValueError, TypeError, KeyError) as error:
            return self.send(400, {'error': str(error)[:160]})

    def stop_budget_acceptance(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 160:
                raise ValueError('停止请求大小不正确')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {'op', 'id'} or payload['op'] != 'stop':
                raise ValueError('这里只接受停止当前预算对照排期')
            with LOCK:
                current = budget_acceptance_status()
                if not current.get('stoppable') or payload['id'] != current.get('id'):
                    return self.send(409, {'error': '当前没有对应的可停止预算排期，请刷新状态'})
                value = {'id': current['id'], 'op': 'stop', 'requested_at': int(time.time())}
                temporary = CONTROL / ('.budget-acceptance-stop-' + secrets.token_hex(8))
                temporary.write_text(json.dumps(value))
                os.chmod(temporary, 0o600)
                os.replace(temporary, CONTROL / 'budget-acceptance-stop.json')
                self.audit('budget-acceptance', reason='已请求停止预算对照排期并恢复原额度')
            return self.send(202, {'message': '已请求停止排期；若已应用临时额度，会先恢复并回读确认'})
        except (ValueError, TypeError, KeyError) as error:
            return self.send(400, {'error': str(error)[:160]})

    def stop_native_budget(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 160:
                raise ValueError('停止请求大小不正确')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {'op', 'id'} or payload['op'] != 'stop':
                raise ValueError('这里只接受停止当前预算试行')
            with LOCK:
                current = native_budget_status()
                if not current.get('stoppable') or payload['id'] != current.get('id'):
                    return self.send(409, {'error': '当前没有对应的可停止预算试行，请刷新状态'})
                value = {'id': current['id'], 'op': 'stop', 'requested_at': int(time.time())}
                temporary = CONTROL / ('.budget-stop-' + secrets.token_hex(8))
                temporary.write_text(json.dumps(value))
                os.chmod(temporary, 0o600)
                os.replace(temporary, CONTROL / 'native-budget-stop.json')
                self.audit('native-budget', reason='已请求停止预算试行并恢复原额度')
            return self.send(202, {'message': '已请求停止；执行服务回读原额度后才会确认恢复'})
        except (ValueError, TypeError, KeyError) as error:
            return self.send(400, {'error': str(error)[:160]})

    def acceptance_request(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 1<=length<=512:raise ValueError('验收请求大小不正确')
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict) or set(payload)-{'op','mode','id'}:raise ValueError('验收请求不接受目标、命令或限速参数')
            with LOCK:
                now=int(time.time());current=acceptance.safe_status(read(STATE/'acceptance/latest.json',{}),now)
                pending=read(CONTROL/'acceptance-request.json',{})
                if payload.get('op')=='start':
                    if current.get('stoppable') or current.get('status')=='recovery_blocked' or pending.get('op')=='start' and pending.get('id')!=current.get('id') and 0<=now-pending.get('requested_at',0)<300:
                        return self.send(409,{'error':'已有验收在等待、执行或恢复，请先查看或停止现有验收'})
                    if not snapshot()['transfer']['fresh']:return self.send(503,{'error':'传输观测未更新，暂不能开始验收'})
                    value={'id':secrets.token_hex(16),'op':'start','mode':payload.get('mode'),'requested_at':now}
                elif payload.get('op')=='stop':
                    if payload.get('id')!=current.get('id') or not current.get('stoppable'):return self.send(409,{'error':'当前没有对应的可停止验收，请刷新状态'})
                    value={'id':current['id'],'op':'stop','requested_at':now}
                else:raise ValueError('请选择开始或停止验收')
                acceptance.request(value,now)
                temp=CONTROL/('.acceptance-'+secrets.token_hex(8));temp.write_text(json.dumps(value));os.chmod(temp,0o600);os.replace(temp,CONTROL/'acceptance-request.json')
                self.audit('acceptance',reason='已提交'+value['op'])
            return self.send(202,{'id':value['id'],'message':'已提交，验收服务将在下一次检查处理；停止会先恢复并回读原额度'})
        except (ValueError,TypeError,KeyError) as exc:return self.send(400,{'error':str(exc)[:160]})

    def audit(self, operation, site=None, revision=None, result='ok', reason=None, site_id=None):
        """所有站点改动都留痕，包括被拒绝的请求。

        旧实现只在成功路径写审计，失败的保存完全不留痕迹，事后无法追溯。
        这里只记录操作类型、站点名、修订号和原因，不记录任何域名之外的输入原文。
        """
        record = {'at': int(time.time()), 'operation': operation, 'result': result,
                  'id': (site or {}).get('id') or site_id or '',
                  'name': (site or {}).get('name') or '',
                  'revision': revision}
        if reason:
            record['reason'] = str(reason)[:160]
        try:
            CONTROL.mkdir(exist_ok=True)
            with (CONTROL / 'site-changes.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError:
            pass  # 审计失败不应阻断用户操作
        # 同步写运行日志：审计是"谁改了什么"，日志是"系统此刻在做什么"，两者配合才好定位。
        level = 'info' if result == 'ok' else 'warn'
        try:
            LogFile(STATE / 'logs').write(level, 'SITE_' + str(operation or 'unknown').upper(),
                                          record.get('reason') or '{} {}'.format(operation, record['name']),
                                          site=record['name'], result=result, revision=revision)
        except Exception:
            pass

    def save_site(self):
        operation = 'save'
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 16384:
                self.audit(operation, result='rejected', reason='请求体大小不正确')
                return self.send(400, {'error': '站点资料大小不正确，请减少域名数量后重试'})
            try:
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                self.audit(operation, result='rejected', reason='请求体不是合法 JSON')
                return self.send(400, {'error': '站点资料格式不正确，请刷新页面后重试'})
            if not isinstance(payload, dict):
                raise RegistryError('站点资料格式不正确')
            operation = payload.get('operation', 'save')
            with LOCK:
                cfg = read(SETTINGS, {})
                registry = load_registry(CONTROL / 'sites.json', cfg.get('domains', []))
                if payload.get('revision') != registry['revision']:
                    self.audit(operation, site_id=payload.get('id'), revision=registry['revision'],
                               result='conflict', reason='修订号不一致（可能开了另一个页签）')
                    return self.send(409, {'error': '站点清单已在别处更新（当前修订号 {}），请刷新后重试'.format(registry['revision'])})
                if operation not in ['save', 'remove']:
                    raise RegistryError('不支持的站点操作：' + str(operation)[:20])
                site_id = payload.get('id')
                existing = next((s for s in registry['sites'] if s['id'] == site_id and not s.get('removed')), None)
                if site_id and existing is None:
                    self.audit(operation, site_id=site_id, revision=registry['revision'],
                               result='not_found', reason='站点不存在或已移除')
                    return self.send(404, {'error': '站点已不存在，请刷新页面'})
                if operation == 'remove':
                    if existing is None:
                        raise RegistryError('请选择要移除的站点')
                    existing['removed'] = True
                    saved = existing
                else:
                    clean = validate_site(payload)
                    hosts = set(clean['website_domains'] + clean['tracker_domains'])
                    for s in registry['sites']:
                        if s.get('removed') or s['id'] == site_id:
                            continue
                        clash = sorted(hosts.intersection(s['website_domains'] + s['tracker_domains']))
                        if clash:
                            # 明确告诉用户是哪个域名和哪个站点冲突，而不是笼统一句“格式不正确”。
                            raise RegistryError('域名 {} 已属于站点“{}”，请编辑那个站点而不是重复添加'.format(
                                '、'.join(clash[:3]), s['name']))
                    if existing is not None:
                        existing.update(clean)
                        saved = existing
                    else:
                        saved = {'id': 'manual-' + secrets.token_hex(8), **clean, 'sources': ['手动添加'], 'removed': False}
                        registry['sites'].append(saved)
                registry['revision'] += 1
                registry['updated_at'] = int(time.time())
                CONTROL.mkdir(exist_ok=True)
                temp = CONTROL / ('.sites-' + secrets.token_hex(8))
                try:
                    temp.write_text(json.dumps(registry, ensure_ascii=False), encoding='utf-8')
                    os.chmod(temp, 0o600)
                    # 写盘前用同一校验器回读，避免把不自洽的清单落盘。
                    load_registry(temp, [])
                    os.replace(temp, CONTROL / 'sites.json')
                finally:
                    if temp.exists():
                        temp.unlink()
                # Only this request file is writable by the page; the worker owns probing and DNS publication.
                if saved.get('enabled') and not saved.get('removed'):
                    request = {'id': secrets.token_hex(16), 'requested_at': int(time.time())}
                    temp = CONTROL / ('.request-' + request['id'])
                    temp.write_text(json.dumps(request)); os.chmod(temp, 0o600)
                    os.replace(temp, CONTROL / 'request.json')
                self.audit(operation, site=saved, revision=registry['revision'], result='ok')
            self.send(200, {'message': '站点已移除，下载器和文件不受影响' if operation == 'remove' else '站点已保存，已启用的域名将自动检测',
                            'id': saved['id'], 'revision': registry['revision']})
        except RegistryError as exc:
            # 精确原因直接回传：用户能立刻知道是哪一项、为什么不合格。
            self.audit(operation, revision=None, result='rejected', reason=str(exc))
            self.send(400, {'error': str(exc)})
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.audit(operation, result='rejected', reason=type(exc).__name__)
            self.send(400, {'error': '提交内容无法识别（{}），请刷新页面后重新填写'.format(type(exc).__name__)})


if __name__ == '__main__':
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
