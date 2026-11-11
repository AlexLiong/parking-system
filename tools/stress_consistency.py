"""Authenticated real HTTP load test; run only against an isolated demo database."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import urllib.request
import urllib.error


def run(url,concurrency,password,offset):
    credential=None
    def request(method,path,body=None):
        data=None if body is None else json.dumps(body).encode()
        headers={'Content-Type':'application/json'}
        if credential:headers['Authorization']='Bearer '+credential
        start=time.perf_counter()
        try:
            with urllib.request.urlopen(urllib.request.Request(url+path,data=data,headers=headers,method=method),timeout=30) as r:
                return {'status':r.status,'body':json.load(r),'ms':(time.perf_counter()-start)*1000}
        except urllib.error.HTTPError as e:
            return {'status':e.code,'body':json.load(e),'ms':(time.perf_counter()-start)*1000}
    auth=request('POST','/api/auth/login',{'username':'admin','password':password})
    assert auth['status']==200
    credential=auth['body']['token']
    def batch(fn):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:return list(pool.map(fn,range(concurrency)))
    entered=batch(lambda i:request('POST','/api/manual/register',{'plate':f'京Z{offset+i:05d}'}))
    assert all(r['status']==200 for r in entered),entered
    reads=batch(lambda i:request('GET','/api/overview'))
    duplicate=batch(lambda i:request('POST','/api/manual/register',{'plate':f'京Z{offset:05d}'}))
    paid=batch(lambda i:request('POST','/api/pay',{'record_id':entered[i]['body']['record_id']}))
    retried=batch(lambda i:request('POST','/api/pay',{'record_id':entered[0]['body']['record_id']}))
    assert all(r['status']==200 for r in reads+paid+retried)
    assert all(r['status']==409 for r in duplicate)
    assert len({r['body']['payment_id'] for r in retried})==1
    # A protocol client finishes every permit and acknowledges recovery commands.
    snapshot=request('GET','/api/state/snapshot')['body']
    for gate,state in snapshot['barriers'].items():
        for i in range(state['pending']):
            assert request('POST','/sim/events/passed',{'gate':gate,'vehicle_id':f'load-{i}'})['status']==200
        if state['state']=='FAULT':
            recovery=request('POST',f'/api/barrier/{gate}/clear-fault')['body']
            if recovery.get('cmd_id'):
                assert request('POST','/sim/events/barrier_ack',{'gate':gate,'state':'closed','cmd_id':recovery['cmd_id']})['status']==200
    stages={}
    for name,values in [('enter',entered),('overview',reads),('duplicate',duplicate),('pay',paid),('retry_pay',retried)]:
        times=sorted(r['ms'] for r in values)
        stages[name]={'count':len(values),'statuses':{str(s):sum(r['status']==s for r in values) for s in {r['status'] for r in values}},
            'p50_ms':round(times[len(times)//2],2),'p95_ms':round(times[min(len(times)-1,int(len(times)*.95))],2),'max_ms':round(times[-1],2)}
    return {'concurrency':concurrency,'stages':stages,'http_500':0,'snapshot':request('GET','/api/state/snapshot')['body']}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',default='http://127.0.0.1:9632')
    p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    results=[run(a.url,c,os.getenv('PARKING_ADMIN_PASSWORD','admin123'),o) for c,o in [(20,81000),(50,82000)]]
    a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps([{k:v for k,v in r.items() if k!='snapshot'} for r in results],ensure_ascii=False,indent=2))
