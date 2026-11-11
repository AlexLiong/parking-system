"""实现 B：仿真时钟/业务时间 + 强制重置/CLI + 人工授权/黑名单/重复。

覆盖：
1. 仿真时钟统一业务时间：entry/exit 业务时间、entry_sim_sec/duration_sim_sec、
   计费按 sim duration、暂停冻结、速度 0.25–10 校验、manual_time 仿真秒/分钟输入；
2. 强制重置：无 force 时闸区有车/道闸动作未完成 → 409；force=true 清理并保留完成历史；
   CLI --reset / --resetdata（离线运行、resetdata 先备份再重建+seed）；
3. 重复判定：/api/sim/dispatch-plan 同牌 open → 409 duplicate_entry + 现有记录；
4. 人工授权：authorize-entry/authorize-exit（charge/free/void、无记录不卡死）；
5. 黑名单：不建 pending/记录，发布 EntryDenied；/api/sim/blacklist-resolution 记录处置；
6. 剧本校验：同牌重叠拒绝、相邻/不同牌允许。
"""
from __future__ import annotations

import sys
from datetime import datetime

import pytest

from server.access.service import AccessControlService
from server.core.errors import DataAnomaly, NoParkingRecordError, StateConflict, ValidationError
from server.core.events import (ArrivalRetryRequested, BarrierCommand, EntryDenied,
                                 ExitAuthorized, ManualAuthorized, SpaceAssigned,
                                 VehicleArrived)
from server.core.timeutil import utcnow
from server.repository.models import BarrierAction, PendingManual, VehicleRuntime

EPOCH = datetime(2026, 10, 25, 8, 0, 0)


# ---------------------------------------------------------------------------
# 1. 仿真时钟 / 业务时间 / 计费
# ---------------------------------------------------------------------------
def test_business_time_and_sim_fields(container):
    c = container
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.sim_time = 0.0
    entry = c.access.manual_register("京A11111", "in", "tester")
    rec = c.parking_repo.get(entry["record_id"])
    assert rec.entry_sim_sec == 0.0
    assert rec.entry_time == EPOCH                     # 业务时间=epoch+sim，而非真实墙钟
    assert abs((utcnow() - rec.created_at).total_seconds()) < 30   # created_at 保留真实墙钟

    c.simulation.sim_time = 1800.0                     # 30 分钟（仿真）
    quote = c.billing.quote("京A11111")
    assert quote.minutes == 30
    assert quote.duration_sim_sec == 1800.0
    assert quote.entry_sim_sec == 0.0 and quote.exit_sim_sec == 1800.0
    assert quote.exit_time == EPOCH.replace(minute=30)  # 业务时间 08:30
    assert quote.amount_due == 5.0                     # 免费 15 分钟后 15 分钟 → 首小时 5 元

    pay = c.billing.pay(rec.id, method="cash", operator="tester")
    assert pay.amount_due == 5.0
    rec = c.parking_repo.get(rec.id)
    assert rec.status == "out"
    assert rec.exit_sim_sec == 1800.0
    assert rec.duration_sim_sec == 1800.0
    assert rec.exit_time == EPOCH.replace(minute=30)


def test_sim_pause_freezes_business_time_and_quote(container):
    c = container
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.sim_time = 0.0
    entry = c.access.manual_register("京B22222", "in", "tester")
    c.simulation.sim_time = 900.0
    before = c.billing.quote("京B22222").minutes
    c.simulation.control(paused=True)
    c.simulation.sim_time = 900.0                       # 暂停期间 tick 不推进；手动赋值仅模拟"时间未走"
    assert c.billing.quote("京B22222").minutes == before == 15
    c.simulation.control(paused=False)
    c.simulation.sim_time = 3600.0
    assert c.billing.quote("京B22222").minutes == 60
    assert c.parking_repo.get(entry["record_id"]).entry_sim_sec == 0.0


