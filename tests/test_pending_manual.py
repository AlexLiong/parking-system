"""Durable manual review for uncertain gate recognition."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from server.core.domain import PlateColor, RecognitionResult
from server.core.events import VehicleArrived
from server.core.timeutil import utcnow
from server.gateway.context import Container
from server.repository.models import ParkingRecord


def _arrive(c, vehicle_id, gate="in", image_path=None):
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id=vehicle_id, gate=gate, image_path=image_path))
    items = c.access.list_pending()
    assert items
    return items[0]


def test_pending_survives_restart_and_confirm_is_idempotent(cfg):
    c = Container(cfg)
    c.bootstrap()
    c.start()
    pending = _arrive(c, "car-1")
    assert pending["reason"] == "camera_missing"
    c.close()

    c = Container(cfg)
    c.bootstrap()
    c.start()
    try:
        assert c.access.list_pending()[0]["id"] == pending["id"]
        result = c.access.confirm_pending(pending["id"], "京A12345", "admin")
        assert result["status"] == "confirmed"
        assert c.access._pending["in"] == 1
        assert c.parking_repo.find_open_by_plate("京A12345") is not None
        assert c.access.confirm_pending(pending["id"], "京A12345", "admin") == result
        count = c.db.session.scalar(select(func.count()).select_from(ParkingRecord))
        assert count == 1
        assert c.access._pending["in"] == 1
    finally:
        c.close()


def test_failed_confirm_keeps_pending_and_gate_closed(container):
    first = _arrive(container, "car-1")
    container.access.confirm_pending(first["id"], "京A12345", "admin")
    second = _arrive(container, "car-2")
    before = container.access._pending["in"]
    try:
        container.access.confirm_pending(second["id"], "京A12345", "admin")
        assert False, "duplicate plate must be rejected"
    except Exception as exc:
        assert getattr(exc, "code", None) == "duplicate_entry"
    assert container.access.list_pending()[0]["id"] == second["id"]
    assert container.access._pending["in"] == before


def test_reject_and_expiry_never_authorize(container):
    rejected = _arrive(container, "reject-me")
    assert container.access.reject_pending(rejected["id"], "admin")["status"] == "rejected"
    expiring = _arrive(container, "expire-me")
    row = container.pending_repo.get(expiring["id"])
    with container.db.scope():
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert container.access.expire_pending() == 1
    assert container.pending_repo.get(expiring["id"]).status == "expired"
    assert container.access._pending["in"] == 0
    assert container.db.session.scalar(select(func.count()).select_from(ParkingRecord)) == 0


def test_exit_confirm_settles_and_authorizes_gate(container):
    """实现 A：出口待办确认必须实际结算（禁止只 quote），标 out/释放/开出口。"""
    from server.core.events import ExitAuthorized

    container.access.manual_register("京A12345", "in", "admin")
    container.access.recognizer = Mock()
    container.access.recognizer.recognize.return_value = RecognitionResult(
        plate="京A12345", color=PlateColor.BLUE, overall_conf=0.2,
        success=True, need_manual=True, candidates=["京A12345"])
    authorized = []
    container.bus.subscribe(ExitAuthorized, authorized.append)
    pending = _arrive(container, "car-out", gate="out", image_path="frame.jpg")
    result = container.access.confirm_pending(pending["id"], "京A12345", "admin")
    assert result["status"] == "confirmed"
    settlement = result["settlement"]
    assert settlement["payment_id"] and settlement["status"] == "out"
    rec = container.parking_repo.get(settlement["record_id"])
    assert rec.status == "out" and rec.exit_time is not None
    assert container.payment_repo.get_by_parking(rec.id) is not None
    assert container.parking_repo.find_open_by_plate("京A12345") is None
    assert container.access._pending["out"] == 1          # 已授权开出口
    assert authorized and authorized[0].plate == "京A12345"
    log = container.recog_repo.get(pending["recognition_log_id"])
    assert log["plate"] == "京A12345"
    assert log["corrected_plate"] == "京A12345"


def test_reset_closes_old_session_pending(client, admin_token):
    c = client.app.state.container
    pending = _arrive(c, "car-1")
    response = client.post("/api/demo/reset", headers=admin_token, json={
        "request_id": "close-pending", "mode": "soft", "new_session": True})
    assert response.status_code == 200, response.text
    assert c.pending_repo.get(pending["id"]).status == "rejected"
    assert client.get("/api/manual/pending", headers=admin_token).json()["items"] == []


def test_after_commit_callbacks_can_write_and_rollback_drops_them(container):
    calls = []
    with container.db.scope():
        with container.db.scope():
            container.db.on_commit(lambda: calls.append(container.db.in_scope))
        assert calls == []
    assert calls == [False]

    def write_after_commit():
        with container.db.scope():
            container.oplog_repo.add(operator="test", action="callback_check")

    with container.db.scope():
        container.db.on_commit(write_after_commit)
    assert any(row["action"] == "callback_check"
               for row in container.oplog_repo.recent())

    with pytest.raises(RuntimeError):
        with container.db.scope():
            container.db.on_commit(lambda: calls.append(True))
            raise RuntimeError("rollback")
    assert calls == [False]


def test_confirmed_log_cannot_be_corrected_away_from_parking_record(client, admin_token):
    c = client.app.state.container
    c.access.recognizer = Mock()
    c.access.recognizer.recognize.return_value = RecognitionResult(
        plate="京A12345", color=PlateColor.BLUE, overall_conf=0.2,
        success=True, need_manual=True, candidates=["京A12345"])
    pending = _arrive(c, "linked-car", image_path="frame.jpg")
    client.cookies.clear()
    assert client.get("/api/manual/pending").status_code == 401
    confirmed = client.post("/api/manual/confirm", headers=admin_token,
                            json={"id": pending["id"], "plate": "京A12345"})
    assert confirmed.status_code == 200, confirmed.text
    conflict = client.post(
        f"/api/recognition-logs/{pending['recognition_log_id']}/correct",
        headers=admin_token, json={"plate": "京B12345"})
    assert conflict.status_code == 409
    assert c.parking_repo.find_open_by_plate("京A12345") is not None
    assert client.post("/api/manual/reject", headers=admin_token,
                       json={"id": pending["id"]}).status_code == 409
