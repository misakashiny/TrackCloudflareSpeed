"""一键导出诊断包：把定位问题需要的材料打成一个 zip。

原则：
1. 只收集诊断必需的内容，不收集媒体文件、下载器数据、任何凭据。
2. 打包前统一脱敏，宁可少给也不带出密钥。
3. 在内存里生成，不落地临时明文文件。
"""
import io
import json
import time
import zipfile
from pathlib import Path

from applog import LogFile, redact, redact_data

MAX_LOG_LINES = 500
MAX_HISTORY_ROUNDS = 5


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def build_diagnostic_zip(state, control, settings, logfile, version, extra=None):
    """返回 (文件名, bytes)。任何单项失败都只跳过该项，不整体失败。"""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = 'diagnostics-{}-v{}.zip'.format(stamp, version)
    buffer = io.BytesIO()
    included, skipped = [], []

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as bundle:
        def add(filename, data):
            bundle.writestr(filename, redact_bytes(data))
            included.append(filename)

        # 1. 运行概况：版本、心跳、进度、手动检测状态
        overview = {
            'exported_at': int(time.time()),
            'version': version,
            'heartbeat': _read_json(state / 'heartbeat.json'),
            'progress': _read_json(state / 'progress.json'),
            'manual_request': _read_json(state / 'manual-request.json'),
            'active_overrides': _read_json(state / 'entries.json'),
        }
        add('overview.json', _json_bytes(overview))

        # 2. 最近几轮检测结果（含失败原因，是定位问题的核心）
        latest = _read_json(state / 'last-run.json')
        if latest:
            add('last-run.json', _json_bytes(_trim_report(latest)))
        else:
            skipped.append('last-run.json 不存在')

        history_dir = state / 'history'
        try:
            files = sorted(history_dir.glob('*.json'), key=lambda p: p.name, reverse=True)[:MAX_HISTORY_ROUNDS]
        except OSError:
            files = []
        for path in files:
            data = _read_json(path)
            if data:
                add('history/' + path.name, _json_bytes(_trim_report(data)))
        if not files:
            skipped.append('没有历史检测文件')

        # 3. 配置（脱敏后）
        # 配置里的 metadata_proxy 等字段带主机名，字符串正则拦不干净，
        # 这里先按结构脱敏（敏感键整值替换），再走一次文本脱敏兜底。
        for label, path in (('config/settings.json', settings), ('config/sites.json', control / 'sites.json')):
            data = _read_json(path)
            if data is not None:
                add(label, _json_bytes(redact_data(data)))
            else:
                skipped.append('{} 不可读'.format(label))

        # 4. 日志尾部
        records = logfile.tail(MAX_LOG_LINES)
        if records:
            add('logs/recent.jsonl',
                '\n'.join(json.dumps(r, ensure_ascii=False) for r in records).encode('utf-8'))
        else:
            skipped.append('暂无日志')

        # 5. 站点操作审计
        audit = control / 'site-changes.jsonl'
        try:
            lines = audit.read_text(encoding='utf-8').splitlines()[-100:]
            if lines:
                add('logs/site-changes.jsonl', '\n'.join(lines).encode('utf-8'))
        except OSError:
            skipped.append('没有站点操作审计')

        # 6. 导出说明，方便收到包的人知道里面有什么
        readme = [
            'NAS 域名优选 诊断包',
            '导出时间: {}'.format(time.strftime('%Y-%m-%d %H:%M:%S')),
            '版本: {}'.format(version),
            '',
            '包含内容:',
        ] + ['  - ' + item for item in included] + [
            '',
            '未包含/跳过:',
        ] + (['  - ' + item for item in skipped] or ['  （无）']) + [
            '',
            '说明: 已统一脱敏，不含 passkey、Cookie、账号密码等凭据。',
            '     不含媒体文件、下载器任务或源文件。',
        ]
        bundle.writestr('README.txt', '\n'.join(readme))
        if extra:
            for key, value in extra.items():
                bundle.writestr('extra/{}.json'.format(key), redact_bytes(_json_bytes(value)))

    return name, buffer.getvalue()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _trim_report(report):
    """去掉体积大且无诊断价值的字段，控制包体大小。"""
    if not isinstance(report, dict):
        return report
    trimmed = dict(report)
    domains = {}
    for name, row in (report.get('domains') or {}).items():
        if not isinstance(row, dict):
            continue
        keep = {k: v for k, v in row.items()
                if k not in ('candidate_probes', 'ranked_candidates')}
        # 候选明细只保留结论，不保留每个地址的两次结果，避免包过大。
        probes = row.get('candidate_probes') or []
        if probes:
            keep['candidate_summary'] = [
                {'ip': p.get('ip'), 'ok': all(r.get('ok') for r in (p.get('results') or []))} for p in probes]
        domains[name] = keep
    trimmed['domains'] = domains
    return trimmed


def redact_bytes(data):
    text = data.decode('utf-8', errors='replace')
    return redact(text).encode('utf-8')
