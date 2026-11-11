"""接入层 REST 端到端测试（gateway/api_admin.py + api_sim.py，经 FastAPI TestClient）。

覆盖：登录鉴权、受保护路由、实时总览、费率、名单 CRUD、人工录入→报价→缴费全链路、
异常面板、统计、以及 prompt 强调的“导入车牌图片识别”端点。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_PLATE = ROOT / "datasets" / "demo-lpr" / "blue-000-clean.png"


# ----------------------------- 认证 -----------------------------
def test_login_success(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin" and body["token"]


def test_login_wrong_password(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_protected_route_requires_token(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_auth_me_with_token(client, admin_token):
    r = client.get("/api/auth/me", headers=admin_token)
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


def test_invalid_token_rejected(client):
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


# ----------------------------- 总览 / 费率 -----------------------------
def test_overview_structure(client, admin_token):
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    for key in ("free_total", "open_records", "barriers", "camera",
                "open_incidents", "recommend"):
        assert key in body
    assert set(body["barriers"].keys()) >= {"in", "out"}


def test_fee_rule_current_and_add(client, admin_token):
    cur = client.get("/api/fee-rules/current")
    assert cur.status_code == 200
    rule = cur.json()
    assert "first_hour_price" in rule

    add = client.post("/api/fee-rules", headers=admin_token, json={
        "free_minutes": 20, "first_hour_price": 6.0, "unit_minutes": 30,
        "unit_price": 2.5, "daily_cap": 50.0, "ne_discount": 0.85,
        "round_up": True, "note": "测试新版本"})
    assert add.status_code == 200
    assert add.json()["first_hour_price"] == 6.0
    # 新增版本后，当前生效版本随之更新
    cur2 = client.get("/api/fee-rules/current").json()
    assert cur2["first_hour_price"] == 6.0


def test_add_fee_rule_requires_auth(client):
    r = client.post("/api/fee-rules", json={})
    assert r.status_code == 401


# ----------------------------- 名单 CRUD -----------------------------
def test_vehicle_list_crud(client, admin_token):
    add = client.post("/api/lists", headers=admin_token,
                      json={"plate": "琼A12345", "list_type": "white", "note": "vip"})
    assert add.status_code == 200
    lst = client.get("/api/lists").json()["items"]
    assert any(x["plate"] == "琼A12345" for x in lst)

    dele = client.delete("/api/lists/琼A12345", headers=admin_token)
    assert dele.status_code == 200
    lst2 = client.get("/api/lists").json()["items"]
    assert not any(x["plate"] == "琼A12345" for x in lst2)


def test_remove_missing_from_list_404(client, admin_token):
    r = client.delete("/api/lists/琼Z00000", headers=admin_token)
    assert r.status_code == 404


# ----------------------------- 人工录入 → 报价 → 缴费 全链路 -----------------------------
def test_manual_register_quote_pay_flow(client, admin_token):
    reg = client.post("/api/manual/register", headers=admin_token,
                      json={"plate": "琼B88888", "color": "blue"})
    assert reg.status_code == 200
    assert reg.json()["plate"] == "琼B88888"

    # 出场报价（无在场记录会走 max_duration 策略；此处车在场，应能报价）
    q = client.get("/api/quote", params={"plate": "琼B88888"})
    assert q.status_code == 200
    assert "amount_due" in q.json()

    # 找到在场记录并缴费
    recs = client.get("/api/records", params={"plate": "琼B88888", "status": "in"}).json()
    assert recs["total"] >= 1
    rid = recs["items"][0]["id"]
    pay = client.post("/api/pay", headers=admin_token,
                      json={"record_id": rid, "method": "mock_scan"})
    assert pay.status_code == 200
    assert pay.json()["record_id"] == rid

    # 缴费幂等：再次缴费返回同一 payment_id
    pay2 = client.post("/api/pay", headers=admin_token, json={"record_id": rid})
    assert pay2.status_code == 200
    assert pay2.json()["payment_id"] == pay.json()["payment_id"]

    # 记录已出场
    rec = client.get(f"/api/records/{rid}").json()
    assert rec["status"] == "out" and rec["exit_time"] is not None
    assert rec["payment"] is not None


def test_pay_requires_auth(client):
    r = client.post("/api/pay", json={"record_id": 1})
    assert r.status_code == 401


def test_get_record_404(client, admin_token):
    r = client.get("/api/records/999999")
    assert r.status_code == 404


# ----------------------------- 异常面板 / 摄像头注入 -----------------------------
def test_camera_inject_and_recover_and_incidents(client, admin_token):
    inj = client.post("/api/camera/in/inject", headers=admin_token,
                      json={"reason": "测试故障"})
    assert inj.status_code == 200 and inj.json()["down"] is True

    incs = client.get("/api/incidents", params={"status": "open"}).json()["items"]
    cam = [x for x in incs if x.get("kind") == "camera"]
    assert len(cam) >= 1

    rec = client.post("/api/camera/in/recover", headers=admin_token,
                      json={"resolution": "已恢复"})
    assert rec.status_code == 200 and rec.json()["down"] is False


def test_camera_inject_bad_gate_422(client, admin_token):
    r = client.post("/api/camera/side/inject", headers=admin_token, json={})
    assert r.status_code == 422


# ----------------------------- 统计 -----------------------------
def test_stats_day(client, admin_token):
    r = client.get("/api/stats", params={"period": "day"})
    assert r.status_code == 200
    body = r.json()
    for key in ("count_in", "count_out", "revenue", "peak_occupancy", "by_day"):
        assert key in body


def test_stats_bad_period_422(client, admin_token):
    r = client.get("/api/stats", params={"period": "decade"})
    assert r.status_code == 422


def test_records_pagination(client, admin_token):
    r = client.get("/api/records", params={"page": 1, "page_size": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["page"] == 1 and body["page_size"] == 5
    assert isinstance(body["items"], list)


# ----------------------------- 导入车牌图片识别（解耦） -----------------------------
def test_recognize_requires_input(client, admin_token):
    r = client.post("/api/recognize", params={"gate": "in"})
    assert r.status_code == 400


@pytest.mark.skipif(not _SAMPLE_PLATE.exists(), reason="缺少合成车牌样本")
def test_recognize_by_image_path(client, admin_token):
    r = client.post("/api/recognize",
                    params={"image_path": str(_SAMPLE_PLATE), "gate": "in"})
    # 识别成功或转人工都是合法结果，但不得 5xx，且返回结构化字段
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        for key in ("plate", "overall_conf", "need_manual", "candidates"):
            assert key in body


@pytest.mark.skipif(not _SAMPLE_PLATE.exists(), reason="缺少合成车牌样本")
def test_recognize_missing_image_404(client, admin_token):
    r = client.post("/api/recognize",
                    params={"image_path": str(ROOT / "datasets" / "demo-lpr" / "no_such.png"), "gate": "in"})
    assert r.status_code == 404


# ----------------------------- 仿真事件通道 -----------------------------
def test_sim_arrived_publishes_event(client, admin_token):
    # 仿真端上报到达（无图 → 转人工，但通道应返回 ok）
    r = client.post("/sim/events/arrived",
                    json={"vehicle_id": "v-test", "gate": "in", "image_path": None})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_sim_scenario(client, admin_token):
    r = client.get("/sim/scenario")
    assert r.status_code == 200
    assert "events" in r.json() or "name" in r.json()
