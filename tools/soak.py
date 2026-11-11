"""Wall-clock HTTP/RSS soak against a fresh local test server and database."""
from __future__ import annotations
import argparse,json,os,platform,socket,subprocess,sys,tempfile,time
from pathlib import Path
import httpx,yaml
ROOT=Path(__file__).resolve().parents[1]

def rss_kib(pid):
    if sys.platform.startswith('linux'):
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):return int(line.split()[1])
    elif sys.platform=='darwin':
        return int(subprocess.check_output(['ps','-p',str(pid),'-o','rss='],text=True).strip())
    else:
        result=subprocess.check_output(['tasklist','/FI',f'PID eq {pid}','/FO','CSV','/NH'],text=True)
        import csv
        return int(''.join(c for c in next(csv.reader([result.strip()]))[-1] if c.isdigit()))
    raise RuntimeError('RSS unavailable')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--seconds',type=int,default=7200);parser.add_argument('--output',default='../_qa_audit/repair/T5/evidence/soak-http.json');args=parser.parse_args()
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='parking-soak-') as tmp:
        folder=Path(tmp);cfg=yaml.safe_load((ROOT/'config.yaml').read_text());cfg['server']['demo_mode']=True
        for key,value in {'data_dir':folder,'db_file':folder/'parking.db','photos_dir':folder/'photos','reports_dir':folder/'reports','logs_dir':folder/'logs'}.items():cfg['paths'][key]=str(value)
        config=folder/'config.yaml';config.write_text(yaml.safe_dump(cfg,allow_unicode=True))
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with output.with_suffix('.server.log').open('w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','server.main:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'PARKING_CONFIG':str(config),'PARKING_ADMIN_PASSWORD':'admin123'},stdout=log,stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=5) as client:
                    for _ in range(200):
                        if server.poll() is not None:raise RuntimeError('server exited at startup')
                        try:client.get('/api/health').raise_for_status();break
                        except httpx.HTTPError:time.sleep(.1)
                    else:raise RuntimeError('startup timeout')
                    headers={};renewed=0;start=time.monotonic();samples=[];requests=0;errors=[]
                    while time.monotonic()-start<args.seconds:
                        now=time.monotonic()
                        if not headers or now-renewed>1800:
                            token=client.post('/api/auth/login',json={'username':'admin','password':'admin123'});token.raise_for_status();headers={'Authorization':'Bearer '+token.json()['token']};renewed=now
                        durations=[]
                        for endpoint in ['/api/health','/api/overview','/api/state/snapshot','/api/stats?period=day']:
                            at=time.monotonic();response=client.get(endpoint,headers=headers);durations.append(time.monotonic()-at);requests+=1
                            if response.status_code!=200:errors.append({'endpoint':endpoint,'status':response.status_code})
                        elapsed=time.monotonic()-start
                        samples.append({'elapsed':elapsed,'rss_kib':rss_kib(server.pid),'max_request_seconds':max(durations)})
                        if len(samples)%30==0:print(json.dumps(samples[-1]),flush=True)
                        if len(samples)%60==0:output.write_text(json.dumps({'complete':False,'target_seconds':args.seconds,'wall_seconds':elapsed,'requests':requests,'errors':errors,'samples':samples},indent=2))
                        time.sleep(max(0,1-(time.monotonic()-now)))
                    warm=[s['rss_kib'] for s in samples if s['elapsed']>=min(300,args.seconds/4)]
                    growth=(max(warm)-min(warm)) if warm else 0
                    result={'complete':True,'environment':platform.platform(),'target_seconds':args.seconds,'wall_seconds':time.monotonic()-start,'requests':requests,'errors':errors,'rss_growth_kib':growth,'rss_limit_kib':32768,'samples':samples,'pass':not errors and growth<=32768}
                    output.write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='samples'}));return 0 if result['pass'] else 1
            finally:
                server.terminate()
                try:server.wait(timeout=10)
                except subprocess.TimeoutExpired:server.kill();server.wait()
if __name__=='__main__':raise SystemExit(main())
