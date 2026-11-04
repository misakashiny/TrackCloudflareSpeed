"""Short offline smoke: real worker/UI, synthetic registry, no downloader or probes."""
import subprocess,tempfile,pathlib,os,time,json,urllib.request,urllib.error,http.cookiejar,sys,socket
root=pathlib.Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as tmp:
 t=pathlib.Path(tmp)
 for d in ['state','dns','work','control','config']:(t/d).mkdir()
 for n in ['settings.json','cloudflare-ipv4.txt']:(t/'config'/n).write_bytes((root/'config'/n).read_bytes())
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 env=dict(os.environ,OPTIMIZER_ROOT=str(t),UI_STATE=str(t/'state'),UI_CONTROL=str(t/'control'),UI_SETTINGS=str(t/'config/settings.json'),UI_DNS=str(t/'dns/active.hosts'),UI_BIND='127.0.0.1',UI_PORT=str(port),UI_PUBLIC_HOST=f'127.0.0.1:{port}',UI_CLIENT_NETWORK='127.0.0.0/8')
 procs=[]
 try:
  for script in ['optimizer.py','web/server.py']:procs.append(subprocess.Popen([sys.executable,str(root/script)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
  url=f'http://127.0.0.1:{port}'
  op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
  for _ in range(50):
   try:
    with op.open(url+'/api/status',timeout=1) as f:data=json.load(f)
    if data['worker_healthy']:break
   except OSError:pass
   time.sleep(.1)
  else:raise AssertionError('worker/UI failed readiness')
  assert data['domains']==[]
  payload={'revision':data['registry_revision'],'operation':'save','name':'Example','website_domains':['example.org'],'tracker_domains':[],'enabled':False}
  # Match the deployed API schema; disabled sites cannot trigger network probes.
  request=urllib.request.Request(url+'/api/sites',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Origin':url,'X-CSRF-Token':data['csrf']})
  with op.open(request,timeout=3) as f:assert f.status==200
  with op.open(url+'/api/status',timeout=3) as f:updated=json.load(f)
  assert updated['registry_revision']==data['registry_revision']+1
  assert (t/'control/sites.json').exists()
  def rejected(path,headers,expected):
   req=urllib.request.Request(url+path,data=b'{}',headers=headers)
   try:op.open(req,timeout=3)
   except urllib.error.HTTPError as e:assert e.code==expected,(e.code,expected)
   else:raise AssertionError('request unexpectedly accepted')
  rejected('/api/sites',{'Origin':url},403)
  rejected('/api/sites',{'Origin':'http://untrusted.example','X-CSRF-Token':data['csrf']},403)
  rejected('/api/acceptance',{'Origin':url,'X-CSRF-Token':data['csrf']},501)
  assert not (t/'control/acceptance-request.json').exists()
  print('PASS: actual worker heartbeat, HTTP status, CSRF site save, persistent registry')
 finally:
  for p in procs:
   p.terminate()
  for p in procs:p.wait(timeout=5)
