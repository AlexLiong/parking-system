"""异常处理与恢复测试（recovery/watchdog.py, recovery/startup.py, access 断连）。"""
from __future__ import annotations

from datetime import timedelta

from server.core.domain import BarrierState, FeeBreakdown
from server.core.timeutil import utcnow


# ----------------------------- 摄像头看门狗 -----------------------------
def test_watchdog_low_confidence_does_not_raise_camera_fault(container):
    streak = container.cfg.recovery.camera_fail_streak
    for _ in range(streak):
        container.watchdog.feed("in", success=False, image_present=True)
    assert container.watchdog.down("in") is False
    assert container.watchdog.status("in") is True


def test_watchdog_raises_after_source_fail_streak(container):
    streak = container.cfg.recovery.camera_fail_streak
    for _ in range(streak):
        container.watchdog.feed("in", success=False, image_present=False)
    assert container.watchdog.down("in") is True
    assert container.watchdog.status("in") is False
    assert container.incidents.has_open("camera", "in")


def test_watchdog_success_resets_streak(container):
    streak = container.cfg.recovery.camera_fail_streak
    for _ in range(streak - 1):
        container.watchdog.feed("in", success=False, image_present=False)
    container.watchdog.feed("in", success=True)     # 成功清零
    for _ in range(streak - 1):
        container.watchdog.feed("in", success=False, image_present=False)
    assert container.watchdog.down("in") is False


def test_watchdog_recover_clears_incident(container):
    container.watchdog.inject_fault("out", reason="test")
    assert container.watchdog.down("out")
    container.watchdog.recover("out", operator="admin")
    assert container.watchdog.down("out") is False
    assert not container.incidents.has_open("camera", "out")


# ----------------------------- 启动恢复 -----------------------------
def test_recover_fixes_half_transaction(container):
    plate = "琼A12345"
    entry = utcnow() - timedelta(hours=2)
    with container.db.scope():
        rec = container.parking_repo.create(
            plate=plate, plate_color="blue", entry_time=entry,
            space_no="A-01", status="in")
        container.space_repo.occupy("A-01", plate)
        # 已收费但记录仍在场（断电/崩溃遗留）
        container.payment_repo.create(
            parking_record_id=rec.id, plate=plate, minutes=120, billable_units=2,
            amount_due=9.0, amount_paid=9.0, method="mock_scan", operator="admin",
            breakdown=FeeBreakdown(minutes=120, amount_due=9.0))
        rid = rec.id

    report = container.recovery.recover()
    assert report.fixed_half_transactions == 1
    rec2 = container.parking_repo.get(rid)
    assert rec2.status == "out" and rec2.exit_time is not None
    assert container.space_repo.get("A-01").status == "free"
    # 半截交易登记 power 异常
    assert container.incidents.has_open("power")


def test_recover_releases_orphan_occupied_space(container):
    with container.db.scope():
        container.space_repo.occupy("B-05", "琼A77777")   # 无对应在场记录
    report = container.recovery.recover()
    assert report.space_reconciled >= 1
    assert container.space_repo.get("B-05").status == "free"


def test_recover_resets_barriers_to_closed(container):
    # 先把道闸推到 OPENING，再恢复
    container.access.barriers["in"].request_open("test")
    assert container.access.barriers["in"].state == BarrierState.OPENING
    container.recovery.recover()
    assert container.access.barriers["in"].state == BarrierState.CLOSED
    assert container.access.barriers["out"].state == BarrierState.CLOSED


def test_recover_rebuilds_occupancy_from_record(container):
    # 在场记录存在但车位未占用 → 以记录为真相源重建占用
    plate = "琼A55555"
    with container.db.scope():
        container.parking_repo.create(plate=plate, plate_color="blue",
                                      entry_time=utcnow(), space_no="C-03", status="in")
    container.recovery.recover()
    sp = container.space_repo.get("C-03")
    assert sp.status == "occupied" and sp.current_plate == plate


# ----------------------------- 断连安全侧 -----------------------------
def test_disconnect_locks_safe_and_reconnect_resets(container):
    container.access.barriers["in"].request_open("entry")
    container.access.barriers["in"].on_ack("open")
    container.access.on_disconnect()
    assert container.access.barriers["in"].state == BarrierState.KEEP_OPEN
    container.access.on_reconnect_reset()
    assert container.access.barriers["in"].state == BarrierState.CLOSED
