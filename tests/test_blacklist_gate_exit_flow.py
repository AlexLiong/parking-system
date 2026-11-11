"""实现 A：黑名单绕行闸杆 + 人工异常出口正常计费/授权 + 可通行语义 + 待办恢复。

覆盖 Lead 需求 1-4：
1) POST /api/sim/blacklist-gate 正常开/关闸、审计、不建 parking_record；
2) 出口相机故障人工确认 33 分钟 → 实际收费>0、记录 out、发 PaymentCompleted/ExitAuthorized、
   释放车位、出口闸可通行；
3) manual_open/KEEP_OPEN/FAULT 人工抬杆 → 物理可通行（manual/passable/authorization），
   但未完成业务结算时不产生授权（不得放行由前端判断）；
4) 拒绝/过期后释放停止线（ArrivalRetryRequested），不阻塞同车重试与后车队列。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from server.access.service import AccessControlService
from server.core.errors import StateConflict
from server.core.events import (ArrivalRetryRequested, BarrierCommand, BarrierStateChanged,
                                ExitAuthorized, PaymentCompleted, VehicleArrived)
from server.core.timeutil import utcnow
from server.repository.models import BarrierStateRow, ParkingRecord


def _black_plate(c, plate="京A44444"):
    with c.db.scope():
        c.list_repo.upsert(plate=plate, list_type="black", note="实现 A 测试")
    return plate


def _ack(client, gate, state, cmd_id):
    return client.post("/sim/events/barrier_ack",
                       json={"gate": gate, "state": state, "cmd_id": cmd_id})


# ---------------------------------------------------------------------------
# 1) 黑名单绕行闸杆（无停车记录，仅审计）
# ---------------------------------------------------------------------------
def test_blacklist_gate_open_close_with_audit_no_parking_record(client, admin_token):
    c = client.app.state.container
    plate = _black_plate(c)
    before_open = c.parking_repo.count_open()

    r = client.post("/api/sim/blacklist-gate", headers=admin_token,
                    json={"vehicle_id": "blk-front", "plate": plate,
                          "gate": "in", "action": "open"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["reason"] == "blacklist_bypass"
    assert body["manual"] is True and body["passable"] is True
    assert body["authorization"] == "manual"
    assert body["state"] == "OPENING" and body["cmd_id"]
    assert body["seq"] is not None
    assert c.parking_repo.count_open() == before_open          # 不建 parking_record
    assert c.parking_repo.find_open_by_plate(plate) is None

    # 正常回执 → 物理保持开启（可通行）
    assert _ack(client, "in", "open", body["cmd_id"]).json()["accepted"]
    snap = client.get("/api/state/snapshot", headers=admin_token).json()["barriers"]["in"]
    assert snap["state"] == "KEEP_OPEN" and snap["passable"] is True

    # 审计：事件 / op_log / 道闸动作 reason
    events = client.get("/api/events?types=blacklist_gate", headers=admin_token).json()["items"]
    assert any(e["data"]["plate"] == plate and e["data"]["action"] == "open"
               and e["data"]["reason"] == "blacklist_bypass" for e in events)
    actions = client.get("/api/barrier/actions?gate=in", headers=admin_token).json()["items"]
    assert any(a["reason"] == "blacklist_bypass" and a["cmd"] == "open" for a in actions)

    # 场内绕行结束 → close 正常落杆
    closed = client.post("/api/sim/blacklist-gate", headers=admin_token,
                         json={"vehicle_id": "blk-front", "plate": plate,
                               "gate": "in", "action": "close"}).json()
    assert closed["passable"] is False and closed["state"] == "CLOSING"
    assert _ack(client, "in", "closed", closed["cmd_id"]).json()["accepted"]
    snap = client.get("/api/state/snapshot", headers=admin_token).json()["barriers"]["in"]
    assert snap["state"] == "CLOSED" and snap["passable"] is False
    assert c.parking_repo.count_open() == before_open
    # 不产生业务授权事件
    types = {e["name"] for e in client.get("/api/events?since=0", headers=admin_token).json()["items"]}
    assert "payment_completed" not in types and "exit_authorized" not in types


def test_blacklist_gate_rejects_non_blacklist(client, admin_token):
    r = client.post("/api/sim/blacklist-gate", headers=admin_token,
                    json={"vehicle_id": "v", "plate": "京B12345",
                          "gate": "in", "action": "open"})
    assert r.status_code == 409
    assert r.json()["error"] == "not_blacklisted"


def test_blacklist_gate_both_paths_no_rear_and_with_rear(client, admin_token):
    """无后车（倒车离场）与有后车（场内绕行）两条路径都走正常闸杆 open/close。"""
    c = client.app.state.container
    for index, plate in enumerate(("京C44444", "京D44444")):
        _black_plate(c, plate)
        opened = client.post("/api/sim/blacklist-gate", headers=admin_token,
                             json={"vehicle_id": f"blk-{index}", "plate": plate,
                                   "gate": "in", "action": "open"}).json()
        assert opened["passable"] is True
        assert opened["manual"] is True
        _ack(client, "in", "open", opened["cmd_id"])
        closed = client.post("/api/sim/blacklist-gate", headers=admin_token,
                             json={"vehicle_id": f"blk-{index}", "plate": plate,
                                   "gate": "in", "action": "close"}).json()
        _ack(client, "in", "closed", closed["cmd_id"])
    assert c.parking_repo.count_open() == 0
    events = client.get("/api/events?types=blacklist_gate", headers=admin_token).json()["items"]
    assert len([e for e in events if e["data"]["action"] == "open"]) >= 2
    assert len([e for e in events if e["data"]["action"] == "close"]) >= 2


# ---------------------------------------------------------------------------
# 2) 出口相机故障人工输入 33 分钟 → 实际收费/出场/开出口
# ---------------------------------------------------------------------------
def test_exit_camera_fault_manual_confirm_33min_really_charges(client, admin_token):
    c = client.app.state.container
    c.cfg.server.demo_mode = True
    from datetime import datetime
    c.simulation.epoch = datetime(2026, 10, 26, 8, 0, 0)
    c.simulation.sim_time = 0.0
    entry = c.access.manual_register("京Q55555", "in", "tester")
    rec_id = entry["record_id"]
    c.simulation.sim_time = 33 * 60.0                       # 33 分钟
    # 出口相机故障 → 到达转人工
    c.watchdog.inject_fault("out", reason="test")
    payments, exits, commands = [], [], []
    c.bus.subscribe(PaymentCompleted, payments.append)
    c.bus.subscribe(ExitAuthorized, exits.append)
    c.bus.subscribe(BarrierCommand, commands.append)
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="car-out", gate="out", image_path="frames/out.png"))
    pending = c.access.list_pending("pending")
    assert len(pending) == 1 and pending[0]["gate"] == "out"
    r = client.post("/api/manual/confirm", headers=admin_token,
                    json={"id": pending[0]["id"], "plate": "京Q55555"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    settlement = body["settlement"]
    assert settlement["payment_id"] and settlement["status"] == "out"
    assert settlement["amount_paid"] == pytest.approx(5.0)
    rec = c.parking_repo.get(rec_id)
    assert rec.status == "out" and rec.exit_time is not None
    payment = c.payment_repo.get_by_parking(rec_id)
    assert payment is not None and payment.amount_due > 0 and payment.amount_paid > 0
    assert payment.method == "cash"
    # 车位释放 + 出口可通行 + 支付/出场事件
    assert c.space_repo.get(entry["space_no"]).status == "free"
    assert payments and exits and exits[0].action == "charge"
    assert any(cmd.gate == "out" and cmd.cmd == "open" and not cmd.manual
               and cmd.authorization == "payment" for cmd in commands)
    assert c.access.barriers["out"].is_passable()
    assert c.access.list_pending("pending") == []


# ---------------------------------------------------------------------------
# 3) 可通行语义：manual_open/KEEP_OPEN/FAULT 人工抬杆物理可通行但不授权
# ---------------------------------------------------------------------------
def test_manual_open_physical_only_does_not_authorize(container):
    c = container
    entry = c.access.manual_register("京R55555", "in", "tester")
    rec_id = entry["record_id"]
    commands, states = [], []
    exits, payments = [], []
    c.bus.subscribe(BarrierCommand, commands.append)
    c.bus.subscribe(BarrierStateChanged, states.append)
    c.bus.subscribe(ExitAuthorized, exits.append)
    c.bus.subscribe(PaymentCompleted, payments.append)
    opened = c.access.manual_open("out", operator="tester", reason="人工抬杆")
    assert opened["ok"] and opened["manual"] is True
    assert opened["passable"] is True and opened["authorization"] == "manual"
    cmd = next(cmd for cmd in commands if cmd.gate == "out" and cmd.cmd == "open")
    assert cmd.manual is True and cmd.passable is True and cmd.authorization == "manual"
    state = next(s for s in states if s.gate == "out" and s.state == "OPENING")
    assert state.manual is True and state.passable is True
    # 业务未结算：记录仍在场、无支付/出场授权
    assert c.parking_repo.get(rec_id).status == "in"
    assert c.payment_repo.get_by_parking(rec_id) is None
    assert exits == [] and payments == []
    assert not c.space_repo.get(entry["space_no"]).status == "free"


def test_keep_open_and_fault_manual_lift_are_passable(client, admin_token):
    c = client.app.state.container
    keep = client.post("/api/barrier/in/keep-open", headers=admin_token,
                       json={"enabled": True}).json()
    assert keep["passable"] is True and keep["manual"] is True
    assert keep["authorization"] == "keep_open"
    snap = client.get("/api/state/snapshot", headers=admin_token).json()["barriers"]["in"]
    assert snap["passable"] is True
    # 注入 stuck_closed 故障 → 人工抬杆（手动曲柄）仍可通行
    assert client.post("/api/barrier/in/fault", headers=admin_token,
                       json={"mode": "stuck_closed"}).status_code == 200
    lifted = client.post("/api/manual/open-barrier", headers=admin_token,
                         json={"gate": "in", "reason": "fault_manual_lift"})
    assert lifted.status_code == 200, lifted.text
    body = lifted.json()
    assert body["manual"] is True and body["passable"] is True
    assert body["authorization"] == "manual"
    snap = client.get("/api/state/snapshot", headers=admin_token).json()["barriers"]["in"]
    assert snap["passable"] is True


def test_normal_exit_payment_then_manual_open_passable(container):
    c = container
    c.cfg.server.demo_mode = True
    entry = c.access.manual_register("京S55555", "in", "tester")
    c.simulation.sim_time = 600.0
    payments, exits = [], []
    c.bus.subscribe(PaymentCompleted, payments.append)
    c.bus.subscribe(ExitAuthorized, exits.append)
    payment = c.billing.pay(entry["record_id"], method="cash", operator="tester")
    rec = c.parking_repo.get(entry["record_id"])
    assert rec.status == "out" and payment.amount_paid == 0.0
    assert payments and c.access.barriers["out"].is_passable()   # 正常付款后开杆
    # 人工抬杆（例如道闸故障后人工放行）→ 物理可通行，authorization=manual
    manual = c.access.manual_open("out", operator="tester", reason="道闸故障人工放行")
    assert manual["manual"] is True and manual["passable"] is True
    assert manual["authorization"] == "manual"


# ---------------------------------------------------------------------------
# 4) 待办拒绝/超时释放停止线，同车重试/后车不阻塞
# ---------------------------------------------------------------------------
def test_reject_and_expire_release_stop_line_and_allow_retry(container, monkeypatch):
    c = container
    retries = []
    c.bus.subscribe(ArrivalRetryRequested, retries.append)
    # 无识别器 → 待办，模拟识别失败停在停止线
    rejected = c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="rej-1", gate="in", image_path=None))
    pending = c.access.list_pending("pending")[0]
    c.access.reject_pending(pending["id"], operator="tester", reason="车主要离开")
    assert retries and retries[-1].reason == "pending_rejected"
    assert retries[-1].vehicle_id == "rej-1"
    assert ("in", "rej-1") not in c.access._arrivals          # 停止线缓存清空，可重试
    # 同车重新到达（识别恢复）→ 正常入场，不被拒绝记录阻塞
    c.access.recognizer = None
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="rej-1", gate="in", image_path=None))
    assert len(c.access.list_pending("pending")) == 1
    # 超时释放
    expiring = c.access.list_pending("pending")[0]
    row = c.pending_repo.get(expiring["id"])
    with c.db.scope():
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert c.access.expire_pending() == 1
    assert retries[-1].reason == "pending_expired"
    assert ("in", "rej-1") not in c.access._arrivals
    # 后车队列不被阻塞：识别正常车辆可正常入场
    monkeypatch.setattr(c.access, "_recognize",
                        lambda gate, event: ("京J66666", "blue", 0.99, False,
                                             ["京J66666"], None, "ok", None))
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="next-1", gate="in", image_path="frames/next.png"))
    assert c.parking_repo.find_open_by_plate("京J66666", include_temporary=False) is not None
    assert c.access.list_pending("pending") == []
