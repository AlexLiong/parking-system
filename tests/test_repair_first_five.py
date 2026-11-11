"""Regression tests for the current first-five repair tasks (2026-10-23).

Covers:
- P1-07: camera recovery retries waiting vehicles (and does not retry while down);
- P1-09: temporary out-of-record bills are excluded from count/entry checks;
- P1-09/P2-14: manual_time updates an existing open record instead of being ignored;
- P1-05: pending deadlines are frozen while the simulation is paused;
- expected-match consistency: recognition_log.success is true after sim expected match.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from server.core.domain import PlateColor, RecognitionResult
from server.core.errors import StateConflict
from server.core.events import VehicleArrived
from server.core.timeutil import utcnow


class FakeRecognizer:
    def __init__(self, plate="京A12345", conf=0.86):
        self.plate = plate
        self.conf = conf
        self.calls = 0

    def _result(self):
        return RecognitionResult(
            plate=self.plate, color=PlateColor.BLUE, overall_conf=self.conf,
            success=False, need_manual=True, candidates=[self.plate],
            source_status="ok")

    def recognize(self, image_path, **kwargs):
        self.calls += 1
        return self._result()

    def recognize_bgr(self, image, **kwargs):
        self.calls += 1
        return self._result()


def _arrive(c, vehicle_id="cam-1", expected="京A12345"):
    c.bus.publish(VehicleArrived(
        vehicle_id=vehicle_id, gate="in", image_path="frames/cam.png",
        expected_plate=expected, source="sim_camera"))


def test_camera_recovery_retries_camera_fault_pending(container):
    c = container
    c.cfg.lpr.sim_conf_threshold = 0.85
    c.access.recognizer = FakeRecognizer(conf=0.86)
    c.watchdog.inject_fault("in", reason="test")
    _arrive(c)
    pending = c.access.list_pending()
    assert len(pending) == 1 and pending[0]["reason"] == "camera_fault"
    assert c.parking_repo.count_open() == 0

    # recovery must first clear the camera fault, then retry the waiting event
    c.watchdog.recover("in", operator="admin")
    result = c.access.retry_waiting_vehicles("in", operator="admin")
    assert result["ok"] is True and result["retried"] == 1
    assert c.parking_repo.count_open() == 1
    assert c.parking_repo.find_open_by_plate("京A12345") is not None
    # old pending is rejected for audit, not left pending
    old = c.pending_repo.get(pending[0]["id"])
    assert old.status == "rejected"
    assert not [p for p in c.access.list_pending() if p["vehicle_id"] == "cam-1"]


def test_camera_recovery_retry_refuses_while_down(container):
    c = container
    c.watchdog.inject_fault("in", reason="test")
    result = c.access.retry_waiting_vehicles("in", operator="admin")
    assert result["ok"] is False and result["retried"] == 0
    assert result["reason"] == "camera_down"


def test_temporary_record_is_not_open_count_or_entry_blocker(container):
    c = container
    c.parking_repo.create(
        plate="京T12345", plate_color="blue", entry_time=utcnow() - timedelta(hours=1),
        space_no=None, temporary=True)
    assert c.parking_repo.count_open() == 0
    assert c.parking_repo.find_open_by_plate(
        "京T12345", include_temporary=False) is None
    # temporary bill must not block a real entry for the same plate
    c.access.manual_register("京T12345", "in", "admin")
    assert c.parking_repo.find_open_by_plate("京T12345") is not None
    assert c.parking_repo.count_open() == 1


def test_manual_time_updates_existing_entry_time_and_bill(container):
    c = container
    entry = c.access.manual_register("京M12345", "in", "admin")
    rec = c.parking_repo.get(entry["record_id"])
    with c.db.scope():
        rec.entry_time = utcnow() - timedelta(hours=4)
    before = c.billing.quote("京M12345").amount_due
    new_entry = utcnow() - timedelta(hours=1)
    result = c.access.manual_exit(
        "京M12345", "admin", "核实入场时间", action="manual_time",
        record_id=rec.id, entry_time=new_entry)
    assert result["record_id"] == rec.id
    assert abs((rec.entry_time - new_entry).total_seconds()) < 5
    payment = c.payment_repo.get_by_parking(rec.id)
    assert payment is not None
    assert payment.amount_due < before


def test_manual_time_rejects_closed_record(container):
    c = container
    entry = c.access.manual_register("京N12345", "in", "admin")
    rec = c.parking_repo.get(entry["record_id"])
    c.access.manual_exit("京N12345", "admin", "正常结算", action="charge",
                         record_id=rec.id)
    with pytest.raises(StateConflict):
        c.access.manual_exit("京N12345", "admin", "再改时间", action="manual_time",
                             record_id=rec.id, entry_time=utcnow() - timedelta(hours=1))


def test_paused_simulation_freezes_pending_expiry(container):
    c = container
    c.cfg.server.demo_mode = True
    # create a pending item with a tiny deadline
    c.access.recognizer = None
    c.access.on_vehicle_arrived(VehicleArrived(vehicle_id="pause-1", gate="in", image_path=None))
    pending = c.access.list_pending()[0]
    row = c.pending_repo.get(pending["id"])
    with c.db.scope():
        row.expires_at = utcnow() + timedelta(seconds=1)

    c.simulation.control(paused=True)
    # force wall clock past the deadline while paused
    with c.db.scope():
        row.expires_at = utcnow() - timedelta(seconds=2)
    assert c.access.expire_pending() == 0
    assert c.pending_repo.get(pending["id"]).status == "pending"

    c.simulation.control(paused=False)
    # after unpause, the paused duration is added back; it must still be pending
    assert c.pending_repo.get(pending["id"]).status == "pending"
    with c.db.scope():
        c.pending_repo.get(pending["id"]).expires_at = utcnow() - timedelta(seconds=1)
    assert c.access.expire_pending() == 1


def test_expected_match_marks_recognition_log_success(container):
    c = container
    c.cfg.lpr.sim_conf_threshold = 0.85
    c.access.recognizer = FakeRecognizer(conf=0.86)
    _arrive(c, vehicle_id="log-1", expected="京A12345")
    logs = c.recog_repo.recent(5)
    assert logs
    assert logs[0]["plate"] == "京A12345"
    assert logs[0]["success"] is True


def test_camera_recover_api_retries_and_returns_retry_summary(client, admin_token):
    """HTTP 层的 P1-07 闭环：recover 接口自动重试等待车辆。"""
    c = client.app.state.container
    c.cfg.lpr.sim_conf_threshold = 0.85
    c.access.recognizer = FakeRecognizer(conf=0.86)
    assert client.post("/api/camera/in/inject", headers=admin_token,
                       json={"reason": "test"}).status_code == 200
    c.bus.publish(VehicleArrived(
        vehicle_id="api-cam-1", gate="in", image_path="frames/api.png",
        expected_plate="京A12345", source="sim_camera"))
    assert c.access.list_pending()
    response = client.post("/api/camera/in/recover", headers=admin_token,
                           json={"resolution": "已恢复"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["down"] is False
    assert body["retry"]["retried"] == 1
    assert c.parking_repo.find_open_by_plate("京A12345") is not None
