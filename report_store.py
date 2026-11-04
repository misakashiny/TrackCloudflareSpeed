"""Host-side compact report cache, refreshed by the existing minute observer."""
import time
from benefit_reports import compact_round,build

def update(root,transfer):
    from observe_transfers import read,atomic
    now=int(time.time());state=root/'runtime/state';directory=state/'reports';directory.mkdir(exist_ok=True)
    rounds=read(directory/'domain-rounds.json',[]);known={r.get('started_at') for r in rounds}
    for path in (state/'history').glob('*.json'):
        try:stamp=int(path.stem)
        except ValueError:continue
        if stamp in known or now-stamp>604800+3600:continue
        raw=read(path,{})
        if raw.get('finished_at'):rounds.append(compact_round(raw));known.add(stamp)
    rounds=sorted((r for r in rounds if 0<=now-r.get('at',0)<=604800+3600),key=lambda r:r['at'])[-600:]
    points=read(directory/'transfer-points.json',[])
    # Seed historical rates only; old samples lack cumulative counters. Do not
    # invent historical byte totals or apply current quotas to old measurements.
    if not points:
        points=[{'at':r['at'],'up_Bps':r.get('up_Bps',0),'down_Bps':r.get('down_Bps',0),'all_clients_ok':len(r.get('clients',{}))==3 and all(c.get('ok') for c in r['clients'].values())} for r in read(state/'transfer/samples.json',[])]
    clients=transfer.get('clients',{});counters={n:c['counters'] for n,c in clients.items() if c.get('ok') and c.get('counters')}
    point={'at':transfer['at'],'up_Bps':transfer.get('total_up_Bps',0),'down_Bps':transfer.get('total_down_Bps',0),
           'all_clients_ok':len(clients)==3 and all(c.get('ok') for c in clients.values()),'counters':counters if len(counters)==3 else None}
    points=[p for p in points if p.get('at')!=point['at'] and 0<=now-p.get('at',0)<=604800+120]+[point]
    points=sorted(points,key=lambda p:p['at'])[-10500:]
    jobs=read(state/'acceptance/history.json',[])
    budget_jobs={row['id']:row for row in read(state/'acceptance/budget-history.json',[]) if row.get('id')}
    current_budget=read(state/'acceptance/budget.json',{})
    if current_budget.get('id'):budget_jobs[current_budget['id']]=current_budget
    report=build(rounds,points,jobs,now,budget_jobs=list(budget_jobs.values()))
    atomic(directory/'domain-rounds.json',rounds);atomic(directory/'transfer-points.json',points);atomic(directory/'latest.json',report)
    (directory/'error.json').unlink(missing_ok=True)
