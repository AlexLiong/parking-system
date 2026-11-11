"""Smoke an owned disposable container, production defaults and all three LPR styles."""
import argparse,json,subprocess,time
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--image',default='parking-course:final');args=p.parse_args()
 out=ROOT.parent/'_qa_audit/repair/final';out.mkdir(parents=True,exist_ok=True)
 cid=subprocess.check_output(['docker','run','--rm','-d','-p','127.0.0.1::8000','-e','PARKING_ADMIN_PASSWORD=container-smoke-only-2026',args.image],text=True).strip()
 try:
  mapping=subprocess.check_output(['docker','port',cid,'8000/tcp'],text=True).strip();port=int(mapping.rsplit(':',1)[1]);start=time.monotonic()
  with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=30) as c:
   for _ in range(300):
    try:c.get('/api/health').raise_for_status();break
    except httpx.HTTPError:time.sleep(.2)
   else:raise RuntimeError('container not ready')
   startup=time.monotonic()-start;assert c.get('/api/overview').status_code==401
   r=c.post('/api/auth/login',json={'username':'admin','password':'container-smoke-only-2026'});r.raise_for_status();headers={'Authorization':'Bearer '+r.json()['token']}
   assert c.post('/api/auth/demo-session').status_code==403
   gallery=c.get('/api/recognize/gallery',headers=headers);gallery.raise_for_status();assert len(gallery.json()['samples'])==18
   assert c.get('/api/find-car?plate=京A12345',headers=headers).status_code==404
   assert c.get('/api/overview',headers=headers).json()['free_total']==80
   predictions=[]
   for style in ['blue','green_car','green_truck']:
    sample=next(x for x in gallery.json()['samples'] if x['style']==style)
    with (ROOT/'datasets/demo-lpr'/sample['file']).open('rb') as image:r=c.post('/api/recognize',headers=headers,files={'file':(sample['file'],image,'image/png')},data={'gate':'in'})
    r.raise_for_status();data=r.json();assert data['plate']==sample['plate'] and not data['need_manual'],data;predictions.append({'style':style,'plate':data['plate'],'need_manual':data['need_manual']})
   result={'pass':True,'image':args.image,'startup_seconds':startup,'demo_mode':False,'gallery':18,'predictions':predictions};(out/'docker-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
 finally:
  (out/'docker-smoke.server.log').write_text(subprocess.check_output(['docker','logs',cid],stderr=subprocess.STDOUT,text=True))
  subprocess.run(['docker','stop',cid],check=True,stdout=subprocess.DEVNULL)
