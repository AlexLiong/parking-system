"""车牌规范/渲染 API + 场景剧本 API 测试。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from server.lpr import charset
from server.lpr.render import PlateFonts, render_plate


def _plate(type_="blue"):
    return charset.generate_plate_type(type_, rng=__import__("random").Random(11))


def test_plate_type_rules_and_city_codes():
    for type_ in charset.PLATE_TYPES:
        for _ in range(10):
            plate = charset.generate_plate_type(type_)
            ok, reason = charset.validate_plate_type(plate, type_)
            assert ok, (type_, plate, reason)
            assert plate[1] in charset.valid_city_codes(plate[0])
    assert charset.validate_plate_type("京Z12345", "blue")[0] is False
    assert charset.validate_plate_type("京A12345", "blue")[0] is True
    assert charset.validate_plate_type("京A12345D", "green_large")[0] is True
    assert charset.validate_plate_type("京A12345", "green_large")[0] is False
    assert charset.validate_plate_type("京A123456", "blue")[0] is False
    # 小绿必须首位 D/F，大绿必须末位 D/F
    assert charset.validate_plate_type("沪UD76840", "green")[0] is True
    assert charset.validate_plate_type("沪A12345D", "green_large")[0] is True


def test_render_all_plate_types_use_mit_assets():
    fonts = PlateFonts()
    for type_ in charset.PLATE_TYPES:
        plate = _plate(type_)
        image = render_plate(plate, type_, fonts)
        assert image.ndim == 3 and image.shape[2] == 3
        assert image.shape[0] == 140
        assert image.shape[1] == (440 if type_ in ("blue", "yellow") else 480)


def test_demo_gallery_has_18_types_and_matching_labels():
    root = Path(__file__).resolve().parents[1] / "datasets" / "demo-lpr"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    samples = manifest["samples"]
    assert len(samples) == 18
    assert {s["plate_type"] for s in samples} == set(charset.PLATE_TYPES)
    for sample in samples:
        data = (root / sample["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sample["sha256"]
        assert charset.validate_plate_type(sample["plate"], sample["plate_type"])[0]
        assert sample["color"] == charset.plate_type_spec(sample["plate_type"])["color"]
    assert len(list(root.glob("*-clean.png"))) == 18


def test_plate_image_api_and_dispatch_plan(client, admin_token):
    c = client.app.state.container
    before = c.parking_repo.count_open()
    plate = _plate("yellow")
    r = client.post("/api/sim/plate-image",
                    json={"plate": plate, "plate_type": "yellow"},
                    headers=admin_token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["plate"] == plate and body["plate_type"] == "yellow"
    assert body["color"] == "yellow" and body["vehicle_kind"] == "van"
    assert body["image_url"].startswith("/media/uploads/dispatch/")
    assert Path(body["image_path"]).exists()
    assert client.get(body["image_url"], headers=admin_token).status_code == 200

    r2 = client.post("/api/sim/dispatch-plan",
                     json={"plate": plate, "plate_type": "yellow", "duration_sec": 120},
                     headers=admin_token)
    assert r2.status_code == 200
    assert r2.json()["duration_sec"] == 120.0
    assert r2.json()["image_url"].startswith("/media/uploads/dispatch/")
    assert c.parking_repo.count_open() == before


def test_plate_image_api_rejects_invalid_type_and_city(client, admin_token):
    r = client.post("/api/sim/plate-image",
                    json={"plate": "京A12345", "plate_type": "white"},
                    headers=admin_token)
    assert r.status_code == 422
    assert "plate_type" in r.text
    r = client.post("/api/sim/plate-image",
                    json={"plate": "京Z12345", "plate_type": "blue"},
                    headers=admin_token)
    assert r.status_code == 422
    assert "城市码" in r.text


def _custom_scenario():
    return {
        "name": "自定义测试剧本",
        "description": "免费+收费，两车",
        "seed": 7,
        "time_unit": "sim_sec",
        "vehicles": [
            {"vid": "A1", "t": 1, "plate": _plate("blue"), "plate_type": "blue",
             "duration": 600},
            {"vid": "A2", "t": 3, "plate": _plate("green"), "plate_type": "green",
             "duration": 1800},
        ],
        "steps": [
            {"t": 1, "action": "spawn", "vid": "A1"},
            {"t": 601, "action": "exit", "vid": "A1"},
        ],
    }


def test_admin_scenario_crud_and_sim_priority(client, admin_token):
    c = client.app.state.container
    builtin = client.get("/api/admin/scenario", headers=admin_token)
    assert builtin.status_code == 200
    base = builtin.json()
    assert base["source"] == "builtin"
    assert len(base["vehicles"]) == 6
    types = {v["plate_type"] for v in base["vehicles"]}
    assert types == set(charset.PLATE_TYPES)
    assert c.parking_repo.count_open() == 0

    custom = _custom_scenario()
    r = client.put("/api/admin/scenario", json=custom, headers=admin_token)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["source"] == "data"
    saved = r.json()["scenario"]
    assert saved["name"] == custom["name"]
    assert (Path(c.cfg.paths.data_dir) / "scenario.json").exists()
    # admin 与 /sim/scenario 都优先 data
    got = client.get("/api/admin/scenario", headers=admin_token).json()
    assert got["source"] == "data" and got["name"] == custom["name"]
    sim = client.get("/sim/scenario", headers=admin_token).json()
    assert sim["name"] == custom["name"] and len(sim["vehicles"]) == 2
    assert c.parking_repo.count_open() == 0

    reset = client.post("/api/admin/scenario/reset", headers=admin_token)
    assert reset.status_code == 200 and reset.json()["source"] == "builtin"
    assert not (Path(c.cfg.paths.data_dir) / "scenario.json").exists()
    sim2 = client.get("/sim/scenario", headers=admin_token).json()
    assert sim2["source"] == "builtin" and len(sim2["vehicles"]) == 6


def test_scenario_validation_rejects_bad_payloads(client, admin_token):
    bad = _custom_scenario()
    bad["vehicles"][1]["vid"] = bad["vehicles"][0]["vid"]
    assert client.put("/api/admin/scenario", json=bad, headers=admin_token).status_code == 422

    bad = _custom_scenario()
    bad["vehicles"][0]["plate_type"] = "white"
    assert client.put("/api/admin/scenario", json=bad, headers=admin_token).status_code == 422

    bad = _custom_scenario()
    bad["vehicles"][0]["plate"] = "京Z12345"
    assert client.put("/api/admin/scenario", json=bad, headers=admin_token).status_code == 422

    bad = _custom_scenario()
    bad["vehicles"][0]["t"] = -1
    assert client.put("/api/admin/scenario", json=bad, headers=admin_token).status_code == 422

    bad = _custom_scenario()
    bad["steps"][0]["action"] = "fly"
    assert client.put("/api/admin/scenario", json=bad, headers=admin_token).status_code == 422


def test_legacy_steps_only_scenario_accepted(client, admin_token):
    legacy = {
        "name": "旧 steps 剧本",
        "description": "兼容",
        "seed": 1,
        "steps": [{"t": 1, "action": "spawn", "vid": "E1", "plate": "湘N7D071"}],
    }
    r = client.put("/api/admin/scenario", json=legacy, headers=admin_token)
    assert r.status_code == 200, r.text
    got = client.get("/sim/scenario", headers=admin_token).json()
    assert got["name"] == "旧 steps 剧本"
    assert got["steps"][0]["action"] == "spawn"
    client.post("/api/admin/scenario/reset", headers=admin_token)
