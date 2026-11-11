"""Vehicle list benefits must affect access, billing, and accounting."""
from datetime import timedelta
import sqlite3
from unittest.mock import Mock

import pytest

from server.core.domain import PlateColor, RecognitionResult
from server.core.errors import DataAnomaly
from server.core.events import IncidentRaised, PlateRecognized, VehicleArrived
from server.core.timeutil import utcnow
from server.repository.db import Database
from server.repository.models import ParkingRecord


def _list(c, plate, list_type, valid_until=None):
    with c.db.scope():
        c.list_repo.upsert(plate=plate, list_type=list_type,
                           valid_until=valid_until)


def test_blacklist_blocks_entry_but_expired_blacklist_does_not(container):
    _list(container, "京A12345", "black", utcnow() + timedelta(days=1))
    with pytest.raises(DataAnomaly):
        container.access.manual_register("京A12345", "in", "admin")
    assert container.parking_repo.find_open_by_plate("京A12345") is None
    assert container.access._pending["in"] == 0

    _list(container, "京A12345", "black", utcnow() - timedelta(seconds=1))
    result = container.access.manual_register("京A12345", "in", "admin")
    assert result["record_id"] > 0


def test_list_expires_at_exact_boundary(container):
    boundary = utcnow() + timedelta(hours=1)
    _list(container, "京A12345", "black", boundary)
    assert container.list_repo.active_type("京A12345", boundary) is None
    assert container.list_repo.active_type("京A12345", boundary - timedelta(microseconds=1)) == "black"


def test_blacklisted_recognition_emits_rejection_without_manual_override(container):
    _list(container, "京A12345", "black")
    incidents, recognized = [], []
    container.bus.subscribe(IncidentRaised, incidents.append)
    container.bus.subscribe(PlateRecognized, recognized.append)
    container.access.recognizer = Mock()
    container.access.recognizer.recognize.return_value = RecognitionResult(
        plate="京A12345", color=PlateColor.BLUE, overall_conf=0.99,
        success=True, need_manual=False)
    container.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="blocked-car", gate="in", image_path="frame.jpg"))
    assert container.parking_repo.find_open_by_plate("京A12345") is None
    assert container.access.list_pending() == []
    assert incidents[-1].detail["reason"] == "blacklist"
    assert recognized[-1].reason == "blacklist"


def test_whitelist_snapshot_is_free_after_list_change_and_auto_exit(container):
    _list(container, "京A12345", "white", utcnow() + timedelta(days=1))
    entry = container.access.manual_register("京A12345", "in", "admin")
    rec = container.parking_repo.get(entry["record_id"])
    with container.db.scope():
        rec.entry_time = utcnow() - timedelta(hours=2)
        container.list_repo.remove("京A12345")
        container.list_repo.upsert(plate="京A12345", list_type="black")
    assert rec.benefit_type == "white"
    assert container.billing.quote("京A12345").amount_due == 0

    # 恢复白名单（避免出口识别按黑名单 0 收费异常路径；仍验证入场收益快照免费）
    with container.db.scope():
        container.list_repo.remove("京A12345")
        container.list_repo.upsert(plate="京A12345", list_type="white")
    container.access.recognizer = Mock()
    container.access.recognizer.recognize.return_value = RecognitionResult(
        plate="京A12345", color=PlateColor.BLUE, overall_conf=0.99,
        success=True, need_manual=False)
    container.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="white-car", gate="out", image_path="frame.jpg"))
    assert rec.status == "out"
    payment = container.payment_repo.get_by_parking(rec.id)
    assert payment.amount_paid == 0
    assert payment.method == "auto"
    assert container.stats.stats("day").revenue == 0


def test_monthly_expiry_and_manual_exit_confirmation(container):
    _list(container, "京AD12345", "monthly", utcnow() - timedelta(seconds=1))
    expired = container.access.manual_register("京AD12345", "in", "admin", "green")
    ordinary = container.parking_repo.get(expired["record_id"])
    with container.db.scope():
        ordinary.entry_time = utcnow() - timedelta(hours=2)
    assert ordinary.benefit_type is None
    assert container.billing.quote("京AD12345").amount_due > 0

    _list(container, "京B12345", "monthly", utcnow() + timedelta(days=1))
    active = container.access.manual_register("京B12345", "in", "admin")
    rec = container.parking_repo.get(active["record_id"])
    with container.db.scope():
        rec.entry_time = utcnow() - timedelta(hours=2)
    assert rec.benefit_type == "monthly"
    assert container.billing.quote("京B12345").amount_due == 0
    container.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="monthly-exit", gate="out"))
    pending = container.access.list_pending()[0]
    container.access.confirm_pending(pending["id"], "京B12345", "admin")
    # 实现 A：出口待办确认即实际结算（月租免费）并出场/释放车位
    rec = container.parking_repo.get(rec.id)
    assert rec.status == "out"
    payment = container.payment_repo.get_by_parking(rec.id)
    assert payment is not None
    assert payment.amount_paid == 0
    assert payment.method in ("monthly", "free")


def test_list_api_rejects_invalid_type_plate_and_date(client, admin_token):
    for payload in (
        {"plate": "京A12345", "list_type": "vip"},
        {"plate": "京O12345", "list_type": "black"},
        {"plate": "京A12345", "list_type": "white", "valid_until": "bad-date"},
    ):
        response = client.post("/api/lists", headers=admin_token, json=payload)
        assert response.status_code == 422


def test_free_benefit_cannot_be_overridden_to_charge(container):
    _list(container, "京A12345", "white")
    entry = container.access.manual_register("京A12345", "in", "admin")
    with pytest.raises(Exception) as error:
        container.billing.pay(entry["record_id"], amount_override=5)
    assert getattr(error.value, "code", None) == "validation_error"
    assert container.payment_repo.get_by_parking(entry["record_id"]) is None


def test_legacy_parking_db_is_backed_up_before_benefit_migration(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    db.init_schema()
    with db.scope():
        db.session.add(ParkingRecord(
            plate="京A12345", plate_color="blue", entry_time=utcnow(),
            status="out", created_at=utcnow()))
    db.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE parking_record DROP COLUMN benefit_type")

    db = Database(path)
    try:
        db.init_schema()
        backup_files = list(tmp_path.glob("legacy.db.bak.*"))
        assert len(backup_files) == 1
        with sqlite3.connect(backup_files[0]) as backup:
            old_columns = {row[1] for row in backup.execute(
                "PRAGMA table_info(parking_record)")}
        assert "benefit_type" not in old_columns
        assert db.session.get(ParkingRecord, 1).benefit_type is None
    finally:
        db.close()
