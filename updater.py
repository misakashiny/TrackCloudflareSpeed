"""版本更新：检查新版本 → 下载 → 校验 → 落到待安装目录。

明确不做的事（安全边界）：
- 不自动重启容器：容器只读运行，自重启失败会把服务停掉；重启由用户在绿联界面完成。
- 不在校验失败时保留文件：SHA256 不匹配立即删除。
- 不把 token 写进日志或回执：只说明"是否配置了 token"。

配置放在 config/settings.json 的 "update" 段，例如：
    "update": {
      "enabled": true,
      "repo": "owner/name",
      "asset": "nas-domain-optimizer.tar.gz",
      "api_base": "https://api.github.com"
    }
仓库私有需要 token，从环境变量 GITHUB_TOKEN 读取，不落盘。
"""
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
import time

CURRENT_VERSION = None  # 由调用方（server）注入，避免循环导入


def _settings(config_dir):
    try:
        return json.loads((Path(config_dir) / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _cfg(config_dir):
    cfg = _settings(config_dir).get('update') or {}
    return {
        'enabled': bool(cfg.get('enabled')),
        'repo': str(cfg.get('repo') or '').strip(),
        'asset': str(cfg.get('asset') or 'nas-domain-optimizer.tar.gz').strip(),
        'api_base': str(cfg.get('api_base') or 'https://api.github.com').rstrip('/'),
    }


def _version_tuple(text):
    """"1.3.1" → (1, 3, 1)；非数字段按 0 处理，保证不因格式异常崩溃。"""
    parts = re.findall(r'\d+', str(text or ''))
    return tuple(int(p) for p in parts[:4]) or (0,)


def status(config_dir):
    """返回当前版本与仓库配置状态。未配置时明确说明，不伪装成可用。"""
    cfg = _cfg(config_dir)
    token_configured = bool(os.environ.get('GITHUB_TOKEN'))
    result = {
        'current': CURRENT_VERSION,
        'enabled': cfg['enabled'],
        'configured': bool(cfg['enabled'] and cfg['repo']),
        'repo': cfg['repo'] or None,
        'token_configured': token_configured,
        'state': 'idle',
    }
    if not cfg['enabled']:
        result['message'] = '未启用更新检查。在 settings.json 的 update 段打开 enabled 并填写 repo。'
        return result
    if not cfg['repo']:
        result['message'] = '尚未填写仓库地址（update.repo，格式 owner/name）。'
        return result
    try:
        release = _latest_release(cfg)
    except urllib.error.HTTPError as exc:
        result.update(state='error', message='查询 GitHub 失败：HTTP {}{}'.format(
            exc.code, '（私有仓可能需要 GITHUB_TOKEN）' if exc.code in (401, 403, 404) else ''))
        return result
    except Exception as exc:
        result.update(state='error', message='查询 GitHub 失败：{}'.format(type(exc).__name__))
        return result
    tag = release.get('tag_name') or ''
    result.update({
        'latest': tag,
        'published_at': release.get('published_at'),
        'notes_url': release.get('html_url'),
        'state': 'update_available' if _version_tuple(tag) > _version_tuple(CURRENT_VERSION) else 'up_to_date',
        'assets': [{'name': a.get('name'), 'size': a.get('size'),
                    'sha256': _asset_digest(a), 'url': a.get('browser_download_url')}
                   for a in release.get('assets') or []],
    })
    result['message'] = ('发现新版本 {}，可以下载。' if result['state'] == 'update_available'
                         else '已是最新版本。')
    return result


def _asset_digest(asset):
    """GitHub 新版 Release 会给出 sha256:... ，旧版没有则为 None。"""
    digest = asset.get('digest') or ''
    if isinstance(digest, str) and digest.startswith('sha256:'):
        return digest.split(':', 1)[1]
    return None


def _latest_release(cfg):
    url = '{}/repos/{}/releases/latest'.format(cfg['api_base'], cfg['repo'])
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': 'NAS-Domain-Optimizer'})
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def download(config_dir, target_dir, expected_sha256=None):
    """下载并校验。返回结果字典，不抛异常给上层界面。

    产物落在 target_dir（可写目录），**不直接替换运行中的文件**。
    """
    result = status(config_dir)
    if result.get('state') != 'update_available':
        return {'ok': False, 'message': result.get('message') or '当前没有可下载的更新'}
    cfg = _cfg(config_dir)
    asset = next((a for a in result.get('assets', []) if a.get('name') == cfg['asset']), None)
    if asset is None:
        return {'ok': False, 'message': '该版本的 Release 里没有找到 {}'.format(cfg['asset'])}
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r'[^A-Za-z0-9._-]', '_', str(asset['name']))
    destination = target_dir / filename
    temporary = target_dir / ('.downloading-' + filename)

    request = urllib.request.Request(asset['url'], headers={'User-Agent': 'NAS-Domain-Optimizer'})
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open('wb') as stream:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
                stream.write(chunk)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return {'ok': False, 'message': '下载失败：{}'.format(type(exc).__name__)}

    actual = digest.hexdigest()
    # 优先用调用方给的哈希，其次用 Release 自带摘要；两者都没有时明确告知未校验。
    expected = (expected_sha256 or asset.get('sha256') or '').lower() or None
    if expected and actual != expected:
        temporary.unlink(missing_ok=True)
        return {'ok': False, 'message': 'SHA256 校验不通过，已删除下载文件（期望 {}，实际 {}）'.format(expected[:12], actual[:12])}
    os.replace(temporary, destination)
    return {
        'ok': True,
        'verified': bool(expected),
        'path': str(destination),
        'sha256': actual,
        'size': destination.stat().st_size,
        'version': result.get('latest'),
        'message': ('已下载并校验通过，文件在 {}。请在绿联容器管理中重建镜像并重启两个容器完成替换。'
                    if expected else
                    '已下载，但该 Release 未提供 SHA256，未能校验完整性。文件在 {}，请自行核对后再替换。').format(destination),
    }