def test_manual_time_accepts_sim_seconds_and_minutes(container):
    c = container
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.sim_time = 0.0
    entry = c.access.manual_register("京C33333", "in", "tester")
    rec = c.parking_repo.get(entry["record_id"])
    c.simulation.sim_time = 3600.0                      # 出场时 1 小时
    c.access.manual_exit("京C33333", "tester", "核实入场", action="manual_time",
                         record_id=rec.id, entry_sim_sec=600)   # 实际 10 分钟前进场
    rec = c.parking_repo.get(rec.id)
    assert rec.entry_sim_sec == 600.0
    assert rec.entry_time == EPOCH.replace(minute=10)
    payment = c.payment_repo.get_by_parking(rec.id)
    assert payment is not None and payment.minutes == 50   # (3600-600)/60
    assert rec.status == "out" and rec.duration_sim_sec == 3000.0

    # 仿真分钟输入
    entry2 = c.access.manual_register("京K44444", "in", "tester")
    rec2 = c.parking_repo.get(entry2["record_id"])
    c.access.manual_exit("京K44444", "tester", "核实入场", action="manual_time",
                         record_id=rec2.id, entry_sim_sec=15 * 60)
    assert c.parking_repo.get(rec2.id).entry_sim_sec == 900.0

    # 旧 ISO 输入仍兼容（保持真实时间语义）
    entry3 = c.access.manual_register("京E55555", "in", "tester")
    rec3 = c.parking_repo.get(entry3["record_id"])
    legacy = utcnow().replace(microsecond=0)
    c.access.manual_exit("京E55555", "tester", "核实入场", action="manual_time",
                         record_id=rec3.id, entry_time=legacy)
    rec3 = c.parking_repo.get(rec3.id)
    assert abs((rec3.entry_time - legacy).total_seconds()) < 2
    assert rec3.entry_sim_sec is None


def test_manual_time_sim_requires_demo_mode(container):
    c = container
    c.cfg.server.demo_mode = False
    entry = c.access.manual_register("京F66666", "in", "tester")
    rec = c.parking_repo.get(entry["record_id"])
    with pytest.raises(ValidationError):
        c.access.manual_exit("京F66666", "tester", "核实入场", action="manual_time",
                             record_id=rec.id, entry_sim_sec=60)


def test_manual_time_api_seconds_and_minutes(client, admin_token):
    c = client.app.state.container
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.sim_time = 7200.0
    entry = c.access.manual_register("京G77777", "in", "tester")
    rec_id = entry["record_id"]
    r = client.post("/api/manual/exit", headers=admin_token,
                    json={"plate": "京G77777", "action": "manual_time",
                          "record_id": rec_id, "entry_sim_min": 60,
                          "reason": "核实入场"})
    assert r.status_code == 200, r.text
    rec = c.parking_repo.get(rec_id)
    assert rec.entry_sim_sec == 3600.0
    # 仿真秒与分钟互斥
    r2 = client.post("/api/manual/exit", headers=admin_token,
                     json={"plate": "京G77777", "action": "manual_time",
                           "record_id": rec_id, "entry_sim_sec": 60,
                           "entry_sim_min": 1})
    assert r2.status_code == 422


def test_sim_fields_in_snapshot_and_records_api(client, admin_token):
    c = client.app.state.container
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.paused = True          # 客户端 fixture 有 tick 后台；冻结以保证精确值
    c.simulation.sim_time = 0.0
    entry = c.access.manual_register("京H88888", "in", "tester")
    c.simulation.sim_time = 600.0
    snap = client.get("/api/state/snapshot", headers=admin_token).json()
    on_lot = {item["record_id"]: item for item in snap["on_lot"]}
    assert on_lot[entry["record_id"]]["entry_sim_sec"] == 0.0
    assert on_lot[entry["record_id"]]["duration_sim_sec"] == 600.0
    assert snap["simulation"]["business_time"]
    rec = client.get(f"/api/records/{entry['record_id']}", headers=admin_token).json()
    assert rec["entry_sim_sec"] == 0.0
    quote = client.get("/api/quote", headers=admin_token,
                       params={"plate": "京H88888"}).json()
    assert quote["duration_sim_sec"] == 600.0 and quote["minutes"] == 10


