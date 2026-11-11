"""Cold-start an owned native bundle with an isolated DB and test actual local ONNX inference."""
from __future__ import annotations
import argparse,json,os,socket,subprocess,tempfile,time,shutil
from pathlib import Path
import httpx,yaml
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--native',type=Path,required=True);parser.add_argument('--default-config',action='store_true');parser.add_argument('--output',type=Path,default=ROOT.parent/'_qa_audit/repair/P3-02/evidence/native-smoke.json');args=parser.parse_args()
    native=args.native.resolve();output=args.output;output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='parking-native-') as tmp:
        folder=Path(tmp);cfg=yaml.safe_load((native.parent/'config.yaml').read_text());cfg['server']['demo_mode']=False
        for key,val in {'data_dir':folder,'db_file':folder/'parking.db','photos_dir':folder/'photos','reports_dir':folder/'reports','logs_dir':folder/'logs'}.items():cfg['paths'][key]=str(val)
        cfg['logging']['dir']=str(folder/'logs');config=folder/'config.yaml';config.write_text(yaml.safe_dump(cfg,allow_unicode=True))
        launch_native=native
        launch_env={**os.environ,'PARKING_CONFIG':str(config),'PARKING_ADMIN_PASSWORD':'native-smoke-only-2026'}
        if args.default_config:
            shutil.copytree(native.parent,folder/'parking-system',symlinks=True,ignore=shutil.ignore_patterns('data','__pycache__'))
            shutil.copytree(native.parent.parent/'assets_3d',folder/'assets_3d',symlinks=True)
            launch_native=folder/'parking-system'/native.name
            launch_env.pop('PARKING_CONFIG',None)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with output.with_suffix('.server.log').open('w') as log:
            start=time.monotonic();proc=subprocess.Popen([str(launch_native),'--port',str(port)],env=launch_env,stdout=log,stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=30) as c:
                    for _ in range(240):
                        if proc.poll() is not None:raise RuntimeError(output.with_suffix('.server.log').read_text())
                        try:c.get('/api/health').raise_for_status();break
                        except httpx.HTTPError:time.sleep(.25)
                    else:raise RuntimeError('startup timeout')
                    ready=time.monotonic()-start
                    assert c.get('/api/overview').status_code==401
                    assert c.post('/api/auth/demo-session').status_code==403
                    assert c.post('/api/auth/login',json={'username':'admin','password':'admin123'}).status_code==401
                    token=c.post('/api/auth/login',json={'username':'admin','password':'native-smoke-only-2026'});token.raise_for_status();headers={'Authorization':'Bearer '+token.json()['token']}
                    pages={path:c.get(path).status_code for path in ['/admin/','/sim/','/common/favicon.svg','/assets3d/layout/lot_v2.json']};assert set(pages.values())=={200}
                    overview=c.get('/api/overview',headers=headers).json();assert overview['free_total']==80
                    predictions=[]
                    for style in ['blue','green_car','green_truck']:
                        gallery=json.loads((native.parent/'datasets/demo-lpr/manifest.json').read_text())
                        sample=next(x for x in gallery['samples'] if x['style']==style)
                        with (native.parent/'datasets/demo-lpr'/sample['file']).open('rb') as image:
                            r=c.post('/api/recognize',headers=headers,files={'file':(sample['file'],image,'image/png')},data={'gate':'in'})
                        r.raise_for_status();result=r.json();assert result['plate']==sample['plate'],result;assert not result['need_manual'],result
                        predictions.append({'style':style,'plate':result['plate'],'need_manual':result['need_manual']})
                    record={'pass':True,'startup_seconds':ready,'pages':pages,'free_total':80,'predictions':predictions,'demo_mode':False,'default_config_without_env':args.default_config}
                    output.write_text(json.dumps(record,ensure_ascii=False,indent=2));print(json.dumps(record,ensure_ascii=False))
            finally:
                proc.terminate()
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
if __name__=='__main__':main()
