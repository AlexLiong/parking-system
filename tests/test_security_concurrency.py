from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import time

import jwt
import pytest
from sqlalchemy import select, func
from starlette.websockets import WebSocketDisconnect
from server.gateway.auth import create_token
from server.core.timeutil import utcnow
from server.repository.models import ParkingRecord, ParkingSpace, PaymentRecord
from tools.audit_consistency import audit


@pytest.mark.parametrize('path,method,body',[
    ('/api/records','get',None),('/api/overview','get',None),
    ('/api/recognition-logs','get',None),('/api/state/snapshot','get',None),
    ('/sim/events/zone','post',{'zone_id':'in_barrier','occupied':False}),
    ('/sim/scenario','get',None),('/media/private.jpg','get',None),
    ('/datasets/external/repo/.git/config','get',None)])
def test_read_and_sim_require_identity(client,path,method,body):
    r=getattr(client,method)(path,**({'json':body} if body is not None else {}))
    assert r.status_code==401


@pytest.mark.parametrize('suffix',['','?features=v2'])
def test_ws_no_token_forged_and_bad_origin(client,admin_token,suffix):
    client.cookies.clear()
    token=admin_token['Authorization'][7:]
    for protocols,headers in [([],{}),(['v2','token.forged'],{}),
                              (['v2','token.'+token],{'Origin':'https://evil.invalid'})]:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect('/ws'+suffix,subprotocols=protocols,headers=headers):
                pass
        assert error.value.code==1008


def test_logout_expiry_role_and_origin(client,admin_token):
    c=client.app.state.container
    assert client.get('/api/records',headers={**admin_token,'Origin':'https://evil.invalid'}).status_code==403
    role={'Authorization':'Bearer '+create_token('viewer','viewer',c.cfg)}
    assert client.get('/api/records',headers=role).status_code==403
    operator={'Authorization':'Bearer '+create_token('sim','operator',c.cfg)}
    assert client.get('/api/lists',headers=operator).status_code==403
    claims=jwt.decode(admin_token['Authorization'][7:],c.cfg.server.jwt_secret,algorithms=['HS256'])
    claims['exp']=int(time.time())-1
    expired=jwt.encode(claims,c.cfg.server.jwt_secret,algorithm='HS256')
    assert client.get('/api/records',headers={'Authorization':'Bearer '+expired}).status_code==401
    assert client.post('/api/auth/logout',headers=admin_token).status_code==200
    assert client.get('/api/records',headers=admin_token).status_code==401
    assert client.get('/api/records').status_code==401
    assert client.options('/api/records',headers={'Origin':'https://evil.invalid',
        'Access-Control-Request-Method':'GET'}).status_code==400


def test_non_demo_has_no_known_admin(cfg):
    from fastapi.testclient import TestClient
    from server.main import create_app
    cfg.server.demo_mode=False
    with TestClient(create_app(cfg)) as cli:
        assert cli.post('/api/auth/demo-session').status_code==403
        assert cli.post('/api/auth/login',json={'username':'admin','password':'admin123'}).status_code==401
        credentials=(cfg.paths.data_dir/'bootstrap-credentials.txt').read_text()
        password=credentials.split('password=',1)[1].strip()
        assert len(password)>=18 and password!='admin123'
        assert cli.post('/api/auth/login',json={'username':'admin','password':password}).status_code==200


def test_upload_path_and_login_rate(client,admin_token,tmp_path):
    assert client.post('/api/recognize',headers=admin_token,params={'image_path':'/etc/passwd'}).status_code==403
    assert client.post('/sim/events/arrived',headers=admin_token,json={
        'gate':'in','vehicle_id':'malicious','image_path':'/etc/passwd'}).status_code==403
    assert client.get('/datasets/external/source/.git/config',headers=admin_token).status_code==404
    assert client.post('/api/recognize',headers=admin_token,files={'file':('a.png',b'bad','image/png')}).status_code==400
    assert client.post('/api/recognize',headers=admin_token,files={'file':('a.jpg',b'a'*(5*1024*1024+1),'image/jpeg')}).status_code==413
    statuses=[client.post('/api/auth/login',json={'username':'admin','password':'wrong'}).status_code for _ in range(11)]
    assert statuses[:10]==[401]*10 and statuses[-1]==429


