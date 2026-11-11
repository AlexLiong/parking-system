"""修复 A（20261107）：M1 季度 CSV、H2 UTC+8 封顶、H3 授权回调失败、H4 VehicleLeft 收尾。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from server.billing.calculator import FeeCalculator
from server.core.domain import FeeBreakdown
from server.core.events import IncidentRaised, SpaceReleased, VehicleLeft
from server.repository.models import OpLog, PaymentRecord


# ---------------------------------------------------------------------------
# M1：quarter CSV 使用 months
# ---------------------------------------------------------------------------
def _mk_complete(container, plate, entry, exit_, amount=5.0, method="auto"):
    with container.db.scope():
        rec = container.parking_repo.create(
            plate=plate, plate_color="blue", entry_time=entry,
            space_no="A-01", status="in", entry_sim_sec=0.0)
        container.parking_repo.mark_exit(rec, exit_time=exit_, status="out")
        container.payment_repo.create(
            parking_record_id=rec.id, plate=plate, minutes=60, billable_units=1,
            amount_due=amount, amount_paid=amount, method=method,
            operator="system",
            breakdown=FeeBreakdown(minutes=60, amount_due=amount), paid_at=exit_)
        return rec.id


def test_quarter_csv_uses_months_and_matches_preview(container):
    c = container
    _mk_complete(c, "京A11111", datetime(2026, 4, 15, 2, 0),
                 datetime(2026, 4, 15, 3, 0), 5.0)
    _mk_complete(c, "京A22222", datetime(2026, 5, 10, 2, 0),
                 datetime(2026, 5, 10, 3, 0), 6.0)
    _mk_complete(c, "京A33333", datetime(2026, 6, 20, 2, 0),
                 datetime(2026, 6, 20, 3, 0), 7.0)
    preview = c.reports.preview("quarter", "2026-05-10")
    assert preview["period"] == "quarter" and preview["level"] == "month"
    months = preview["months"]
    assert [m["month"] for m in months] == ["2026-04", "2026-05", "2026-06"]
    text = c.reports.csv_text(preview)
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0] == "月份,进场车次,出场车次,营收(元)"
    assert len(lines) == 1 + len(months) == 4
    for line, month in zip(lines[1:], months):
        parts = line.split(",")
        assert parts[0] == month["month"]
        assert int(parts[1]) == month["count_in"]
        assert int(parts[2]) == month["count_out"]
        assert abs(float(parts[3]) - month["revenue"]) < 0.01
    # 季度 CSV 不应再出现周汇总列
    assert "周起始" not in text


# ---------------------------------------------------------------------------
# H2：每日封顶固定 UTC+8 自然日
# ---------------------------------------------------------------------------
def _rule(cap=10.0, first_hour=100.0, unit_price=100.0):
    return SimpleNamespace(free_minutes=0, first_hour_price=first_hour,
                           unit_minutes=30, unit_price=unit_price,
                           daily_cap=cap, ne_discount=1.0, round_up=True)


def _rec(entry):
    return SimpleNamespace(entry_time=entry, plate_color="blue", benefit_type=None)


def test_daily_cap_same_utc8_day_counts_one_day():
    """跨 UTC 零点但同一 UTC+8 自然日 → 只按 1 天封顶（旧实现会算 2 天）。"""
    calc = FeeCalculator()
    entry = datetime(2026, 5, 9, 20, 0)     # UTC+8 05-10 04:00
    exit_ = datetime(2026, 5, 10, 1, 0)     # UTC+8 05-10 09:00（同一天）
    bd = calc.compute(_rec(entry), exit_, _rule(cap=10.0))
    assert bd.daily_cap_applied is True
    assert bd.amount_due == 10.0            # 1 天 × 10，而不是 2×10


def test_daily_cap_cross_utc8_days_counts_two_days():
    calc = FeeCalculator()
    entry = datetime(2026, 5, 10, 14, 0)    # UTC+8 05-10 22:00
    exit_ = datetime(2026, 5, 10, 20, 0)    # UTC+8 05-11 04:00（跨本地日）
    bd = calc.compute(_rec(entry), exit_, _rule(cap=10.0))
    assert bd.daily_cap_applied is True
    assert bd.amount_due == 20.0            # 2 天 × 10


def test_normal_billing_unchanged_without_cap():
    """无封顶影响时普通计费保持不变（30 分钟首小时 5 + 1 单位 2 = 7）。"""
    calc = FeeCalculator()
    entry = datetime(2026, 1, 1, 10, 0)
    exit_ = datetime(2026, 1, 1, 11, 30)
    rule = SimpleNamespace(free_minutes=0, first_hour_price=5.0,
                           unit_minutes=30, unit_price=2.0,
                           daily_cap=40.0, ne_discount=1.0, round_up=True)
    bd = calc.compute(_rec(entry), exit_, rule)
    assert bd.amount_due == 7.0


# ---------------------------------------------------------------------------
# H3：授权后开闸回调失败不静默
# ---------------------------------------------------------------------------
def test_authorization_callback_failure_emits_incident(container, monkeypatch):
    c = container
    incidents = []
    c.bus.subscribe(IncidentRaised, incidents.append)
    barrier = c.access.barriers["in"]
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("request_open boom")

    monkeypatch.setattr(barrier, "request_open", boom)
    entry = c.access.manual_register("京B11111", "in", "tester")
    rec = c.parking_repo.find_open_by_plate("京B11111")
    # 记录/车位已提交，不回滚
    assert rec is not None and rec.id == entry["record_id"] and rec.space_no
    assert c.space_repo.get(rec.space_no).status == "occupied"
    # 有限重试 + incident（不静默）
    assert calls["n"] == c.access.AUTH_OPEN_ATTEMPTS
    assert incidents, "授权后开闸失败必须发布 incident"
    incident = incidents[-1]
    assert incident.kind == "barrier" and incident.gate == "in"
    assert incident.detail["reason"] == "authorization_callback_failed"
    assert incident.detail["record_id"] == rec.id
    assert incident.detail["plate"] == "京B11111"
    assert incident.detail["attempts"] == c.access.AUTH_OPEN_ATTEMPTS
    with c.db.scope():
        logs = c.db.session.execute(select(OpLog).where(
            OpLog.action == "authorization_callback_failed")).scalars().all()
    assert logs and str(rec.id) in (logs[-1].detail or "")


def test_authorization_callback_retry_recovers(container, monkeypatch):
    c = container
    incidents = []
    c.bus.subscribe(IncidentRaised, incidents.append)
    barrier = c.access.barriers["in"]
    original = barrier.request_open
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(barrier, "request_open", flaky)
    c.access.manual_register("京B22222", "in", "tester")
    assert calls["n"] >= 2
    assert barrier.state.value == "OPENING"
    assert not incidents


# ---------------------------------------------------------------------------
# H4：VehicleLeft 收尾（幂等，不重复收费/释放）
# ---------------------------------------------------------------------------
def test_vehicle_left_unpaid_marks_abnormal_and_idempotent(container):
    c = container
    releases, incidents = [], []
    c.bus.subscribe(SpaceReleased, releases.append)
    c.bus.subscribe(IncidentRaised, incidents.append)
    entry = c.access.authorize_entry("veh-left", "京C33333")
    rec_id = entry["record_id"]
    space_no = entry["space_no"]
    payments_before = c.db.session.query(PaymentRecord).count()

    c.access.on_vehicle_left(VehicleLeft(vehicle_id="veh-left", gate="in"))
    rec = c.parking_repo.get(rec_id)
    assert rec.status == "abnormal"
    assert rec.aborted_reason == "vehicle_left_without_exit"
    assert rec.exit_time is not None
    assert c.space_repo.get(space_no).status == "free"
    assert [s.space_no for s in releases].count(space_no) == 1
    assert any(i.detail.get("reason") == "vehicle_left_without_exit"
               and i.detail.get("record_id") == rec_id for i in incidents)
    assert c.db.session.query(PaymentRecord).count() == payments_before
    with c.db.scope():
        logs = c.db.session.execute(select(OpLog).where(
            OpLog.action == "vehicle_left_without_exit")).scalars().all()
    assert logs
    # 幂等：重复驶离不重复 incident/释放/收费
    releases_before = len(releases)
    incidents_before = len(incidents)
    c.access.on_vehicle_left(VehicleLeft(vehicle_id="veh-left", gate="in"))
    assert len(releases) == releases_before
    assert len(incidents) == incidents_before
    assert c.db.session.query(PaymentRecord).count() == payments_before


def test_vehicle_left_paid_marks_out_without_new_payment(container):
    c = container
    releases = []
    c.bus.subscribe(SpaceReleased, releases.append)
    entry = c.access.authorize_entry("veh-paid", "京D44444")
    rec_id = entry["record_id"]
    space_no = entry["space_no"]
    # 模拟已支付但尚未经过 PaymentCompleted 处理（遗留 open 记录）
    with c.db.scope():
        c.payment_repo.create(
            parking_record_id=rec_id, plate="京D44444", minutes=60,
            billable_units=1, amount_due=5.0, amount_paid=5.0, method="auto",
            operator="system",
            breakdown=FeeBreakdown(minutes=60, amount_due=5.0))
    payments_before = c.db.session.query(PaymentRecord).count()
    c.access.on_vehicle_left(VehicleLeft(vehicle_id="veh-paid", gate="out",
                                         plate="京D44444"))
    rec = c.parking_repo.get(rec_id)
    assert rec.status == "out" and rec.aborted_reason is None
    assert c.space_repo.get(space_no).status == "free"
    assert c.db.session.query(PaymentRecord).count() == payments_before
    assert [s.space_no for s in releases].count(space_no) == 1
    with c.db.scope():
        logs = c.db.session.execute(select(OpLog).where(
            OpLog.action == "vehicle_left_after_payment")).scalars().all()
    assert logs
    # 已出场的再次驶离幂等
    c.access.on_vehicle_left(VehicleLeft(vehicle_id="veh-paid", gate="out"))
    assert c.db.session.query(PaymentRecord).count() == payments_before
    assert c.parking_repo.get(rec_id).status == "out"


def test_vehicle_left_without_open_record_is_idempotent(container):
    c = container
    releases, incidents = [], []
    c.bus.subscribe(SpaceReleased, releases.append)
    c.bus.subscribe(IncidentRaised, incidents.append)
    c.access.on_vehicle_left(VehicleLeft(vehicle_id="ghost", gate="in"))
    c.access.on_vehicle_left(VehicleLeft(vehicle_id="ghost", gate="out",
                                         plate="京E55555"))
    assert releases == [] and incidents == []
    assert c.parking_repo.count_open() == 0
