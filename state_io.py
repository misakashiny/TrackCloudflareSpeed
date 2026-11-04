"""Atomic JSON persistence shared by read-only reporting services."""
import json,os,tempfile
from pathlib import Path
def read(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return default
def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.state-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