def test_payment_and_space_rollback(container,monkeypatch):
    c=container
    entry=c.access.manual_register('京A12345','in','test')
    original=c.allocator.release
    def broken_release(space):
        original(space)
        raise RuntimeError('injected transactional failure')
    monkeypatch.setattr(c.allocator,'release',broken_release)
    with pytest.raises(RuntimeError,match='injected'):
        c.billing.pay(entry['record_id'])
    assert c.payment_repo.get_by_parking(entry['record_id']) is None
    assert c.parking_repo.get(entry['record_id']).status=='in'
    assert c.space_repo.find_by_plate('京A12345') is not None
    assert c.access._pending['out']==0
    monkeypatch.setattr(c.allocator,'release',original)
    assert c.billing.pay(entry['record_id']).id
    assert c.parking_repo.get(entry['record_id']).status=='out'
    assert c.space_repo.find_by_plate('京A12345') is None


@pytest.mark.parametrize('concurrency',[20,50])
def test_concurrent_enter_overview_duplicate_pay(client,admin_token,concurrency):
    c=client.app.state.container
    def batch(fn):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(fn,range(concurrency)))
    def enter(i):
        return client.post('/api/manual/register',headers=admin_token,
            json={'plate':f'京A{i:05d}'} )
    entered=batch(enter)
    assert all(r.status_code==200 for r in entered),[(r.status_code,r.text) for r in entered if r.status_code!=200]
    views=batch(lambda i:client.get('/api/overview',headers=admin_token))
    assert all(r.status_code==200 for r in views)
    duplicates=batch(lambda i:enter(0))
    assert all(r.status_code==409 for r in duplicates)
    record_ids=[r.json()['record_id'] for r in entered]
    paid=batch(lambda i:client.post('/api/pay',headers=admin_token,json={'record_id':record_ids[i]}))
    assert all(r.status_code==200 for r in paid),[(r.status_code,r.text) for r in paid if r.status_code!=200]
    retried=batch(lambda i:client.post('/api/pay',headers=admin_token,json={'record_id':record_ids[0]}))
    assert len({r.json()['payment_id'] for r in retried})==1
    assert audit(c.cfg.paths.db_file)['consistent']
    assert c.db.session.scalar(select(func.count()).select_from(PaymentRecord))==concurrency
    assert c.parking_repo.count_open()==0
    c.db.remove()
    # Every worker request has returned its connection, only background tick may hold one briefly.
    assert c.db.engine.pool.checkedout()<=1


def test_index_conflict_preserves_data_and_backup(tmp_path):
    import sqlite3
    from server.repository.db import Database
    path=tmp_path/'duplicate.db'
    db=Database(path);db.init_schema();db.close()
    with sqlite3.connect(path) as sql:
        sql.execute('DROP INDEX uq_open_plate')
        for i in range(2):
            sql.execute("INSERT INTO parking_record(plate,plate_color,entry_time,status,created_at,manual_entry,temporary) VALUES ('京A12345','blue','2026-01-01','in','2026-01-01',0,0)")
    db=Database(path)
    try:
        with pytest.raises(ValueError,match='uq_open_plate'):
            db.init_schema()
        assert len(list(tmp_path.glob('duplicate.db.bak.*')))==1
        with sqlite3.connect(path) as sql:
            assert sql.execute('SELECT COUNT(*) FROM parking_record').fetchone()[0]==2
    finally:db.close()


def test_broadcast_does_not_share_publisher_transaction(container):
    import asyncio
    from server.gateway.ws_realtime import Broadcaster
    from server.repository.db import request_scope
    c=container
    async def exercise():
        broadcaster=Broadcaster(c.bus,c.cfg,state=c.state)
        key=request_scope.set('publisher-request')
        try:
            publisher=c.db.session
            publisher.execute(select(func.count()).select_from(ParkingRecord))
            async def read_batch(ws):
                assert c.db.session is not publisher
                c.db.session.execute(select(func.count()).select_from(ParkingRecord))
                c.db.remove()
            broadcaster._catch_up=read_batch
            broadcaster._v2[object()]=0
            await broadcaster._broadcast_event({},None)
            assert c.db.session is publisher
            assert publisher.in_transaction()
        finally:
            c.db.remove();request_scope.reset(key)
    asyncio.run(exercise())
