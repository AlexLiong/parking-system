"""操作员角色权限隔离：运营操作可用，配置类写操作仅管理员。"""
from __future__ import annotations

from server.gateway.auth import create_token


def _operator_headers(client):
    r = client.post("/api/auth/login", json={"username": "operator", "password": "operator123"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "operator"
    return {"Authorization": f"Bearer {body['token']}"}


def test_operator_can_do_operational_work_but_not_config(client, admin_token):
    headers = _operator_headers(client)

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "operator"

    # 运营读取和人工操作可用
    assert client.get("/api/records", headers=headers).status_code == 200
    opened = client.post("/api/manual/open-barrier", headers=headers,
                         json={"gate": "in", "reason": "operator test"})
    assert opened.status_code == 200, opened.text

    # 配置写操作被统一中间件拒绝
    assert client.post("/api/fee-rules", headers=headers, json={
        "free_minutes": 15, "first_hour_price": 5, "unit_minutes": 30,
        "unit_price": 2, "daily_cap": 40, "ne_discount": 0.9,
        "round_up": True,
    }).status_code == 403
    assert client.post("/api/lists", headers=headers, json={
        "plate": "京Z99999", "list_type": "white",
    }).status_code == 403
    assert client.post("/api/admin/layout", headers=headers, json={
        "zones": [{"zone": "A", "count": 20}],
    }).status_code == 403
    assert client.put("/api/admin/scenario", headers=headers, json={}).status_code == 403
    assert client.get("/api/admin/layout/export", headers=headers).status_code == 403
    assert client.post("/api/demo/reset", headers=headers, json={"request_id": "operator-reset"}).status_code == 403

    # 管理员仍可执行配置操作
    assert client.post("/api/lists", headers=admin_token, json={
        "plate": "京Z99998", "list_type": "white",
    }).status_code == 200


def test_operator_config_read_is_available_for_3d_scenario(client):
    headers = _operator_headers(client)
    # 3D 仿真需要用 operator demo token 读取剧本；只读，不放开写入。
    assert client.get("/api/admin/scenario", headers=headers).status_code == 200


def test_simulator_token_can_still_reset_demo(client):
    c = client.app.state.container
    sim = {"Authorization": "Bearer " + create_token("simulator", "operator", c.cfg)}
    result = client.post("/api/demo/reset", headers=sim, json={
        "request_id": "simulator-reset", "mode": "soft", "new_session": True, "force": True,
    })
    assert result.status_code == 200, result.text
