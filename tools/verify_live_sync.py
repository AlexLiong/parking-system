"""Real sockets, 31-second disconnect, durable replay and process restart; isolated DB."""
from __future__ import annotations
import asyncio,json,os,socket,subprocess,sys,tempfile,time
from pathlib import Path
import httpx,yaml,websockets
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.audit_consistency import audit

async def run():
 out=ROOT.parent/'_qa_audit/repair/final';out.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='parking-live-sync-') as tmp:
  folder=Path(tmp);cfg=yaml.safe_load((ROOT/'config.yaml').read_text());cfg['server']['demo_mode']=True
  for k,v in {'data_dir':folder,'db_file':folder/'parking.db','photos_dir':folder/'photos','reports_dir':folder/'reports','logs_dir':folder/'logs'}.items():cfg['paths'][k]=str(v)
  config=folder/'config.yaml';config.write_text(yaml.safe_dump(cfg,allow_unicode=True))
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  base=f'http://127.0.0.1:{port}';log=(out/'live-sync.server.log').open('w');server=None
  def start():return subprocess.Popen([sys.executable,'-m','uvicorn','server.main:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'PARKING_CONFIG':str(config)},stdout=log,stderr=subprocess.STDOUT)
  async def ready(client):
   for _ in range(300):
    if server.poll() is not None:raise RuntimeError('server exited')
    try:
     r=await client.get('/api/health');r.raise_for_status();return
    except httpx.HTTPError:await asyncio.sleep(.1)
   raise RuntimeError('startup timed out')
  async def stop():
   server.terminate();await asyncio.to_thread(server.wait,15)
  try:
   server=start()
   async with httpx.AsyncClient(base_url=base,timeout=10) as c:
    await ready(c);r=await c.post('/api/auth/login',json={'username':'admin','password':'admin123'});r.raise_for_status();token=r.json()['token'];c.headers['Authorization']='Bearer '+token
    async def post(path,data={}):
     r=await c.post(path,json=data);r.raise_for_status();return r.json()
    async def snapshot():
     r=await c.get('/api/state/snapshot');r.raise_for_status();return r.json()
    await post('/api/camera/in/inject',{'reason':'initial cursor'});await post('/api/camera/in/recover')
    first=await snapshot();cursor=first['seq'];sid=first['session']['id'];assert cursor>0
    async def connect(since):
     ws=await websockets.connect(f'ws://127.0.0.1:{port}/ws?features=v2&since={since}&session={sid}',subprotocols=['v2','token.'+token],origin=base)
     welcome=json.loads(await ws.recv());assert welcome['type']=='welcome';return ws
    async def until(ws,target):
     events=[]
     while not events or events[-1]['seq']<target:
      message=json.loads(await asyncio.wait_for(ws.recv(),10));events.extend(message['events'] if message['type']=='batch' else [message] if message['type']=='event' else [])
     assert len({e['seq'] for e in events})==len(events);assert [e['seq'] for e in events]==list(range(events[0]['seq'],target+1));return events
    a=await connect(cursor);b=await connect(cursor)
    await post('/api/camera/out/inject',{'reason':'both clients'});head=(await snapshot())['seq'];ea,eb=await asyncio.gather(until(a,head),until(b,head));assert ea==eb
    cursor=head;await a.close();await b.close();began=time.monotonic();print('disconnected; waiting 31 wall seconds',flush=True);await asyncio.sleep(31)
    await post('/api/camera/out/recover');await post('/api/camera/in/inject',{'reason':'offline event'})
    head=(await snapshot())['seq'];r=await c.get('/api/events',params={'since':cursor,'limit':500});r.raise_for_status();expected=r.json()['items'];assert expected
    a=await connect(cursor);b=await connect(cursor);ra,rb=await asyncio.gather(until(a,head),until(b,head));assert ra==rb==expected;await a.close();await b.close()
    await post('/api/simulation/control',{'paused':True});before=await snapshot();await stop();server=start();await ready(c);after=await snapshot();assert after['session']==before['session'];assert after['seq']>=before['seq'];assert after['layout']['free_total']==before['layout']['free_total']==80
    for gate in ['in','out']:assert after['barriers'][gate]['state']==before['barriers'][gate]['state']
    await stop();result={'pass':True,'disconnect_wall_seconds':time.monotonic()-began,'minimum_disconnect_seconds':31,'two_clients_identical':True,'replayed':len(expected),'seqs':[e['seq'] for e in expected],'session':sid,'before_restart_seq':before['seq'],'after_restart_seq':after['seq'],'barriers_before':before['barriers'],'barriers_after':after['barriers'],'sql_audit':audit(folder/'parking.db')};assert result['sql_audit']['consistent'];(out/'live-sync.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
  finally:
   if server and server.poll() is None:await stop()
   log.close()
if __name__=='__main__':asyncio.run(run())
