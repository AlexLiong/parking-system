"""Durable session reset and replay behavior against an isolated database."""
from __future__ import annotations

from uuid import uuid4
import sqlite3

import pytest

from server.core.events import SpaceAssigned, SpaceUpdate
from server.core.timeutil import utcnow
from server.repository.db import Database
from server.repository.models import BarrierAction, BarrierStateRow, DemoSession


def _token(client):
    response = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin123"})
    assert response.status_code == 200
    return response.json()["token"]


def test_reset_is_atomic_idempotent_and_preserves_historical_records(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    c = client.app.state.container
    old = c.state.current_session().id
    with c.db.scope():
        c.space_repo.occupy("A-01", "京A12345")
        active = c.parking_repo.create(plate="京A12345", plate_color="blue",
                                       entry_time=utcnow(), space_no="A-01")
        c.space_repo.occupy("B-01", "京B12345")
        historical = c.parking_repo.create(plate="京B12345", plate_color="blue",
                                           entry_time=utcnow(), space_no="B-01")
        historical.session_id = None
    assert active.session_id == old
    before = client.get("/api/state/snapshot", headers=headers).json()
    assert before["layout"]["free_total"] == 78
    request = {"request_id": uuid4().hex, "mode": "soft", "reason": "test"}
    response = client.post("/api/demo/reset", json=request, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["closed_session"] == old
    assert result["new_session"] != old
    report = result["reset_report"]
    assert {k: report[k] for k in ("closed_records", "released_spaces",
                                   "resolved_incidents", "purged_records")} == {
        "closed_records": 1, "released_spaces": 1,
        "resolved_incidents": 0, "purged_records": 0}
    assert report["force"] is False
    assert report["aborted_records"] == report["closed_records"]
    assert report["rejected_pending"] == 0
    assert report["interrupted_barrier_actions"] == 0
    assert result["snapshot"]["layout"]["free_total"] == 79
    c.db.remove()
    assert c.parking_repo.get(active.id).status == "aborted"
    assert c.parking_repo.get(historical.id).status == "in"
    assert client.post("/api/demo/reset", json=request,
                       headers=headers).json() == result
    conflict = client.post("/api/demo/reset", json={**request, "mode": "hard"},
                           headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_conflict"
    events = client.get("/api/events?since=0", headers=headers).json()["items"]
    assert [item["name"] for item in events].count("DEMO_RESET") == 1


def test_reset_rejects_occupied_barrier_without_changes(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    c = client.app.state.container
    before = c.state.snapshot()
    c.access.barriers["in"].zone_occupied = True
    response = client.post("/api/demo/reset", headers=headers, json={
        "request_id": uuid4().hex, "mode": "soft"})
    assert response.status_code == 409
    assert response.json()["error"] == "reset_unsafe"
    after = c.state.snapshot()
    assert after["session"] == before["session"]
    assert after["seq"] == before["seq"]


def test_reset_rejects_pending_barrier_action(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    c = client.app.state.container
    c.access.manual_open("in", "admin", "test")
    before = c.state.snapshot()
    response = client.post("/api/demo/reset", headers=headers, json={
        "request_id": uuid4().hex, "mode": "soft"})
    assert response.status_code == 409
    assert response.json()["error"] == "reset_unsafe"
    assert c.state.snapshot()["session"] == before["session"]
    assert c.state.snapshot()["seq"] == before["seq"]


def test_hard_reset_purges_only_unpaid_session_records_and_resolves_its_faults(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    c = client.app.state.container
    old = c.state.current_session().id
    with c.db.scope():
        c.space_repo.occupy("A-01", "京A12345")
        demo = c.parking_repo.create(plate="京A12345", plate_color="blue",
                                     entry_time=utcnow(), space_no="A-01")
        fault = c.incident_repo.raise_incident(kind="camera", gate="in")
        c.space_repo.occupy("B-01", "京B12345")
        historical = c.parking_repo.create(plate="京B12345", plate_color="blue",
                                           entry_time=utcnow(), space_no="B-01")
        historical.session_id = None
    assert fault.session_id == old
    response = client.post("/api/demo/reset", headers=headers, json={
        "request_id": uuid4().hex, "mode": "hard"})
    assert response.status_code == 200, response.text
    report = response.json()["reset_report"]
    assert {k: report[k] for k in ("closed_records", "released_spaces",
                                   "resolved_incidents", "purged_records")} == {
        "closed_records": 1, "released_spaces": 1,
        "resolved_incidents": 1, "purged_records": 1}
    assert report["aborted_records"] == report["closed_records"]
    c.db.remove()
    assert c.parking_repo.get(demo.id) is None
    assert c.parking_repo.get(historical.id).status == "in"
    assert c.incident_repo.get(fault.id).status == "resolved"
    assert c.watchdog.status("in") is True


def test_event_rollback_and_two_client_replay_beyond_old_buffer(client):
    token = _token(client)
    c = client.app.state.container
    with pytest.raises(RuntimeError):
        with c.db.scope():
            c.bus.publish(SpaceUpdate(free_total=80))
            raise RuntimeError("rollback")
    assert c.state.events()["items"] == []

    for i in range(510):
        c.bus.publish(SpaceUpdate(free_total=i))
    assert c.state.events(limit=500)["has_more"]
    headers = {"sec-websocket-protocol": f"v2, token.{token}"}
    for _ in range(2):
        with client.websocket_connect("/ws?features=v2&since=1", headers=headers,
                                      subprotocols=["v2", f"token.{token}"]) as ws:
            welcome = ws.receive_json()
            assert welcome["protocol"] == 2
            events = []
            while len(events) < 509:
                message = ws.receive_json()
                events.extend(message["events"] if message["type"] == "batch"
                              else [message])
            assert len(events) == 509
            assert [item["seq"] for item in events] == list(range(2, 511))


def test_ws_stale_session_receives_new_snapshot(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    old = client.get("/api/state/snapshot", headers=headers).json()["session"]["id"]
    reset = client.post("/api/demo/reset", headers=headers, json={
        "request_id": uuid4().hex, "mode": "soft"})
    assert reset.status_code == 200
    seq = reset.json()["seq"]
    with client.websocket_connect(
            f"/ws?features=v2&since={seq}&session={old}",
            subprotocols=["v2", f"token.{token}"]) as ws:
        assert ws.receive_json()["type"] == "welcome"
        message = ws.receive_json()
        assert message["type"] == "snapshot"
        assert message["data"]["session"]["id"] == reset.json()["new_session"]


def test_entry_and_outbound_events_roll_back_together(client):
    c = client.app.state.container
    original = c.bus.outbound_handler

    def fail_on_assignment(event):
        if isinstance(event, SpaceAssigned):
            raise RuntimeError("outbox unavailable")
        original(event)

    c.bus.outbound_handler = fail_on_assignment
    try:
        with pytest.raises(RuntimeError, match="outbox unavailable"):
            c.access.manual_register("京A12345", "in", "admin")
    finally:
        c.bus.outbound_handler = original
    c.db.remove()
    assert c.parking_repo.find_open_by_plate("京A12345") is None
    assert c.space_repo.find_by_plate("京A12345") is None
    assert c.state.events()["items"] == []


def test_snapshot_and_events_require_authentication(client):
    assert client.get("/api/state/snapshot").status_code == 401
    assert client.get("/api/events").status_code == 401
    assert client.post("/api/demo/reset", json={"request_id": uuid4().hex}).status_code == 401


def test_snapshot_rejects_stale_session(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    old = client.get("/api/state/snapshot", headers=headers).json()["session"]["id"]
    reset = client.post("/api/demo/reset", headers=headers, json={
        "request_id": uuid4().hex, "mode": "soft"})
    assert reset.status_code == 200
    conflict = client.get(f"/api/state/snapshot?session={old}", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "session_changed"
    assert conflict.json()["detail"]["current_session"] == reset.json()["new_session"]
    current = reset.json()["new_session"]
    assert client.get(f"/api/state/snapshot?session={current}&since=0",
                      headers=headers).status_code == 200


def test_session_start_is_idempotent_and_active_session_is_unique(client):
    from sqlalchemy.exc import IntegrityError

    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    first = client.post("/api/demo/session", headers=headers, json={
        "request_id": uuid4().hex, "seed": 17})
    second = client.post("/api/demo/session", headers=headers, json={
        "request_id": uuid4().hex, "seed": 99})
    assert first.status_code == second.status_code == 200
    assert first.json()["session"] == second.json()["session"]
    c = client.app.state.container
    with pytest.raises(IntegrityError):
        with c.db.scope():
            c.db.session.add(DemoSession(id=uuid4().hex, started_at=utcnow(),
                                         status="active"))


def test_ws_v2_can_be_disabled_without_disabling_legacy_ws(client):
    from starlette.websockets import WebSocketDisconnect

    token = _token(client)
    c = client.app.state.container
    c.cfg.server.ws_v2 = False
    try:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws?features=v2",
                                          subprotocols=["v2", f"token.{token}"]):
                pass
        assert exc.value.code == 1013
        with client.websocket_connect("/ws", subprotocols=[f"token.{token}"]) as ws:
            assert ws.receive_json()["type"] == "welcome"
    finally:
        c.cfg.server.ws_v2 = True


def test_restart_recovers_session_runtime_and_event_cursor(cfg):
    from server.gateway.context import Container

    first = Container(cfg)
    try:
        first.bootstrap()
        first.start()
        old = first.state.ensure_session().id
        first.state.update_runtime(session_id=old, owner_id="browser-A", vehicles=[{
            "vehicle_id": "moving", "state": "approach",
            "pose": {"x": 1.0, "z": 2.0, "theta": 0.0}}])
        with first.db.scope():
            event = first.state.append_event("test_restart", {"ok": True})
            cursor = event.seq
    finally:
        first.close()

    second = Container(cfg)
    try:
        second.bootstrap()
        second.recovery.recover()
        second.start()
        snap = second.state.snapshot()
        assert snap["session"]["id"] == old
        assert snap["seq"] == cursor
        assert snap["vehicle_runtime"][0]["vehicle_id"] == "moving"
        assert second.state.events(0)["items"][-1]["seq"] == cursor
    finally:
        second.close()


def test_restart_marks_inflight_barrier_command_interrupted(cfg):
    from server.gateway.context import Container

    first = Container(cfg)
    try:
        first.bootstrap()
        first.start()
        with first.db.scope():
            first.db.session.add(BarrierStateRow(
                gate="in", state="OPENING", arm_position=0.4, pending=1,
                last_cmd_id="restart-command", updated_at=utcnow()))
            first.db.session.add(BarrierAction(
                cmd_id="restart-command", gate="in", cmd="open", reason="test",
                status="pending", created_at=utcnow()))
    finally:
        first.close()

    second = Container(cfg)
    try:
        second.bootstrap()
        second.recovery.recover()
        second.start()
        snap = second.state.snapshot()
        assert snap["barriers"]["in"]["state"] == "FAULT"
        assert snap["barriers"]["in"]["fault"] == "restart_during_motion"
        assert snap["barriers"]["in"]["pending"] == 0
        action = second.db.session.query(BarrierAction).filter_by(
            cmd_id="restart-command").one()
        assert action.status == "interrupted"
    finally:
        second.close()


def test_runtime_pose_is_snapshotted_and_cleared_on_reset(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    session_id = client.get("/api/state/snapshot", headers=headers).json()["session"]["id"]
    vehicle = {"vehicle_id": "car_01", "state": "to_space", "kind": "blue",
               "pose": {"x": 4.5, "z": -8.25, "theta": 1.2},
               "legs": [], "dist": 12.0}
    response = client.post("/sim/state", headers=headers, json={
        "session_id": session_id, "owner_id": "browser-A", "vehicles": [vehicle]})
    assert response.status_code == 200, response.text
    runtime = client.get("/api/state/snapshot", headers=headers).json()["vehicle_runtime"]
    assert runtime[0]["pose"] == vehicle["pose"]
    assert runtime[0]["owner_id"] == "browser-A"
    reset = client.post("/api/demo/reset", headers=headers, json={
        "request_id": uuid4().hex, "mode": "soft"})
    assert reset.status_code == 200, reset.text
    assert reset.json()["snapshot"]["vehicle_runtime"] == []
    stale = client.post("/sim/state", headers=headers, json={
        "session_id": session_id, "owner_id": "browser-A", "vehicles": [vehicle]})
    assert stale.status_code == 409


def test_barrier_ack_requires_matching_command_id_and_persists_action(client):
    token = _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    c = client.app.state.container
    c.access.manual_open("in", "admin", "test")
    actions = client.get("/api/barrier/actions?gate=in", headers=headers).json()["items"]
    command = next(row for row in actions if row["cmd"] == "open")
    cmd_id = command["cmd_id"]
    c.access.on_barrier_ack("in", "open", "wrong", require_cmd=True)
    assert c.access.barriers["in"].state.value == "OPENING"
    c.access.on_barrier_ack("in", "open", require_cmd=True)
    assert c.access.barriers["in"].state.value == "OPENING"
    c.access.on_barrier_ack("in", "open", cmd_id, require_cmd=True)
    assert c.access.barriers["in"].state.value == "KEEP_OPEN"
    assert client.get("/api/state/snapshot", headers=headers).json()["barriers"]["in"]["state"] == "KEEP_OPEN"
    actions = client.get("/api/barrier/actions?gate=in", headers=headers).json()["items"]
    assert next(row for row in actions if row["cmd_id"] == cmd_id)["status"] == "acked"
    assert client.get(f"/api/barrier/actions?since={actions[0]['id']}",
                      headers=headers).json()["items"] == []


def test_old_database_migration_creates_backup_and_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE parking_record (id INTEGER PRIMARY KEY, status TEXT, plate TEXT, space_no TEXT)")
    db = Database(path)
    try:
        db.init_schema()
        db.init_schema()
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(parking_record)")}
            assert {"session_id", "aborted_reason"} <= columns
        backups = list(tmp_path.glob("old.db.bak.*"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(parking_record)")}
            assert "session_id" not in columns
    finally:
        db.close()


def test_scheduler_queue_and_rng_persist_separately_from_physical_vehicles(client, admin_token):
    session=client.get('/api/state/snapshot',headers=admin_token).json()['session']['id']
    scheduler={'seed':42,'seq':8,'rng_state':123,'pending':[{'id':'Q8','kind':'green','spec':{'file':'car_sedan.glb','family':'sedan'}}]}
    body={'session_id':session,'owner_id':'driver','vehicles':[], 'scheduler':scheduler}
    assert client.post('/sim/state',headers=admin_token,json=body).status_code==200
    snap=client.get('/api/state/snapshot',headers=admin_token).json()
    assert snap['vehicle_runtime']==[]
    assert snap['scheduler_runtime'][0]['pending']==scheduler['pending']
    assert snap['scheduler_runtime'][0]['rng_state']==123
    assert client.post('/sim/state',headers=admin_token,json={**body,'vehicles':[{'vehicle_id':'v','pose':{'x':1}}]}).status_code==422


def test_only_one_dispatcher_can_spawn_per_session_and_reload_retains_owner(client, admin_token):
    session=client.get('/api/state/snapshot',headers=admin_token).json()['session']['id']
    body={'session_id':session,'owner_id':'driver'}
    assert client.post('/sim/dispatcher',headers=admin_token,json=body).status_code==200
    assert client.post('/sim/dispatcher',headers=admin_token,json={**body,'owner_id':'observer'}).status_code==409
    assert client.post('/sim/state',headers=admin_token,json={**body,'vehicles':[]}).status_code==200
    assert client.post('/sim/dispatcher',headers=admin_token,json=body).status_code==200
    assert client.post('/sim/dispatcher',headers=admin_token,json={**body,'owner_id':'observer'}).status_code==409


def test_multi_owner_sensor_occupancy_cannot_be_cleared_by_observer(client, admin_token):
    session=client.get('/api/state/snapshot',headers=admin_token).json()['session']['id']
    def zone(owner,occupied,sid=session):
        return client.post('/sim/events/zone',headers=admin_token,json={'zone_id':'in_barrier','occupied':occupied,'owner_id':owner,'session_id':sid})
    assert zone('driver',True).status_code==200
    assert zone('observer',False).status_code==200
    assert client.app.state.container.access.barriers['in'].zone_occupied
    assert zone('driver',False,'old').status_code==409
    assert client.app.state.container.access.barriers['in'].zone_occupied
    assert zone('driver',False).status_code==200
    assert not client.app.state.container.access.barriers['in'].zone_occupied


def test_paid_exit_snapshot_restores_only_relevant_runtime_authorizations(client, admin_token):
    from server.core.events import PaymentCompleted
    from server.gateway.ws_realtime import event_to_message
    event=PaymentCompleted(record_id=7,plate='京A12345',amount=0,method='free',vehicle_id='driver-car')
    payload=event_to_message(event)
    assert payload['type']=='payment_completed' and payload['vehicle_id']=='driver-car' and payload['record_id']==7
    c=client.app.state.container
    with c.db.scope():
        c.space_repo.occupy('A-01','京A12345')
        rec=c.parking_repo.create(plate='京A12345',plate_color='blue',space_no='A-01',entry_time=utcnow())
        rec.vehicle_id='driver-car'
    c.billing.pay(rec.id,method='free',amount_override=0)
    session=c.state.current_session().id
    c.state.update_runtime(session_id=session,owner_id='driver',vehicles=[{
        'vehicle_id':'driver-car','pose':{'x':3.5,'z':30,'theta':3.14}}])
    snap=c.state.snapshot()
    assert snap['paid_exits']==[{'record_id':rec.id,'plate':'京A12345','vehicle_id':'driver-car'}]
    c.state.update_runtime(session_id=session,owner_id='driver',vehicles=[])
    assert c.state.snapshot()['paid_exits']==[]


def test_gate_display_snapshot_is_scoped_and_ordered(client, admin_token):
    c = client.app.state.container
    with c.db.scope():
        c.state.append_event('fee_quote', {'type': 'fee_quote', 'plate': '京A12345', 'amount': 5, 'minutes': 30})
        c.state.append_event('payment_completed', {'type': 'payment_completed', 'plate': '京A12345', 'amount': 5})
    headers = admin_token
    response = client.get('/api/state/snapshot', headers=headers)
    assert response.status_code == 200
    events = response.json()['display_events']
    assert [e['data']['type'] for e in events][-2:] == ['fee_quote', 'payment_completed']
    session = response.json()['session']['id']
    assert all(e['session_id'] == session for e in events)


def test_old_database_migration_skips_statistics_indexes_for_missing_columns(tmp_path):
    """P1-13：极简老库缺 entry_time/exit_time 时，迁移只建可建索引，不得生成表达式垃圾索引。"""
    path = tmp_path / "minimal-old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE parking_record (id INTEGER PRIMARY KEY, status TEXT, plate TEXT, space_no TEXT)")
    db = Database(path)
    try:
        db.init_schema()
        db.init_schema()  # 幂等
        with sqlite3.connect(path) as conn:
            indexes = {row[1] for row in conn.execute("PRAGMA index_list('parking_record')")}
        # 缺列索引必须跳过（对照：正常库见 tests/test_statistics.py 的索引存在断言）
        assert 'idx_exit_time' not in indexes
        assert 'idx_entry_cover' not in indexes
        assert 'idx_exit_cover' not in indexes
    finally:
        db.close()
