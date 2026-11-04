"""Shared site registry validation for the UI, probe worker and DNS guard."""
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

MAX_SITES = 128
MAX_DOMAINS = 256
VERSION = '1.10'


class RegistryError(ValueError):
    """可安全展示给用户的校验错误：message 是中文原因，不包含用户输入的原文。"""


def _safe(value):
    """回显用户输入前截断，避免把长字符串（可能含密钥）写进回执或日志。"""
    text = str(value)
    return text[:40] + '…' if len(text) > 40 else text


def domain(value):
    if not isinstance(value, str):
        raise RegistryError('域名格式不正确')
    value = value.strip().lower()
    if '://' in value:
        p = urlsplit(value)
        if p.username or p.password:
            raise RegistryError('地址里含有账号或密钥，本工具只保存主机名，请勿粘贴这类链接')
        if p.scheme not in ['https', 'http']:
            raise RegistryError('只支持 http 或 https 地址')
        if p.port:
            raise RegistryError('只填写主机名即可，不要带端口号')
        if p.query or p.fragment:
            raise RegistryError('请只填写域名，不要带查询参数（可能是 passkey 等密钥）')
        if p.path not in ['', '/']:
            raise RegistryError('请只填写主机名，不要带汇报路径（例如 /announce）')
        value = p.hostname or ''
    elif re.search(r'[/?#]', value):
        # 没有协议头但带了路径/查询，同样属于“贴了完整汇报地址”。
        raise RegistryError('请只填写主机名，不要带路径或查询参数（例如 /announce?passkey=…）')
    value = value.rstrip('.')
    if is_excluded(value):
        raise RegistryError('该站点命中排除规则，不加入检测或优选')
    # 形如 IP 的输入先单独识别，给出“不要填 IP”的明确提示。
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise RegistryError('请填写域名，不要填写 IP 地址')
    # 纯数字的点分写法（如 192.0.2.10 已在上一步拦住），这里拦其它明显不是域名的形式。
    if ':' in value:
        raise RegistryError('请填写域名，不要填写 IPv6 地址或带端口的主机')
    if not value or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.[a-z]{2,24}', value) or '..' in value:
        raise RegistryError('“{}”不是有效的公开域名，请填写类似 tracker.example.org 的主机名'.format(_safe(value)))
    if any(not x or len(x) > 63 or x.startswith('-') or x.endswith('-') for x in value.split('.')):
        raise RegistryError('“{}”的某一段格式不正确（每段 1-63 字符，不能以 - 开头或结尾）'.format(_safe(value)))
    if value.endswith(('.local', '.internal', '.invalid', '.test', '.example', '.localhost', '.home', '.lan')):
        raise RegistryError('不检测局域网或保留域名')
    if not value.split('.')[-1].isalpha():
        raise RegistryError('域名后缀不正确，请填写完整的公开域名')
    return value


def domain_list(value, label='域名'):
    if isinstance(value, str):
        value = re.split(r'[\s,，;；]+', value.strip()) if value.strip() else []
    if not isinstance(value, list) or len(value) > 32:
        raise RegistryError('每类最多填写 32 个域名')
    result = []
    for item in value:
        try:
            result.append(domain(item))
        except RegistryError as exc:
            raise RegistryError('{}：{}'.format(label, exc)) from None
    return list(dict.fromkeys(result))


def validate_site(value):
    if not isinstance(value, dict):
        raise RegistryError('站点资料格式不正确')
    name = value.get('name', '')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or any(ord(c) < 32 for c in name):
        raise RegistryError('站点名称需要 1 至 40 个字')
    if is_excluded(name):
        raise RegistryError('该站点命中排除规则，不加入检测或优选')
    websites = domain_list(value.get('website_domains', []), '站点首页')
    trackers = domain_list(value.get('tracker_domains', []), 'Tracker')
    if not websites and not trackers:
        raise RegistryError('至少填写一个站点首页域名或 Tracker 域名')
    if not isinstance(value.get('enabled', True), bool):
        raise RegistryError('启用状态格式不正确')
    return {'name': name.strip(), 'website_domains': websites, 'tracker_domains': trackers,
            'enabled': value.get('enabled', True)}


def duplicate_domains(registry):
    """返回被**多个不同站点**同时声明的域名（同一站点内首页+Tracker 重复不算）。

    重要：同一个域名在**同一个站点**里既作为站点首页又作为 Tracker 是完全正常的
    （例如 station.example.org 这种站点用自己的域名做汇报地址）。
    只有跨站点的重复才是真冲突——那不可能是同一个站点的两条记录。

    历史教训：V1.4 首版把"同一站点的首页+Tracker"也判为重复，
    跨站点检查应保留，同站点角色重复应去重。
    """
    owners = {}
    for s in registry.get('sites', []):
        if s.get('removed'):
            continue
        site = s.get('id') or s.get('name')
        for d in set(s.get('website_domains', [])) | set(s.get('tracker_domains', [])):
            owners.setdefault(d, set()).add(site)
    names = {s.get('id') or s.get('name'): s.get('name') or s.get('id') for s in registry.get('sites', [])}
    return {d: sorted(names.get(owner, owner) for owner in sites)
            for d, sites in owners.items() if len(sites) > 1}


def load_registry(path, fallback):
    path = Path(path)
    if not path.exists():
        return {'version': 1, 'revision': 0, 'sites': [
            {'id': 'legacy-' + str(i), 'name': d, 'enabled': True, 'removed': False,
             'website_domains': [], 'tracker_domains': [domain(d)], 'sources': ['原白名单']}
            for i, d in enumerate(fallback)]}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('revision'), int) or not isinstance(value.get('sites'), list):
        msg = '站点清单暂时不可用'
        raise RegistryError(msg)
    if len(value['sites']) > MAX_SITES * 4:
        msg = '站点记录数量超过限制'
        raise RegistryError(msg)
    active, ids = [], set()
    for s in value['sites']:
        if not isinstance(s, dict) or not re.fullmatch(r'[a-z0-9-]{1,80}', str(s.get('id', ''))) or s['id'] in ids:
            msg = '站点标识格式不正确'
            raise RegistryError(msg)
        ids.add(s['id'])
        if s.get('removed'):
            active.append(s)
            continue
        clean = validate_site(s)
        active.append({**s, **clean})
    visible = [s for s in active if not s.get('removed')]
    if len(visible) > MAX_SITES or len({d for s in visible for d in s['website_domains'] + s['tracker_domains']}) > MAX_DOMAINS:
        msg = '站点或域名数量超过限制'
        raise RegistryError(msg)
    return {**value, 'sites': active}


def domain_index(registry, enabled_only=False):
    result = {}
    for s in registry['sites']:
        if s.get('removed') or (enabled_only and not s['enabled']):
            continue
        for role, key in [('website', 'website_domains'), ('tracker', 'tracker_domains')]:
            for d in s[key]:
                row = result.setdefault(d, {'site_id': s['id'], 'site_name': s['name'], 'roles': [], 'enabled': s['enabled']})
                if role not in row['roles']:
                    row['roles'].append(role)
                row['enabled'] = row['enabled'] or s['enabled']
    return result


def is_excluded(value):
    import os
    return any(word.strip().lower() in str(value).lower() for word in os.environ.get("EXCLUDED_KEYWORDS", "").split(",") if word.strip())
