"""Fetch a pinned upstream tool or verify an offline build cache. No execution."""
from pathlib import Path
import hashlib,urllib.request,tarfile,platform,sys
VERSION='2.3.5'
SHA256='c4c8fc76b4e1bf2bdb5ced8b765956d82dda7bc4eb59df5c04053f0f7db98d90'
if platform.machine().lower() not in ('x86_64','amd64'):raise SystemExit('This release supports linux/amd64 only')
p=Path('build/cfst.tar.gz');p.parent.mkdir(exist_ok=True)
if not p.exists():
    with urllib.request.urlopen(f'https://github.com/XIU2/CloudflareSpeedTest/releases/download/v{VERSION}/cfst_linux_amd64.tar.gz',timeout=60) as r:
        data=r.read(16*1024*1024)
    if hashlib.sha256(data).hexdigest()!=SHA256:raise SystemExit('CFST download hash mismatch')
    p.write_bytes(data)
if hashlib.sha256(p.read_bytes()).hexdigest()!=SHA256:raise SystemExit('CFST cache hash mismatch')
if len(sys.argv)>1:
    with tarfile.open(p) as t:
        rows=[m for m in t.getmembers() if m.isfile() and m.name.rsplit('/',1)[-1]=='cfst']
        assert len(rows)==1
        binary=t.extractfile(rows[0]).read()
    assert hashlib.sha256(binary).hexdigest()=='2d0b1f831eea02ea2eb98d1cecbe9d8d631a72f19c46b23bb0457cb12f75e556'
    target=Path(sys.argv[1]);target.write_bytes(binary);target.chmod(0o755)
print('Verified CFST v'+VERSION)
