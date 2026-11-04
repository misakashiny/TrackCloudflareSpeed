from site_registry import is_excluded
"""Remove expired overrides even when the optimizer container has stopped."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import nullcontext

try:  # 生产环境（NAS 宿主机 Linux）使用真实跨进程文件锁
    import fcntl
except ImportError:  # 本地 Windows 复盘与测试：降级为总是成功的进程内锁
    import types

    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 2
    fcntl.LOCK_NB = 4
    fcntl.flock = lambda handle, operation=0: True

from site_registry import load_registry, domain_index
from policy_model import GUARD_STALE_SECONDS
import speed_engine


def clean(base, now=None, dns_directory=None):
    now = time.time() if now is None else now
    runtime = base / "runtime"
    state = runtime / "state"
    dns = dns_directory or runtime / "dns"
    if runtime.is_symlink() or state.is_symlink() or dns.is_symlink():
        raise ValueError("Unexpected linked runtime directory")
    try:
        cfg = json.loads((base / "config/settings.json").read_text())
        if not isinstance(cfg,dict):raise ValueError('settings must be an object')
    except (OSError,ValueError):cfg={}
    try:
        registry=load_registry(runtime / 'control/sites.json', cfg['domains'])
        meta=domain_index(registry,enabled_only=True)
        cfg['_domain_meta']=meta
        cfg['speed']=speed_engine.policy(cfg,json.loads((runtime/'control/speed-policy.json').read_text()) if (runtime/'control/speed-policy.json').exists() else {})
        allowed = {d for d in meta if speed_engine.site_mode(d,cfg)!='origin'}
    except (ValueError, TypeError, KeyError, OSError):
        allowed = set()
    try:networks = [ipaddress.ip_network(x) for x in
                (base / "config/cloudflare-ipv4.txt").read_text().splitlines()
                if x.strip() and not x.startswith("#")]
    except (OSError,ValueError):networks=[]
    if not state.is_dir():return {'action':'waiting_for_worker'}
    try:
        beat=json.loads((state/'heartbeat.json').read_text())
        prehealthy=0<=now-beat['at']<GUARD_STALE_SECONDS
    except (OSError,ValueError,KeyError,TypeError):prehealthy=False
    update=state/'update.lock'
    if update.is_symlink():raise ValueError('Unexpected update lock symlink')
    with update.open('r') if update.exists() else nullcontext(None) as lock:
        try:
            if lock is not None:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if prehealthy:return {"action": "probe_in_progress"}
        publisher=state/'publish.lock'
        if publisher.is_symlink():raise ValueError('Unexpected publisher symlink')
        created=not publisher.exists()
        with publisher.open('a' if created else 'r') as publish_lock:
            if created:
                metadata=state.stat();os.chown(publisher,metadata.st_uid,metadata.st_gid) if hasattr(os,'chown') else None
            try:fcntl.flock(publish_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'action':'publication_in_progress','worker_healthy':prehealthy}
            return _prune(state,dns,allowed,networks,now)


def _prune(state,dns,allowed,networks,now):
        try:
            heartbeat = json.loads((state / "heartbeat.json").read_text())
            healthy = 0 <= now - heartbeat["at"] < GUARD_STALE_SECONDS
            entries = json.loads((state / "entries.json").read_text())
            if not isinstance(entries,dict):raise ValueError('invalid entries')
        except (OSError, ValueError, KeyError,TypeError):
            healthy, entries = False, {}
        live = {}
        for domain, row in entries.items():
            try:
                addr = ipaddress.ip_address(row["ip"])
                if (healthy and domain in allowed and not is_excluded(domain)
                        and now < row["expires_at"] and addr.is_global
                        and addr.version == 4 and any(addr in net for net in networks)):
                    live[domain] = row["ip"]
            except (ValueError, KeyError, TypeError):
                continue
        content = "# NAS Domain Optimizer: exact domains; no Tracker URLs or credentials\n"
        content += "".join(ip + " " + domain + "\n" for domain, ip in sorted(live.items()))
        target = dns / "active.hosts"
        if target.is_symlink():
            raise ValueError("Unexpected hosts symlink")
        if target.exists() and target.read_text() == content:
            return {"action": "unchanged", "active": len(live)}
        fd, tmp = tempfile.mkstemp(prefix=".guard-", dir=dns.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"action": "pruned", "active": len(live), "worker_healthy": healthy}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--dns-dir", type=Path,
                        default=None)
    args = parser.parse_args()
    result = clean(args.root, dns_directory=args.dns_dir)
    if result["action"] != "unchanged":
        print(json.dumps(result))
