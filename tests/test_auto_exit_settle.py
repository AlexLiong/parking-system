"""实现 A（20261031）：正常出口自动结算 + 异常纠正 + 黑名单不收费。

覆盖 Lead 需求：
1) 免费/收费/白名单/月租正常出场自动结算（PaymentRecord method=auto，金额含 0）→
   PaymentCompleted → out/释放车位/开出口，出场照片写回；
2) /api/pay 在自动结算后幂等（不重复扣费），剧本 pay 不阻塞；
3) 异常纠正 POST /api/manual/correct-exit（车牌/入场时长纠正，按 sim 时长自动计费）；
   无记录 free/void 临时放行、auto 409 no_parking_record、重复/非 open 错误；
4) 黑名单出场 0 费用、0 普通记录（含遗留记录仅审计清理）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from server.core.events import (BarrierCommand, FeeQuoted, PaymentCompleted,
                                VehicleArrived)
from server.repository.models import ParkingRecord, PaymentRecord

EPOCH = datetime(2026, 10, 31, 8, 0, 0)


def _setup_clock(c, sim_time=0.0):
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.sim_time = float(sim_time)


def _fake_out(monkeypatch, c, plate):
    monkeypatch.setattr(c.access, "_recognize",
                        lambda gate, event: (plate, "blue", 0.99, False,
                                             [plate], "crops/out.png", "ok", None))


def _out_arrival(c, vehicle_id, image_path="photos/out.png"):
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id=vehicle_id, gate="out", image_path=image_path))


# ---------------------------------------------------------------------------
# 1) 正常有记录车辆出场自动结算
# ---------------------------------------------------------------------------
def test_free_vehicle_auto_settles_zero_and_opens_gate(container, monkeypatch):
    c = container
    _setup_clock(c, 0.0)
    entry = c.access.manual_register("京A11111", "in", "tester")
    _setup_clock(c, 600.0)                       # 10 分钟 ≤ 免费 15 分钟
    payments, quotes, commands = [], [], []
    c.bus.subscribe(PaymentCompleted, payments.append)
    c.bus.subscribe(FeeQuoted, quotes.append)
    c.bus.subscribe(BarrierCommand, commands.append)
    _fake_out(monkeypatch, c, "京A11111")
    _out_arrival(c, "veh-free")
    rec = c.parking_repo.get(entry["record_id"])
    assert rec.status == "out" and rec.exit_photo == "photos/out.png"
    pay = c.payment_repo.get_by_parking(rec.id)
    assert pay is not None
    assert pay.amount_paid == 0.0 and pay.amount_due == 0.0
    assert pay.method == "auto"
    assert quotes and quotes[-1].amount == 0.0
    assert payments and payments[-1].record_id == rec.id
    assert c.space_repo.get(entry["space_no"]).status == "free"
    assert any(cmd.gate == "out" and cmd.cmd == "open"
               and cmd.authorization == "payment" for cmd in commands)
    assert c.access.barriers["out"].is_passable()
    assert c.access.list_pending("pending") == []


def test_charged_vehicle_auto_settles_amount_and_exit_photo(container, monkeypatch):
    c = container
    _setup_clock(c, 0.0)
    entry = c.access.manual_register("京B22222", "in", "tester")
    _setup_clock(c, 1800.0)                      # 30 分钟 → 首小时 5 元
    _fake_out(monkeypatch, c, "京B22222")
    _out_arrival(c, "veh-charge", image_path="photos/charged.png")
    rec = c.parking_repo.get(entry["record_id"])
    assert rec.status == "out"
    assert rec.exit_photo == "photos/charged.png"
    pay = c.payment_repo.get_by_parking(rec.id)
    assert pay is not None and pay.method == "auto"
    assert pay.amount_due == 5.0 and pay.amount_paid == 5.0
    assert pay.minutes == 30
    assert c.space_repo.get(entry["space_no"]).status == "free"


@pytest.mark.parametrize("list_type,benefit", [("white", "white"), ("monthly", "monthly")])
def test_whitelist_and_monthly_auto_settle(container, monkeypatch, list_type, benefit):
    c = container
    plate = "京C33333" if list_type == "white" else "京D44444"
    with c.db.scope():
        c.list_repo.upsert(plate=plate, list_type=list_type, note="自动结算测试")
    _setup_clock(c, 0.0)
    entry = c.access.manual_register(plate, "in", "tester")
    _setup_clock(c, 3600.0)
    _fake_out(monkeypatch, c, plate)
    _out_arrival(c, f"veh-{list_type}")
    rec = c.parking_repo.get(entry["record_id"])
    assert rec.status == "out"
    assert rec.benefit_type == benefit
    pay = c.payment_repo.get_by_parking(rec.id)
    assert pay is not None and pay.amount_paid == 0.0 and pay.method == "auto"


def test_pay_api_idempotent_after_auto_settle(client, admin_token, monkeypatch):
    c = client.app.state.container
    _setup_clock(c, 0.0)
    entry = c.access.manual_register("京E55555", "in", "tester")
    _setup_clock(c, 1800.0)
    _fake_out(monkeypatch, c, "京E55555")
    _out_arrival(c, "veh-api")
    rec = c.parking_repo.get(entry["record_id"])
    pay_auto = c.payment_repo.get_by_parking(rec.id)
    assert rec.status == "out" and pay_auto is not None
    # 剧本 pay 动作在自动结算后仍幂等返回（不阻塞、不重复扣费）
    r = client.post("/api/pay", headers=admin_token,
                    json={"record_id": rec.id, "method": "mock_scan"})
    assert r.status_code == 200, r.text
    assert r.json()["payment_id"] == pay_auto.id
    assert c.db.session.query(PaymentRecord).filter_by(
        parking_record_id=rec.id).count() == 1
    assert c.payment_repo.get_by_parking(rec.id).amount_paid == pay_auto.amount_paid
    # 重复人工出场同样幂等
    r2 = client.post("/api/manual/exit", headers=admin_token,
                     json={"plate": "京E55555", "action": "charge",
                           "record_id": rec.id})
    assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------------------
# 2) 异常纠正 correct-exit
# ---------------------------------------------------------------------------
def test_correct_exit_plate_charges_original_duration(client, admin_token, monkeypatch):
    c = client.app.state.container
    _setup_clock(c, 0.0)
    wrong = c.access.manual_register("京F66666", "in", "tester")
    _setup_clock(c, 1800.0)                       # 原入场时长 30 分钟
    r = client.post("/api/manual/correct-exit", headers=admin_token,
                    json={"record_id": wrong["record_id"], "plate": "京G77777",
                          "reason": "入场车牌录错"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["plate"] == "京G77777"
    settlement = body["settlement"]
    assert settlement["action"] == "auto"
    assert settlement["amount_paid"] == 5.0
    rec = c.parking_repo.get(wrong["record_id"])
    assert rec.plate == "京G77777" and rec.status == "out"
    pay = c.payment_repo.get_by_parking(rec.id)
    assert pay is not None and pay.plate == "京G77777" and pay.amount_paid == 5.0
    assert c.space_repo.get(wrong["space_no"]).status == "free"
    assert c.access.barriers["out"].is_passable()


def test_correct_exit_entry_time_charges_by_sim_duration(client, admin_token):
    c = client.app.state.container
    _setup_clock(c, 0.0)
    entry = c.access.manual_register("京H88888", "in", "tester")
    _setup_clock(c, 7200.0)                       # 120 分钟
    r = client.post("/api/manual/correct-exit", headers=admin_token,
                    json={"record_id": entry["record_id"], "plate": "京H88888",
                          "entry_sim_sec": 0, "reason": "实际入场时间纠正"})
    assert r.status_code == 200, r.text
    settlement = r.json()["settlement"]
    # 免费 15 分钟 + 首小时 5 元 + 45 分钟按 2×30 分钟单位（2×2 元）= 9 元
    assert settlement["amount_paid"] == pytest.approx(9.0)
    rec = c.parking_repo.get(entry["record_id"])
    assert rec.status == "out" and rec.entry_sim_sec == 0.0
    # 非法入场时间 → invalid_entry_time
    entry2 = c.access.manual_register("京J99999", "in", "tester")
    bad = client.post("/api/manual/correct-exit", headers=admin_token,
                      json={"record_id": entry2["record_id"], "plate": "京J99999",
                            "entry_sim_sec": 99999999, "reason": "错误时间"})
    assert bad.status_code == 409
    assert bad.json()["error"] == "invalid_entry_time"


def test_correct_exit_without_record_free_void_or_no_record(client, admin_token):
    c = client.app.state.container
    _setup_clock(c, 0.0)
    # 无记录 auto → no_parking_record + open_records 摘要
    r = client.post("/api/manual/correct-exit", headers=admin_token,
                    json={"plate": "京K11111", "action": "auto",
                          "reason": "尝试自动"})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "no_parking_record"
    assert "open_records" in body["detail"]
    # 无记录 free → 临时账单 0 元放行
    r2 = client.post("/api/manual/correct-exit", headers=admin_token,
                     json={"plate": "京K11111", "action": "free",
                           "reason": "无入场记录，免费放行"})
    assert r2.status_code == 200, r2.text
    s = r2.json()["settlement"]
    assert s["temporary"] is True and s["amount_paid"] == 0.0
    assert r2.json()["status"] == "out"
    # 无记录 void → 临时作废
    r3 = client.post("/api/manual/correct-exit", headers=admin_token,
                     json={"plate": "京L22222", "action": "void",
                           "reason": "无入场记录，作废"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["settlement"]["temporary"] is True


def test_correct_exit_duplicate_and_not_open_errors(client, admin_token):
    c = client.app.state.container
    _setup_clock(c, 0.0)
    first = c.access.manual_register("京M33333", "in", "tester")
    second = c.access.manual_register("京N44444", "in", "tester")
    dup = client.post("/api/manual/correct-exit", headers=admin_token,
                      json={"record_id": first["record_id"], "plate": "京N44444",
                            "reason": "纠正为已在场的车牌"})
    assert dup.status_code == 409 and dup.json()["error"] == "duplicate_entry"
    # 已结束记录 → record_not_open
    c.access.manual_exit("京M33333", "tester", "正常结算", action="charge",
                         record_id=first["record_id"])
    closed = client.post("/api/manual/correct-exit", headers=admin_token,
                         json={"record_id": first["record_id"], "plate": "京M33333",
                               "reason": "已结束"})
    assert closed.status_code == 409 and closed.json()["error"] == "record_not_open"
    # request_id 幂等
    payload = {"record_id": second["record_id"], "plate": "京N44444",
               "reason": "幂等测试", "request_id": "corr-1"}
    a = client.post("/api/manual/correct-exit", headers=admin_token, json=payload)
    b = client.post("/api/manual/correct-exit", headers=admin_token, json=payload)
    assert a.status_code == 200 and b.status_code == 200
    assert a.json() == b.json()
    assert c.parking_repo.get(second["record_id"]).status == "out"


# ---------------------------------------------------------------------------
# 3) 黑名单出场：0 费用、0 普通记录
# ---------------------------------------------------------------------------
def test_blacklist_exit_never_charges(container, monkeypatch):
    c = container
    _setup_clock(c, 0.0)
    plate = "京P55555"
    with c.db.scope():
        c.list_repo.upsert(plate=plate, list_type="black", note="实现 A 测试")
    # 模拟遗留 open 记录（历史上已入场）
    with c.db.scope():
        legacy = c.parking_repo.create(plate=plate, plate_color="blue",
                                       entry_time=c.access._now(),
                                       space_no="A-01", status="in",
                                       entry_sim_sec=0.0)
        c.space_repo.occupy("A-01", plate)
    payments_before = c.db.session.query(PaymentRecord).count()
    _setup_clock(c, 1800.0)
    _fake_out(monkeypatch, c, plate)
    _out_arrival(c, "veh-black-exit")
    rec = c.parking_repo.get(legacy.id)
    assert rec.status == "abnormal" and rec.aborted_reason == "blacklist_exit"
    assert c.payment_repo.get_by_parking(rec.id) is None
    assert c.db.session.query(PaymentRecord).count() == payments_before
    assert c.space_repo.get("A-01").status == "free"
    assert c.access.barriers["out"].state.value != "KEEP_OPEN"  # 不自动开闸收费放行
    # 黑名单绕行 open：遗留记录只审计清理、不收费
    with c.db.scope():
        legacy2 = c.parking_repo.create(plate=plate, plate_color="blue",
                                        entry_time=c.access._now(),
                                        space_no="B-01", status="in",
                                        entry_sim_sec=1800.0)
        c.space_repo.occupy("B-01", plate)
    result = c.access.blacklist_gate("veh-black-exit", plate, "out", "open")
    assert result["cleared_record_id"] == legacy2.id
    assert c.payment_repo.get_by_parking(legacy2.id) is None
    assert c.db.session.query(PaymentRecord).count() == payments_before
    assert c.parking_repo.get(legacy2.id).status == "abnormal"
