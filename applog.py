"""日志：分级别写入、可按条件检索、可脱敏导出。

设计目标只有一个：出问题时能最快定位。
因此：
1. 每条日志带 level / 事件码 / 中文说明，能按级别和关键字筛。
2. 文件按大小轮转，不会无限增长。
3. 写入失败绝不能影响主流程（日志是辅助，不是负担）。
4. 导出前统一脱敏，避免把凭据带出去。
"""
import json
import os
import re
import time
from pathlib import Path
import threading

LEVELS = {'debug': 10, 'info': 20, 'warn': 30, 'error': 40}
MAX_BYTES = 512 * 1024      # 单个日志文件上限
MAX_FILES = 3               # 轮转保留份数（含当前）
LOCK = threading.Lock()

# 脱敏规则：导出与写入前都过一遍，宁严勿松。
SECRET_PATTERNS = [
    re.compile(r'(passkey|token|secret|password|apikey|api_key)([=:]\s*)([^\s&,"\]}]+)', re.I),
    re.compile(r'(https?://[^/\s:@]+):([^@\s]+)@'),          # URL 里的 user:pass@
    re.compile(r'\b([A-Za-z0-9._-]{1,64}):([^\s@/]{1,64})@'),  # 裸写的 user:pass@（无协议头）
    re.compile(r'\b[A-Za-z0-9_-]{24,}\b'),                   # 过长的随机串（token 形态）
]


def redact(text):
    text = str(text)
    text = SECRET_PATTERNS[0].sub(lambda m: '{}{}***'.format(m.group(1), m.group(2)), text)
    text = SECRET_PATTERNS[1].sub(lambda m: '{}:***@'.format(m.group(1)), text)
    # 裸写的凭据：保留用户名形态但抹掉密码，便于仍能看出是哪个账号。
    text = SECRET_PATTERNS[2].sub(lambda m: '{}:***@'.format(m.group(1)[:2] + '***'), text)
    text = SECRET_PATTERNS[3].sub(lambda m: _mask_token(m.group(0)), text)
    return text


def redact_data(value):
    """递归脱敏 JSON 结构。

    配置里可能有 metadata_proxy（带主机名端口）这类值，
    只靠字符串正则拦不干净，这里对配置整段做“键名 → 值”的脱敏：
    键名命中敏感词且值像 URL 时，只保留协议和路径，去掉主机与端口。
    """
    sensitive = re.compile(r'(token|secret|password|passkey|proxy|auth|key|cookie)', re.I)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if sensitive.search(str(key)) and isinstance(item, str) and item:
                out[key] = '<已脱敏:{}>'.format(str(key))
            else:
                out[key] = redact_data(item)
        return out
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def _mask_token(value):
    # 只遮疑似 token 的串，避免把普通长英文单词也遮掉：含数字或大小写混合才处理。
    if re.search(r'\d', value) and re.search(r'[a-zA-Z]', value):
        return value[:4] + '***'
    return value


class LogFile:
    """一个可轮转的日志文件。"""

    def __init__(self, directory, name='optimizer.log'):
        self.directory = Path(directory)
        self.path = self.directory / name

    def _rotate_if_needed(self):
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                for index in range(MAX_FILES - 1, 0, -1):
                    src = self.directory / '{}.{}'.format(self.path.name, index)
                    dst = self.directory / '{}.{}'.format(self.path.name, index + 1)
                    if src.exists():
                        os.replace(src, dst)
                os.replace(self.path, self.directory / (self.path.name + '.1'))
        except OSError:
            pass

    def write(self, level, code, message, **fields):
        if level not in LEVELS:
            level = 'info'
        record = {'at': int(time.time()), 'level': level, 'code': code,
                  'message': redact(message)}
        if fields:
            record['detail'] = {k: redact(v) if isinstance(v, str) else v for k, v in fields.items() if v is not None}
        line = json.dumps(record, ensure_ascii=False)
        with LOCK:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(line + '\n')
            except OSError:
                pass  # 日志写失败不能影响业务
        return record

    def tail(self, limit=200, level='', keyword='', code=''):
        """读取最近的日志，按级别/关键字/事件码过滤。返回新→旧。"""
        records = []
        files = [self.path] + [self.directory / '{}.{}'.format(self.path.name, i) for i in range(1, MAX_FILES)]
        min_level = LEVELS.get(level, 0)
        for path in files:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                if min_level and LEVELS.get(item.get('level', 'info'), 20) < min_level:
                    continue
                if code and item.get('code') != code:
                    continue
                if keyword:
                    haystack = json.dumps(item, ensure_ascii=False).lower()
                    if keyword.lower() not in haystack:
                        continue
                records.append(item)
                if len(records) >= limit:
                    return records
        return records


def summary(records):
    """按级别与事件码统计，快速看出问题分布。"""
    by_level, by_code = {}, {}
    for item in records:
        by_level[item.get('level', 'info')] = by_level.get(item.get('level', 'info'), 0) + 1
        by_code[item.get('code', '?')] = by_code.get(item.get('code', '?'), 0) + 1
    return {'by_level': by_level, 'by_code': dict(sorted(by_code.items(), key=lambda kv: -kv[1])[:10])}