# ---------------------------------------------------------------------------
# 2. 强制重置 + CLI
# ---------------------------------------------------------------------------
def test_force_reset_skips_safety_checks_and_keeps_completed_history(container):
    c = container
    c.cfg.server.demo_mode = True
    # 已完成历史（应保留）
    done = c.access.manual_register("京J11111", "in", "tester")
    c.access.manual_exit("京J11111", "tester", "正常结算", action="charge",
                         record_id=done["record_id"])
    # 开放记录 + 闸区有车 + 道闸动作未完成 → 无 force 必须拒绝
    open_entry = c.access.manual_register("京J22222", "in", "tester")
    c.access.barriers["in"].zone_occupied = True
    c.access._pending["in"] = 1
    c.simulation.sim_time = 1234.0
    with pytest.raises(StateConflict) as ei:
        c.state.reset(request_id="r-no-force", mode="soft", operator="tester")
    assert ei.value.kind == "reset_unsafe"
    # force=true → 清理成功
    result = c.state.reset(request_id="r-force", mode="soft", operator="tester",
                           reason="force reset", force=True)
    assert result["reset_report"]["closed_records"] >= 1
    assert result["reset_report"]["force"] is True
    rec_open = c.parking_repo.get(open_entry["record_id"])
    assert rec_open.status == "aborted" and rec_open.aborted_reason == "demo_reset"
    assert rec_open.exit_sim_sec == 1234.0
    rec_done = c.parking_repo.get(done["record_id"])
    assert rec_done.status == "out"                       # 完成历史未删
    assert c.parking_repo.count_open() == 0
    assert c.space_repo.free_total() == 80
    assert c.simulation.sim_time == 0.0
    assert result["new_session"] != result["closed_session"]
    session = c.state.current_session()
    assert session is not None and session.status == "active"
    pending_statuses = {row.status for row in c.db.session.query(PendingManual).all()}
    assert "pending" not in pending_statuses
    assert c.db.session.query(VehicleRuntime).filter_by(
        session_id=result["closed_session"]).count() == 0


def test_force_reset_api(client, admin_token):
    c = client.app.state.container
    c.access.barriers["in"].zone_occupied = True
    r = client.post("/api/demo/reset", headers=admin_token,
                    json={"request_id": "api-no-force", "mode": "soft"})
    assert r.status_code == 409 and r.json()["error"] == "reset_unsafe"
    r2 = client.post("/api/demo/reset", headers=admin_token,
                     json={"request_id": "api-force", "mode": "soft", "force": True})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["reset_report"]["force"] is True
    assert body["snapshot"]["session"]["status"] == "active"


def test_reset_interrupts_pending_barrier_actions(container):
    c = container
    c.db.session.add(BarrierAction(cmd_id="stuck-cmd", gate="out", cmd="open",
                                   reason="test", status="pending", created_at=utcnow()))
    c.db.session.flush()
    c.state.reset(request_id="r-interrupt", mode="soft", operator="tester", force=True)
    row = c.db.session.query(BarrierAction).filter_by(cmd_id="stuck-cmd").one()
    assert row.status == "interrupted"


def test_cli_reset_offline(container):
    """tools/reset_runtime.py --reset：无需后台服务，清开放记录保留完成历史。"""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("reset_runtime",
                                                  root / "tools" / "reset_runtime.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reset_runtime"] = mod
    spec.loader.exec_module(mod)
    c = container
    done = c.access.manual_register("京K11111", "in", "tester")
    c.access.manual_exit("京K11111", "tester", "正常结算", action="charge",
                         record_id=done["record_id"])
    open_entry = c.access.manual_register("京K22222", "in", "tester")
    result = mod.reset_runtime(c.cfg)
    assert result["mode"] == "reset"
    assert result["after"]["open_records"] == 0
    assert result["after"]["free_total"] == 80
    assert result["after"]["sim_time"] == 0.0
    assert c.parking_repo.get(open_entry["record_id"]).status == "aborted"
    assert c.parking_repo.get(done["record_id"]).status == "out"


def test_cli_resetdata_backup_and_rebuild(cfg, tmp_path):
    """tools/reset_runtime.py --resetdata：备份 → 删库 → 重建 → 重灌演示数据。"""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("reset_runtime",
                                                  root / "tools" / "reset_runtime.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reset_runtime"] = mod
    spec.loader.exec_module(mod)
    from server.gateway.context import Container
    c = Container(cfg)
    c.bootstrap()
    c.start()
    c.access.manual_register("京L11111", "in", "tester")
    c.close()                                   # Windows 下先释放 DB 文件句柄
    result = mod.resetdata(cfg, days=3, records=10, inhouse=3, seed=7)
    assert result["mode"] == "resetdata"
    assert result["backup_dir"] and Path(result["backup_dir"]).is_dir()
    backup_files = list(Path(result["backup_dir"]).glob("*"))
    assert any(f.name.startswith("parking.db") or f.name == "manifest.json"
               for f in backup_files)
    assert result["history"]["records"] == 10
    assert result["inhouse"]["in"] == 3
    assert result["runtime"]["open_records"] == 3
    assert result["runtime"]["sim_time"] == 0.0


