"""Portable startup, with no host service or downloader access."""
import os,sys,time
from pathlib import Path
role=sys.argv[1] if len(sys.argv)>1 else 'worker'
if role=='guard':
    import guard
    while True:
        guard.clean(Path('/project'))
        time.sleep(15)
elif role=='worker':
    root=Path(os.environ.get('OPTIMIZER_ROOT','/service'))
    for d in ['state','dns','work','control']:(root/d).mkdir(parents=True,exist_ok=True)
    os.execv(sys.executable,[sys.executable,str(Path(__file__).parent/'optimizer.py'),*sys.argv[2:]])
elif role=='collector':
    os.execv(sys.executable,[sys.executable,str(Path(__file__).parent/'collector.py'),*sys.argv[2:]])
elif role=='ui':
    os.execv(sys.executable,[sys.executable,str(Path(__file__).parent/'web/server.py')])
else:raise SystemExit('Unknown role')
