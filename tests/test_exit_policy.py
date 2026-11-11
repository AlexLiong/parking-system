from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from server.core.events import VehicleArrived,FeeQuoted,PlateRecognized
from server.core.domain import PlateColor,RecognitionResult
from server.core.timeutil import utcnow
from server.core.errors import ValidationError
from server.repository.models import ParkingRecord,PaymentRecord


def arrived(c,vid='missing-exit',plate='京A12345'):
    c.access.recognizer=Mock()
    c.access.recognizer.recognize.return_value=RecognitionResult(plate=plate,
        color=PlateColor.BLUE,overall_conf=.99,success=True,need_manual=False)
    c.access.on_vehicle_arrived(VehicleArrived(vehicle_id=vid,gate='out',image_path='frame.jpg'))


def test_missing_exit_is_manual_then_max_duration_settles(container):
    """实现 A：无入场记录不静默 auto → 待办人工选择 max_duration 后实际入账并出场。"""
    c=container;c.cfg.access.exit_no_record_policy='max_duration';c.cfg.access.temp_max_hours=24
    quotes=[];c.bus.subscribe(FeeQuoted,quotes.append)
    free=c.space_repo.free_total()
    arrived(c)
    pending=c.access.list_pending()[0]
    assert pending['reason']=='exit_without_record'
    assert c.parking_repo.find_open_by_plate('京A12345') is None   # 不静默建单
    assert c.access.barriers['out'].state.value=='CLOSED'          # 不静默开闸
    assert c.space_repo.free_total()==free
    result=c.access.confirm_pending(pending['id'],'京A12345','admin',
                                    exit_action='max_duration',reason='无记录按临时处置')
    settlement=result['settlement']
    rec=c.parking_repo.get(settlement['record_id'])
    assert rec.temporary and rec.space_no is None and not rec.manual_entry
    payment=c.payment_repo.get_by_parking(rec.id)
    assert payment is not None and rec.status=='out'
    assert c.space_repo.free_total()==free and c.guard.reconcile()['consistent']
    assert c.stats.stats('day').count_in==0 and c.stats.stats('day').peak_occupancy==0
    c.access.confirm_pending(pending['id'],'京A12345','admin',
                             exit_action='max_duration',reason='重试')
    assert c.db.session.scalar(select(func.count()).select_from(ParkingRecord))==1


def test_free_missing_exit_manual_confirm_writes_zero_payment(container):
    """实现 A：无记录 free 必须人工确认后走临时账单（0 元）放行。"""
    c=container;c.cfg.access.exit_no_record_policy='free'
    arrived(c)
    pending=c.access.list_pending()[0]
    assert c.db.session.scalar(select(func.count()).select_from(ParkingRecord))==0
    result=c.access.confirm_pending(pending['id'],'京A12345','admin',
                                    exit_action='free',reason='无记录免费放行')
    rec=c.parking_repo.get(result['settlement']['record_id'])
    pay=c.payment_repo.get_by_parking(rec.id)
    assert rec.temporary and rec.status=='out'
    assert pay.amount_paid==0 and pay.method=='free'
    assert c.access._pending['out']==1
    assert c.stats.stats('day').revenue==0


@pytest.mark.parametrize('action',['max_duration','free','void','manual_time'])
def test_manual_missing_exit_dispositions(container,action):
    c=container;c.cfg.access.exit_no_record_policy='manual'
    arrived(c)
    pending=c.access.list_pending()[0]
    assert pending['reason']=='exit_without_record' and pending['source_plate']=='京A12345'
    assert c.parking_repo.find_open_by_plate('京A12345') is None
    result=c.access.confirm_pending(pending['id'],'京A12345','admin',exit_action=action,
        reason='已核对原始凭证',entry_time=utcnow()-timedelta(hours=2) if action=='manual_time' else None)
    assert result['status']=='confirmed'
    records=c.db.session.execute(select(ParkingRecord)).scalars().all()
    assert len(records)==1 and records[0].temporary
    payment=c.payment_repo.get_by_parking(records[0].id)
    # 实现 A：所有人工异常出口（含 max_duration 临时处置）都必须实际结算，禁止只 quote
    assert payment is not None
    assert records[0].status==('abnormal' if action=='void' else 'out')
    c.access.confirm_pending(pending['id'],'京A12345','admin',exit_action=action,reason='重试')
    assert c.db.session.scalar(select(func.count()).select_from(ParkingRecord))==1


@pytest.mark.parametrize('action',['charge','free','void'])
def test_manual_existing_accounting_matches_income_and_releases(container,action):
    c=container
    entry=c.access.manual_register('京A12345','in','admin')
    rec=c.parking_repo.get(entry['record_id'])
    with c.db.scope():rec.entry_time=utcnow()-timedelta(hours=2)
    due=c.billing.quote('京A12345').amount_due
    result=c.access.manual_exit('京A12345','admin','人工核实',action=action,record_id=rec.id)
    pay=c.payment_repo.get_by_parking(rec.id)
    assert pay.amount_due==due
    assert pay.amount_paid==(due if action=='charge' else 0)
    assert c.stats.stats('day').revenue==pay.amount_paid
    assert c.space_repo.find_by_plate('京A12345') is None
    retry=c.access.manual_exit('京A12345','admin','人工核实',action=action,record_id=rec.id)
    assert retry['payment_id']==result['payment_id'] and c.access._pending['out']==1


def test_manual_missing_request_id_and_bad_amount_fail_closed(client,admin_token):
    key=uuid4().hex
    args={'plate':'京A12345','action':'free','reason':'放行审核','request_id':key}
    r=client.post('/api/manual/exit',headers=admin_token,json=args)
    assert r.status_code==200,r.text
    retry=client.post('/api/manual/exit',headers=admin_token,json=args)
    assert retry.json()==r.json()
    assert client.post('/api/manual/exit',headers=admin_token,json={**args,'reason':'different'}).status_code==409
    assert client.post('/api/pay',headers=admin_token,json={'record_id':r.json()['record_id'],'amount_override':-1}).status_code==422
    assert client.post('/api/manual/exit',headers=admin_token,json={'plate':'京B12345','action':'free'}).status_code==422


def test_real_confidence_is_preserved_on_success(container):
    c=container;events=[];c.bus.subscribe(PlateRecognized,events.append)
    c.access.recognizer=Mock()
    c.access.recognizer.recognize.return_value=RecognitionResult(plate='京A12345',color=PlateColor.BLUE,
        overall_conf=.9823,success=True,need_manual=False)
    c.access.on_vehicle_arrived(VehicleArrived(vehicle_id='confidence',gate='in',image_path='frame.jpg'))
    assert events[-1].overall_conf==.9823