# ---------------------------------------------------------------------------
# 3. 重复判定
# ---------------------------------------------------------------------------
def test_dispatch_plan_rejects_duplicate_open_plate(client, admin_token):
    c = client.app.state.container
    c.access.manual_register("京M12345", "in", "tester")
    r = client.post("/api/sim/dispatch-plan", headers=admin_token,
                    json={"plate": "京M12345", "plate_type": "blue", "duration_sec": 60})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "duplicate_entry"
    existing = body["detail"]["existing_record"]
    assert existing["plate"] == "京M12345" and existing["status"] == "in"
    # 不生成车牌图/不派车：photos 目录没有新文件
    photos = [p for p in (c.cfg.paths.photos_dir).rglob("*") if p.is_file()]
    before_dispatch = len(photos)
    r2 = client.post("/api/sim/dispatch-plan", headers=admin_token,
                     json={"plate": "京N54321", "plate_type": "blue", "duration_sec": 60})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    after_dispatch = len([p for p in (c.cfg.paths.photos_dir).rglob("*") if p.is_file()])
    assert after_dispatch >= before_dispatch + 1        # 新牌才生成图


# ---------------------------------------------------------------------------
# 4. 人工授权
# ---------------------------------------------------------------------------
def test_manual_authorize_entry_and_charge_exit(container):
    c = container
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.sim_time = 0.0
    assigned = []
    c.bus.subscribe(SpaceAssigned, assigned.append)
    entry = c.access.authorize_entry("veh-m1", "京P11111", operator="tester")
    assert entry["record_id"] and entry["space_no"]
    assert c.parking_repo.find_open_by_plate("京P11111").vehicle_id == "veh-m1"
    assert any(e.vehicle_id == "veh-m1" and e.space_no == entry["space_no"]
               for e in assigned)
    space = c.space_repo.get(entry["space_no"])
    assert space.status == "occupied" and space.current_plate == "京P11111"
    c.simulation.sim_time = 1200.0
    opened = []
    c.bus.subscribe(BarrierCommand, opened.append)
    out = c.access.authorize_exit("veh-m1", "京P11111", action="charge",
                                  operator="tester")
    assert out["status"] == "out" and out["payment_id"] and out["amount_paid"] == 5.0
    rec = c.parking_repo.get(entry["record_id"])
    assert rec.status == "out"
    assert c.space_repo.get(entry["space_no"]).status == "free"
    # 道闸：出口应收到开门指令
    assert any(e.gate == "out" for e in opened)   # 出口收到开门指令


def test_manual_authorize_exit_without_record(container):
    c = container
    # 无记录 charge → 明确 409（不静默卡死）
    with pytest.raises(NoParkingRecordError):
        c.access.authorize_exit("veh-x", "京Q22222", action="charge", operator="tester")
    # 无记录 free（必须原因）→ 临时账单放行
    with pytest.raises(ValidationError):
        c.access.authorize_exit("veh-x", "京Q22222", action="free",
                                reason="", operator="tester")
    result = c.access.authorize_exit("veh-x", "京Q22222", action="free",
                                     reason="无入场记录，免费放行", operator="tester")
    rec = c.parking_repo.get(result["record_id"])
    assert rec.temporary is True and rec.status in ("out", "abnormal")
    assert rec.space_no is None


def test_manual_authorize_api_entry_exit(client, admin_token):
    c = client.app.state.container
    c.cfg.server.demo_mode = True
    r = client.post("/api/manual/authorize-entry", headers=admin_token,
                    json={"vehicle_id": "veh-api-1", "plate": "京G33333"})
    assert r.status_code == 200, r.text
    record_id = r.json()["record_id"]
    c.simulation.sim_time = 900.0
    r2 = client.post("/api/manual/authorize-exit", headers=admin_token,
                     json={"vehicle_id": "veh-api-1", "plate": "京G33333",
                           "action": "charge"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "out"
    assert c.parking_repo.get(record_id).status == "out"


def test_ws_event_mapping_blacklist_and_manual_authorized():
    """3D 消费的 WS 映射：entry_denied / exit_authorized / manual_authorized。"""
    from server.gateway.ws_realtime import event_to_message

    denied = event_to_message(EntryDenied(
        vehicle_id="v-black", gate="in", plate="京A12345",
        reason="blacklist", message="车牌在黑名单中，禁止进场"))
    assert denied == {"type": "entry_denied", "vehicle_id": "v-black",
                      "gate": "in", "plate": "京A12345", "reason": "blacklist",
                      "message": "车牌在黑名单中，禁止进场",
                      "expected_plate": None, "image_path": None,
                      "mode": "blacklist"}
    out = event_to_message(ExitAuthorized(
        vehicle_id="v1", plate="京A12345", record_id=7, action="charge",
        amount_paid=5.0, space_no="A-01", reason="人工放行"))
    assert out["type"] == "exit_authorized"
    assert (out["vehicle_id"], out["plate"], out["gate"], out["record_id"],
            out["action"], out["amount_paid"], out["space_no"]) == (
        "v1", "京A12345", "out", 7, "charge", 5.0, "A-01")
    retry_req = event_to_message(ArrivalRetryRequested(
        vehicle_id="v3", gate="in", reason="retry_failed", message="请重新抓拍"))
    assert retry_req == {"type": "arrival_retry_requested", "vehicle_id": "v3",
                         "gate": "in", "reason": "retry_failed",
                         "message": "请重新抓拍"}
    man = event_to_message(ManualAuthorized(
        vehicle_id="v2", plate="京B12345", record_id=8, space_no="B-01",
        action="entry", reason="manual_authorize"))
    assert man["type"] == "manual_authorized"
    assert (man["vehicle_id"], man["plate"], man["gate"], man["record_id"],
            man["space_no"], man["action"]) == (
        "v2", "京B12345", "in", 8, "B-01", "entry")


def test_manual_authorize_publishes_manual_authorized_event(container):
    c = container
    events = []
    c.bus.subscribe(ManualAuthorized, events.append)
    entry = c.access.authorize_entry("veh-m2", "京C12345", operator="tester")
    assert len(events) == 1
    e = events[0]
    assert (e.vehicle_id, e.plate, e.gate, e.record_id, e.space_no) == (
        "veh-m2", "京C12345", "in", entry["record_id"], entry["space_no"])
    assert e.action == "entry"


def test_ws_receives_manual_authorized_and_exit_authorized(client, admin_token):
    """WS 端到端：人工授权入场/出场后，3D 能收到 manual_authorized / exit_authorized。"""
    c = client.app.state.container
    token = admin_token["Authorization"].replace("Bearer ", "")
    with client.websocket_connect("/ws", subprotocols=[f"token.{token}"]) as ws:
        r = client.post("/api/manual/authorize-entry", headers=admin_token,
                        json={"vehicle_id": "veh-ws", "plate": "京D12345"})
        assert r.status_code == 200, r.text
        got = set()
        for _ in range(30):
            msg = ws.receive_json()
            if msg.get("type") == "sync":
                continue
            got.add(msg.get("type"))
            if msg.get("type") == "manual_authorized":
                assert msg["vehicle_id"] == "veh-ws" and msg["gate"] == "in"
                break
        assert "manual_authorized" in got
        c.simulation.sim_time = 1200.0
        r2 = client.post("/api/manual/authorize-exit", headers=admin_token,
                         json={"vehicle_id": "veh-ws", "plate": "京D12345",
                               "action": "charge"})
        assert r2.status_code == 200, r2.text
        seen_exit = None
        for _ in range(30):
            msg = ws.receive_json()
            if msg.get("type") == "exit_authorized":
                seen_exit = msg
                break
        assert seen_exit is not None
        assert seen_exit["vehicle_id"] == "veh-ws" and seen_exit["gate"] == "out"
        assert seen_exit["action"] == "charge"


def test_manual_open_is_physical_only(container):
    c = container
    before_records = c.parking_repo.count_open()
    c.access.manual_open("in", operator="tester", reason="物理抬杆")
    assert c.parking_repo.count_open() == before_records


# ---------------------------------------------------------------------------
# 5. 黑名单
# ---------------------------------------------------------------------------
def test_blacklist_publishes_entry_denied_without_pending_or_record(container, monkeypatch):
    c = container
    c.cfg.server.demo_mode = True
    black = "京A44444"
    c.list_repo.upsert(plate=black, list_type="black", note="测试黑名单")
    denied = []
    c.bus.subscribe(EntryDenied, denied.append)
    # 绕过真实识别：直接给"识别成功"结果，让流程走到 _authorize_entry 的黑名单分支
    monkeypatch.setattr(c.access, "_recognize", lambda gate, event: (
        black, "blue", 0.99, False, [], None, "ok", None))
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-black", gate="in", image_path=None))
    assert len(denied) == 1
    assert denied[0].plate == black and denied[0].vehicle_id == "veh-black"
    assert denied[0].reason == "blacklist"
    assert c.parking_repo.find_open_by_plate(black) is None
    assert c.access.list_pending("pending") == []       # 不建普通 pending 阻塞
    assert c.parking_repo.count_open() == 0


def test_blacklist_resolution_api(client, admin_token):
    c = client.app.state.container
    r = client.post("/api/sim/blacklist-resolution", headers=admin_token,
                    json={"vehicle_id": "veh-black", "plate": "京B55555",
                          "resolution": "entered_then_left", "note": "后方有车"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["resolution"] == "entered_then_left"
    assert body["seq"] >= 1
    c.db.session.expire_all()
    events = c.state.events(since=0, limit=100)["items"]
    assert any(e["name"] == "blacklist_resolution"
               and e["data"]["plate"] == "京B55555" for e in events)
    assert c.parking_repo.find_open_by_plate("京B55555") is None
    assert c.parking_repo.count_open() == 0


# ---------------------------------------------------------------------------
# 5b. 黑名单后车自动恢复 + authorize-entry 清理旧 pending（收尾修复）
# ---------------------------------------------------------------------------
def test_blacklist_resolution_auto_enters_waiting_rear_vehicle(client, admin_token, monkeypatch):
    """黑名单前车倒车离场后，后车（已有 pending）自动重试入场，不再需要 admin 兜底。"""
    c = client.app.state.container
    black_plate, rear_plate = "京A44444", "京M55555"
    c.list_repo.upsert(plate=black_plate, list_type="black", note="测试黑名单")
    status = {"rear_ok": False}

    def fake_recognize(gate, event):
        if event.vehicle_id == "veh-black-front":
            return (black_plate, "blue", 0.99, False, [], None, "ok", None)
        if status["rear_ok"]:
            return (rear_plate, "blue", 0.99, False, [rear_plate], None, "ok", None)
        return (None, "blue", 0.0, True, [], None, "low_conf", None)

    monkeypatch.setattr(c.access, "_recognize", fake_recognize)
    # 1) 前车黑名单 → EntryDenied，不建 pending
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-black-front", gate="in", image_path="frames/front.png"))
    assert c.access.list_pending("pending") == []
    # 2) 后车到达但识别失败 → 停在 pending（旧实现下会卡住等待 admin）
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-rear", gate="in", image_path="frames/rear.png"))
    pending = c.access.list_pending("pending")
    assert len(pending) == 1 and pending[0]["vehicle_id"] == "veh-rear"
    # 3) 前车处置完成（倒车离场），后车识别恢复 → blacklist-resolution 自动重试后车入场
    status["rear_ok"] = True
    r = client.post("/api/sim/blacklist-resolution", headers=admin_token,
                    json={"vehicle_id": "veh-black-front", "plate": black_plate,
                          "resolution": "reversed", "note": "前方黑名单车辆倒车离场"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["retried"] >= 1 and body["authorized"] >= 1
    assert "veh-rear" in body["retried_vehicle_ids"]
    rec = c.parking_repo.find_open_by_plate(rear_plate, include_temporary=False)
    assert rec is not None and rec.space_no, "后车应自动授权入场"
    assert rec.vehicle_id == "veh-rear"
    assert c.access.list_pending("pending") == []
    rejected = [p for p in c.access.list_pending("all")
                if p["vehicle_id"] == "veh-rear" and p["status"] == "rejected"]
    assert rejected, "旧 pending 应保留 rejected 审计"


def test_blacklist_retry_failure_requeues_without_blocking_following_vehicle(
        container, monkeypatch):
    """重试仍失败 → 重新入队（保留旧审计）；不阻塞同队列后续车辆；恢复后自动清空。"""
    c = container
    c.cfg.server.demo_mode = True
    state = {"stuck_ok": False}
    stuck_plate, next_plate = "京N77777", "京P88888"

    def fake_recognize(gate, event):
        if event.vehicle_id == "veh-next":
            return (next_plate, "blue", 0.99, False, [next_plate], None, "ok", None)
        if state["stuck_ok"]:
            return (stuck_plate, "blue", 0.99, False, [stuck_plate], None, "ok", None)
        return (None, "blue", 0.0, True, [], None, "low_conf", None)

    retry_requests = []
    c.bus.subscribe(ArrivalRetryRequested, retry_requests.append)
    monkeypatch.setattr(c.access, "_recognize", fake_recognize)
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-stuck", gate="in", image_path="frames/stuck.png"))
    old = c.access.list_pending("pending")
    assert len(old) == 1
    r1 = c.access.retry_waiting_vehicles(
        "in", operator="system", include_all=True,
        resolution="blacklist_resolved_retry")
    assert r1["retried"] == 1 and r1["failed"] == 1 and r1["authorized"] == 0
    assert retry_requests and retry_requests[0].vehicle_id == "veh-stuck"
    requeued = c.access.list_pending("pending")
    assert len(requeued) == 1 and requeued[0]["id"] != old[0]["id"]
    # 后续车辆识别正常仍可入场（前车失败重试不阻塞队列）
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-next", gate="in", image_path="frames/next.png"))
    assert c.parking_repo.find_open_by_plate(next_plate, include_temporary=False) is not None
    # 后车识别恢复后再次重试 → 自动入场且 pending 清零
    state["stuck_ok"] = True
    r2 = c.access.retry_waiting_vehicles(
        "in", operator="system", include_all=True,
        resolution="blacklist_resolved_retry")
    assert r2["retried"] >= 1 and r2["authorized"] >= 1
    assert c.parking_repo.find_open_by_plate(stuck_plate, include_temporary=False) is not None
    assert c.access.list_pending("pending") == []


def test_authorize_entry_resolves_old_pending(client, admin_token, monkeypatch):
    """authorize-entry 成功 → 同 vehicle_id 旧 pending 自动 confirmed，pending=0。"""
    c = client.app.state.container
    monkeypatch.setattr(c.access, "_recognize",
                        lambda gate, event: (None, "blue", 0.0, True, [], None,
                                             "low_conf", None))
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-pending-auth", gate="in", image_path="frames/p.png"))
    assert len(c.access.list_pending("pending")) == 1
    r = client.post("/api/manual/authorize-entry", headers=admin_token,
                    json={"vehicle_id": "veh-pending-auth", "plate": "京N66666"})
    assert r.status_code == 200, r.text
    assert r.json()["resolved_pending"], "响应应返回被清理的旧 pending"
    assert c.access.list_pending("pending") == []
    rows = [p for p in c.access.list_pending("all")
            if p["vehicle_id"] == "veh-pending-auth"]
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["operator"] == "admin"
    assert c.parking_repo.find_open_by_plate("京N66666", include_temporary=False) is not None


def test_repeated_arrival_after_auto_authorization_is_idempotent(
        client, admin_token, monkeypatch):
    """自动恢复入场后 3D 补发同 vehicle_id arrival → 幂等忽略，不新增记录/pending。"""
    c = client.app.state.container
    plate = "京Q77777"
    status = {"ok": False}

    def fake_recognize(gate, event):
        if event.vehicle_id == "veh-idem" and status["ok"]:
            return (plate, "blue", 0.99, False, [plate], None, "ok", None)
        return (None, "blue", 0.0, True, [], None, "low_conf", None)

    monkeypatch.setattr(c.access, "_recognize", fake_recognize)
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-idem", gate="in", image_path="frames/i.png"))
    assert len(c.access.list_pending("pending")) == 1
    status["ok"] = True
    r = client.post("/api/sim/blacklist-resolution", headers=admin_token,
                    json={"vehicle_id": "veh-front", "plate": "京A44444",
                          "resolution": "reversed"})
    assert r.status_code == 200, r.text
    assert c.parking_repo.find_open_by_plate(plate, include_temporary=False) is not None
    count = c.parking_repo.count_open()
    # 3D 清理后补发 arrival（含新帧）→ 幂等，不重复建单/建待办
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-idem", gate="in", image_path="frames/i2.png"))
    assert c.parking_repo.count_open() == count
    assert c.parking_repo.find_open_by_plate(plate, include_temporary=False) is not None
    assert c.access.list_pending("pending") == []


def test_blacklist_resolution_uses_sim_camera_expected_plate_when_frame_low_conf(
        client, admin_token, monkeypatch):
    """后车旧帧不可读（low_conf）时，以 3D expected_plate 兜底自动放行（仍走完整校验）。

    复现 qa-runtime debug：后车排在黑名单前车后方，抓拍帧置信度不足；前车处置后
    后端重试同一旧帧仍 low_conf → 用 arriving 事件的 expected_plate 完成授权。
    """
    c = client.app.state.container
    rear_plate = "京Q55555"
    # 识别器始终无法从后车旧帧读出车牌（模拟后车被前车遮挡/距离远的帧）
    monkeypatch.setattr(c.access, "_recognize",
                        lambda gate, event: (None, "blue", 0.0, True, [], None,
                                             "low_conf", None))
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-rear-cam", gate="in", image_path="frames/rear-low.png",
        expected_plate=rear_plate, source="sim_camera"))
    pending = c.access.list_pending("pending")
    assert len(pending) == 1 and pending[0]["reason"] == "low_conf"
    r = client.post("/api/sim/blacklist-resolution", headers=admin_token,
                    json={"vehicle_id": "veh-black-front", "plate": "京A44444",
                          "resolution": "entered_then_left",
                          "note": "后方有车，前车进场后直接出场"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authorized"] >= 1 and body["expected_authorized"] >= 1, body
    rec = c.parking_repo.find_open_by_plate(rear_plate, include_temporary=False)
    assert rec is not None and rec.vehicle_id == "veh-rear-cam"
    assert rec.space_no
    assert c.parking_repo.count_open() == 1
    assert c.access.list_pending("pending") == []


# ---------------------------------------------------------------------------
# 6. 剧本校验
# ---------------------------------------------------------------------------
def _scenario(vehicles):
    return {"name": "测试剧本", "seed": 1, "time_unit": "sim_sec",
            "vehicles": vehicles}


def test_scenario_rejects_same_plate_overlap():
    from server import scenario

    base = {"vid": "V1", "t": 0.0, "plate": "京C66666",
            "plate_type": "blue", "duration": 600.0}
    clash = {"vid": "V2", "t": 300.0, "plate": "京C66666",
             "plate_type": "blue", "duration": 600.0}
    with pytest.raises(ValueError, match="入场时段重叠"):
        scenario.validate_scenario(_scenario([base, clash]))
    # 首尾相接（t2 == t1+duration）允许
    ok_adjacent = scenario.validate_scenario(_scenario(
        [base, {**clash, "t": 600.0}]))
    assert len(ok_adjacent["vehicles"]) == 2
    # 不同车牌同时段允许
    other = {"vid": "V3", "t": 100.0, "plate": "京E77777",
             "plate_type": "blue", "duration": 600.0}
    assert len(scenario.validate_scenario(_scenario([base, other]))["vehicles"]) == 2


def test_scenario_sim_seconds_and_free_billing(container):
    """默认剧本时间单位=仿真秒，且业务计费按仿真时长（10 分钟免费 / 30 分钟收费）。"""
    c = container
    c.cfg.server.demo_mode = True
    c.simulation.epoch = EPOCH
    c.simulation.sim_time = 0.0
    free = c.access.manual_register("京F11111", "in", "tester")
    charge = c.access.manual_register("京F22222", "in", "tester")
    c.simulation.sim_time = 600.0          # 10 分钟
    assert c.billing.quote("京F11111").amount_due == 0.0
    c.simulation.sim_time = 1800.0         # 30 分钟
    q = c.billing.quote("京F22222")
    assert q.minutes == 30 and q.amount_due == 5.0
    assert q.duration_sim_sec == 1800.0
    assert c.parking_repo.get(free["record_id"]).entry_sim_sec == 0.0
    assert c.parking_repo.get(charge["record_id"]).entry_sim_sec == 0.0
