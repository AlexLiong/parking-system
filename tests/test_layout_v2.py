"""route-v2 后端布局改造测试（layout manifest / generator / service / API / 迁移）。

覆盖点（对应 Lead 任务 task-12）：
1. manifest 加载/校验：版本、manifest_hash（规范化算法）、节点边完整性、
   缺失/非法时回退旧常量并告警；
2. generator：行 y 与支路宽度从 manifest 读取（A/C 25.75/13.25，B/D −7.25/−19.75，
   排距 12.5），x 起点/列距/列数沿用，仍生成 80 车位；
3. service：``_gates_dict()`` 出口闸 y=−34.0、停止线漆按公式；``_scene_dict()``
   新增 aisle_w/aisle_n/aisle_s/median_half/plaza/stop_paint/gate_out_y/
   road_graph_version/manifest_hash，且**旧字段全部保留**；
4. API：``GET /api/layout`` 顶层返回 road_graph_version/manifest_hash，``/assets3d``
   静态挂载可只读拉到 manifest；config.scene 与 manifest 不一致时能检测告警；
5. 迁移脚本：dry-run 不写库、正式迁移版本 +1 且坐标/占用校验通过、二次运行幂等、
   ``--empty-lot`` 有在场记录时拒绝。

说明：manifest 由 Lead 侧脚本生成且可能微调（hash 会变），因此测试**不硬编码
hash**，而是用 ``canonical_hash()`` 重算比对；坐标等设计常量作为契约断言。
"""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from server.core.domain import SpaceKind, ZoneDecl
from server.gateway.context import Container
from server.layout.generator import LayoutGenerator, zone_geom_from_manifest
from server.layout.manifest import (
    ENV_MANIFEST,
    Manifest,
    canonical_hash,
    clear_cache,
    load_manifest,
    resolve_assets_root,
    resolve_manifest_path,
    scene_config_issues,
    validate_manifest,
)
from server.layout.migrate import run_migration
from server.layout.service import LayoutService
from server.access.allocator import SpaceAllocator

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_REAL = ROOT.parent / "assets_3d" / "layout" / "lot_v2.json"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    """每个用例前后清空 manifest 缓存，避免测试间互相污染。"""
    clear_cache()
    yield
    clear_cache()


def _manifest_raw() -> dict:
    assert MANIFEST_REAL.is_file(), f"缺少 manifest: {MANIFEST_REAL}"
    return json.loads(MANIFEST_REAL.read_text(encoding="utf-8"))


def _write_manifest(tmp_path: Path, mutate=None) -> Path:
    """复制真实 manifest 到 tmp（可选 mutate），用于非法/边界用例。"""
    raw = _manifest_raw()
    if mutate is not None:
        mutate(raw)
    p = tmp_path / "lot_v2_test.json"
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _legacy_coords(space_no: str) -> float:
    """route-v1 的排 y（用于模拟旧库）。"""
    idx = int(space_no.split("-")[1])
    zone = space_no[0]
    if zone in ("A", "C"):
        return 25.0 if idx <= 10 else 14.0
    return -14.0 if idx <= 10 else -25.0


def _space_spec(gen: LayoutGenerator, zone: str, space_no: str):
    specs = gen.generate([ZoneDecl(zone=zone, count=20)])
    return next(s for s in specs if s.space_no == space_no)


def _db_coords(db_file: Path) -> dict[str, float]:
    con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    try:
        return {row[0]: row[1] for row in
                con.execute("select space_no, z from parking_space")}
    finally:
        con.close()


def _simulate_legacy_coords(container: Container) -> None:
    with container.db.scope():
        for sp in container.space_repo.list_all():
            sp.z = _legacy_coords(sp.space_no)
        container.db.commit()


# ---------------------------------------------------------------------------
# 1. manifest 加载 / 校验
# ---------------------------------------------------------------------------
def test_manifest_resolves_and_loads():
    assert resolve_manifest_path() == MANIFEST_REAL.resolve()
    m = load_manifest()
    assert isinstance(m, Manifest)
    assert m.manifest_version >= 2
    assert m.road_graph_version == 2
    assert m.coordinate_system == "blender_xy"
    assert m.manifest_hash.startswith("sha256:")
    # 契约：115 节点 / 113 边 / 7 冲突区 / 80 车位
    assert len(m.nodes) == 115
    assert len(m.edges) == 113
    assert [z["id"] for z in m.conflict_zones] == ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7"]
    assert sum(g.capacity for g in m.space_grid.values()) == 80
    assert m.issues  # 目前含 8 条 turn 零长边 WARN（非致命）


def test_manifest_hash_matches_canonical_recompute():
    raw = _manifest_raw()
    assert raw["manifest_hash"] == canonical_hash(raw)


def test_manifest_design_constants():
    m = load_manifest()
    assert m.row_geoms("A")[0].y == 25.75
    assert m.row_geoms("A")[1].y == 13.25
    assert m.row_geoms("B")[0].y == -7.25
    assert m.row_geoms("B")[1].y == -19.75
    assert m.aisle_width == 6.5
    assert m.row_pitch == 12.5
    assert m.stop_paint_y("in") == -25.8
    assert m.stop_paint_y("out") == -33.7
    assert m.stop_center_y("in") == -28.4
    assert m.stop_center_y("out") == -31.1
    assert m.queue_length("entry") == 17.1
    assert m.queue_length("exit") == 14.35
    assert m.gate("out")["y"] == -34.0
    assert m.corridor["median_half"] == 1.75


def test_manifest_node_edge_integrity():
    m = load_manifest()
    for e in m.edges:
        assert str(e["u"]) in m.nodes, f"边 {e['id']} 起点不存在"
        assert str(e["v"]) in m.nodes, f"边 {e['id']} 终点不存在"
        assert float(e["length"]) >= 0
    zone_ids = {str(z["id"]) for z in m.conflict_zones}
    for e in m.edges:
        for zid in e.get("zones") or []:
            assert zid in zone_ids
    for zone in "ABCD":
        for key in ("entry", "exit"):
            node_id = m.zone_ports[zone][key]
            assert node_id in m.nodes


def test_manifest_missing_returns_none(tmp_path):
    clear_cache()
    assert load_manifest(path=tmp_path / "no_such_manifest.json") is None


def test_manifest_hash_mismatch_rejected(tmp_path):
    def mutate(raw: dict) -> None:
        raw["nodes"][0]["x"] = float(raw["nodes"][0]["x"]) + 1.0  # 不改 hash

    p = _write_manifest(tmp_path, mutate)
    clear_cache()
    assert load_manifest(path=p) is None
    ok, issues = validate_manifest(json.loads(p.read_text(encoding="utf-8")), p)
    assert not ok
    assert any("manifest_hash 不匹配" in i for i in issues)


def test_manifest_broken_edge_rejected(tmp_path):
    def mutate(raw: dict) -> None:
        raw["edges"][0]["u"] = "NO_SUCH_NODE"
        raw["manifest_hash"] = canonical_hash(raw)  # 让 hash 一致，只剩边错误

    p = _write_manifest(tmp_path, mutate)
    clear_cache()
    assert load_manifest(path=p) is None
    ok, issues = validate_manifest(json.loads(p.read_text(encoding="utf-8")), p)
    assert not ok
    assert any("起点节点不存在" in i for i in issues)


def test_manifest_bad_stop_line_formula_rejected(tmp_path):
    def mutate(raw: dict) -> None:
        raw["gates"]["out"]["stop_paint_y"] = -33.0  # 与 gate_y - clearance 不符
        raw["manifest_hash"] = canonical_hash(raw)

    p = _write_manifest(tmp_path, mutate)
    clear_cache()
    ok, issues = validate_manifest(json.loads(p.read_text(encoding="utf-8")), p)
    assert not ok
    assert any("停止线公式" in i for i in issues)


# ---------------------------------------------------------------------------
# 2. generator（manifest 驱动）
# ---------------------------------------------------------------------------
def test_generator_uses_manifest_rows_and_pitch():
    gen = LayoutGenerator()  # 自动加载 manifest
    assert gen.using_manifest is True
    assert gen.aisle_w == 6.5
    assert gen.row_pitch == 12.5
    a01 = _space_spec(gen, "A", "A-01")
    a11 = _space_spec(gen, "A", "A-11")
    b01 = _space_spec(gen, "B", "B-01")
    b11 = _space_spec(gen, "B", "B-11")
    assert (a01.x, a01.z, a01.rotation) == (-34.5, 25.75, 180.0)
    assert (a11.x, a11.z, a11.rotation) == (-34.5, 13.25, 0.0)
    assert (b01.z, b01.rotation) == (-7.25, 180.0)
    assert (b11.z, b11.rotation) == (-19.75, 0.0)


def test_generator_still_80_spaces_and_x_unchanged():
    gen = LayoutGenerator()
    specs = gen.generate([
        ZoneDecl(zone="A", count=20), ZoneDecl(zone="B", count=20),
        ZoneDecl(zone="C", count=20), ZoneDecl(zone="D", count=20)])
    assert len(specs) == 80
    # x 起点/列距沿用：A-01 x=-34.5、A-10 x=-7.5；C-01 x=+7.5
    x_map = {s.space_no: s.x for s in specs}
    assert x_map["A-01"] == -34.5 and x_map["A-10"] == -7.5
    assert x_map["C-01"] == 7.5 and x_map["C-10"] == 34.5
    assert all(s.zone in "ABCD" for s in specs)


def test_generator_custom_rows_use_manifest_pitch():
    gen = LayoutGenerator()
    specs = gen.generate([ZoneDecl(zone="A", count=25, rows=3,
                                   anchor=(-36.0, 25.0), columns=10)])
    zs = [s.z for s in specs]
    assert zs[0] == 25.0
    assert zs[10] == 12.5       # 25.0 - 12.5（manifest 排距）
    assert zs[20] == 0.0        # 25.0 - 2*12.5


def test_generator_legacy_fallback_when_manifest_disabled():
    gen = LayoutGenerator(manifest=None)
    assert gen.using_manifest is False
    assert gen.aisle_w == 5.0 and gen.row_pitch == 11.0
    a01 = _space_spec(gen, "A", "A-01")
    a11 = _space_spec(gen, "A", "A-11")
    assert (a01.z, a11.z) == (25.0, 14.0)   # 旧坐标回退


def test_zone_geom_from_manifest_has_four_zones():
    m = load_manifest()
    geoms = zone_geom_from_manifest(m)
    assert set(geoms) == {"A", "B", "C", "D"}
    assert geoms["C"].default_kind == SpaceKind.NE_PREFER
    assert [round(r.z, 2) for r in geoms["B"].rows] == [-7.25, -19.75]


# ---------------------------------------------------------------------------
# 3. service._scene_dict / _gates_dict
# ---------------------------------------------------------------------------
def test_service_scene_and_gates_v2(container):
    layout = container.layout.current_layout()
    scene = layout.scene
    gates = layout.gates
    # 旧键必须保留（前端兼容）
    for key in ("lot_x", "top_y", "bot_y", "main_road_half", "entry_gate_x",
                "exit_gate_x", "gate_y", "outer_road_y",
                "stop_line_entry_y", "stop_line_exit_y"):
        assert key in scene, f"旧字段 {key} 丢失"
    # route-v2 新键
    assert scene["aisle_w"] == 6.5
    assert scene["aisle_n"] == 19.5
    assert scene["aisle_s"] == -13.5
    assert scene["median_half"] == 1.75
    assert scene["gate_out_y"] == -34.0
    assert scene["stop_line_entry_y"] == -25.8
    assert scene["stop_line_exit_y"] == -33.7
    assert scene["stop_entry_center_y"] == -28.4
    assert scene["stop_exit_center_y"] == -31.1
    assert scene["stop_paint"] == {"in": -25.8, "out": -33.7}
    assert scene["plaza"]["n"]["y"] == 19.5 and scene["plaza"]["s"]["y"] == -13.5
    assert scene["road_graph_version"] == 2
    assert scene["manifest_hash"].startswith("sha256:")
    assert scene["layout_source"] == "manifest"
    # 与 web/sim3d/js/config.js SCENE_DEFAULT 对齐的字段（前端直接消费）
    assert scene["lot_x_min"] == -36.0 and scene["lot_x_max"] == 36.0
    assert scene["lot_y_min"] == -30.0 and scene["lot_y_max"] == 29.5
    assert scene["apron_y_min"] == -45.5 and scene["apron_y_max"] == -30.0
    assert scene["plaza_n"] == {"x": 0.0, "y": 19.5, "w": 12.0, "h": 6.5}
    assert scene["plaza_s"] == {"x": 0.0, "y": -13.5, "w": 12.0, "h": 6.5}
    # 闸口：出口闸南移 -34.0，停止线按公式
    assert gates["exit"]["y"] == -34.0
    assert gates["exit"]["stop_line_y"] == -33.7
    assert gates["entry"]["y"] == -25.5
    assert gates["entry"]["stop_line_y"] == -25.8
    assert gates["entry"]["stop_center_y"] == -28.4
    assert gates["entry"]["stop_center"] == -28.4   # 前端可能用 stop_center 别名
    assert gates["exit"]["stop_center_y"] == -31.1
    assert gates["exit"]["stop_center"] == -31.1
    assert gates["booth"] == {"x": 0.0, "y": -27.5}
    assert gates["outer_road_y"] == -48.5


def test_service_layout_meta(container):
    meta = container.layout.manifest_meta()
    assert meta["road_graph_version"] == 2
    assert meta["manifest_hash"].startswith("sha256:")
    assert meta["layout_source"] == "manifest"
    summary = container.layout.manifest_summary()
    assert summary["nodes"] == 115 and summary["edges"] == 113
    assert summary["conflict_zones"] == ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7"]


def test_service_scene_legacy_fallback(cfg, monkeypatch, tmp_path):
    """manifest 不可用时回退旧常量（服务层不崩溃）。"""
    from server.gateway.context import Container as C
    from server.repository.db import init_db
    from server.repository.space_repo import SpaceRepo
    from server.core.events import EventBus

    monkeypatch.setenv(ENV_MANIFEST, str(tmp_path / "missing.json"))
    clear_cache()
    db = init_db(cfg.paths.db_file)
    try:
        cfg2 = copy.deepcopy(cfg)
        cfg2.scene = dict(cfg2.scene)
        # 把场景回退值显式写成旧值，断言 legacy 分支读取 config.scene
        cfg2.scene.update({"gate_y": -25.5, "stop_line_entry_y": -27.5,
                           "stop_line_exit_y": -23.5, "outer_road_y": -46.0,
                           "road_graph_version": 1})
        svc = LayoutService(SpaceRepo(db), EventBus(), cfg2)
        assert svc.layout_source == "legacy"
        scene = svc._scene_dict()
        gates = svc._gates_dict()
        assert scene["layout_source"] == "legacy"
        assert gates["entry"]["y"] == gates["exit"]["y"] == -25.5
        assert gates["exit"]["stop_line_y"] == -23.5
        assert gates["entry"]["stop_center_y"] == -28.4
        assert gates["exit"]["stop_center"] == -22.6
        assert scene["road_graph_version"] == 1
        # 回退时也提供前端 SCENE_DEFAULT 同名键（避免前端读 undefined）
        assert scene["lot_x_min"] == -36.0 and scene["lot_x_max"] == 36.0
        assert scene["lot_y_min"] == -30.0 and scene["lot_y_max"] == 29.5
        assert scene["apron_y_min"] == -45.5 and scene["apron_y_max"] == -30.0
        assert scene["plaza_n"]["y"] == 19.5 and scene["plaza_s"]["y"] == -13.5
    finally:
        db.close()
        clear_cache()


def test_scene_config_mismatch_detected(cfg):
    m = load_manifest()
    assert scene_config_issues(cfg, m) == []   # 仓库 config.yaml 已同步 v2
    bad = copy.deepcopy(cfg)
    bad.scene = dict(bad.scene)
    bad.scene["gate_y"] = -25.0
    bad.scene["aisle_w"] = 5.0
    issues = scene_config_issues(bad, m)
    assert any("gate_y" in i for i in issues)
    assert any("aisle_w" in i for i in issues)


# ---------------------------------------------------------------------------
# 4. API：/api/layout 顶层字段 + /assets3d 挂载
# ---------------------------------------------------------------------------
def test_api_layout_has_road_graph_fields(client, admin_token):
    r = client.get("/api/layout")
    assert r.status_code == 200
    data = r.json()
    m = load_manifest()
    assert data["road_graph_version"] == m.road_graph_version == 2
    assert data["manifest_hash"] == m.manifest_hash
    assert data["manifest_version"] == m.manifest_version
    assert data["layout_source"] == "manifest"
    assert data["assets3d_url"] == "/assets3d"
    assert data["road_graph"]["nodes"] == 115
    assert data["road_graph"]["edges"] == 113
    # 兼容旧响应结构
    assert set(data) >= {"version", "spaces", "zones", "free", "free_total",
                         "scene", "gates"}
    assert len(data["spaces"]) == 80
    a01 = next(s for s in data["spaces"] if s["space_no"] == "A-01")
    assert (a01["x"], a01["z"], a01["rotation"]) == (-34.5, 25.75, 180.0)
    assert data["gates"]["exit"]["y"] == -34.0
    assert data["scene"]["aisle_w"] == 6.5
    # 前端 SCENE_DEFAULT 同名键 / gates 停止点别名（联调契约）
    scene = data["scene"]
    assert scene["lot_x_min"] == -36.0 and scene["lot_y_max"] == 29.5
    assert scene["apron_y_min"] == -45.5
    assert scene["plaza_n"]["y"] == 19.5 and scene["plaza_s"]["y"] == -13.5
    assert data["gates"]["entry"]["stop_center"] == -28.4
    assert data["gates"]["exit"]["stop_center"] == -31.1


def test_assets3d_static_mount(client):
    root = resolve_assets_root()
    assert root is not None and root.name == "assets_3d"
    r = client.get("/assets3d/layout/lot_v2.json")
    assert r.status_code == 200
    payload = r.json()
    raw = _manifest_raw()
    assert payload["manifest_hash"] == raw["manifest_hash"]
    assert payload["manifest_hash"] == canonical_hash(payload)
    assert client.get("/assets3d/no_such_file.json").status_code == 404


def test_api_layout_spaces_new_coords(client, admin_token):
    data = client.get("/api/spaces").json()
    assert data["total"] == 80
    z = {it["space_no"]: it["z"] for it in data["items"]}
    assert z["A-01"] == 25.75 and z["A-11"] == 13.25
    assert z["B-01"] == -7.25 and z["B-11"] == -19.75


# ---------------------------------------------------------------------------
# 5. 迁移脚本
# ---------------------------------------------------------------------------
def _fresh_container(cfg) -> Container:
    c = Container(cfg)
    c.bootstrap()
    return c


def test_migrate_dry_run_does_not_write(cfg):
    c = _fresh_container(cfg)
    try:
        _simulate_legacy_coords(c)
    finally:
        c.close()
    before = _db_coords(cfg.paths.db_file)
    assert before["A-01"] == 25.0
    report = run_migration(cfg, dry_run=True, backup=False)
    assert report["status"] == "planned" and report["exit_code"] == 0
    assert report["changed_count"] == 80
    after = _db_coords(cfg.paths.db_file)
    assert after == before, "dry-run 不应修改数据库"
    assert report["manifest"]["manifest_hash"].startswith("sha256:")


def test_migrate_real_then_idempotent(cfg):
    c = _fresh_container(cfg)
    old_version = None
    try:
        _simulate_legacy_coords(c)
        old_version = c.layout.current_version()
    finally:
        c.close()
    report = run_migration(cfg, backup=True)
    assert report["status"] == "migrated", report.get("messages")
    assert report["exit_code"] == 0
    assert report["changed_count"] == 80
    assert report["old_version"] == old_version
    assert report["new_version"] == old_version + 1
    assert report["history_recorded"] is True
    assert report["verification"]["consistent"] is True
    assert report["verification"]["mismatch_count"] == 0
    assert report["backup_path"] and Path(report["backup_path"]).is_file()
    coords = _db_coords(cfg.paths.db_file)
    assert coords["A-01"] == 25.75 and coords["B-11"] == -19.75
    # 二次运行：幂等（版本不涨、不再写历史）
    report2 = run_migration(cfg, backup=True)
    assert report2["status"] == "already_current"
    assert report2["new_version"] == report2["old_version"] == old_version + 1
    assert report2.get("backup_path") is None
    assert _db_coords(cfg.paths.db_file)["A-01"] == 25.75


def test_migrate_preserves_occupied_space(cfg):
    c = _fresh_container(cfg)
    try:
        _simulate_legacy_coords(c)
        with c.db.scope():
            c.parking_repo.create(plate="琼A12345", plate_color="blue",
                                  entry_time=datetime(2026, 10, 18),
                                  space_no="A-01", fee_rule_id=1, status="in")
            c.space_repo.occupy("A-01", "琼A12345")
    finally:
        c.close()
    report = run_migration(cfg, backup=False)
    assert report["status"] == "migrated", report.get("messages")
    assert report["verification"]["occupied_preserved"] is True
    assert report["verification"]["occupied_before_count"] == 1
    con = sqlite3.connect(f"file:{cfg.paths.db_file.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select status, current_plate from parking_space where space_no='A-01'").fetchone()
        rec = con.execute(
            "select status, space_no from parking_record where plate='琼A12345'").fetchone()
    finally:
        con.close()
    assert row == ("occupied", "琼A12345")
    assert rec == ("in", "A-01")


def test_migrate_empty_lot_refuses_with_occupancy(cfg):
    c = _fresh_container(cfg)
    try:
        _simulate_legacy_coords(c)
        with c.db.scope():
            c.parking_repo.create(plate="琼B12345", plate_color="blue",
                                  entry_time=datetime(2026, 10, 18),
                                  space_no="A-02", fee_rule_id=1, status="in")
            c.space_repo.occupy("A-02", "琼B12345")
    finally:
        c.close()
    report = run_migration(cfg, empty_lot=True, backup=False)
    assert report["status"] == "refused_occupied"
    assert report["exit_code"] == 2
    assert _db_coords(cfg.paths.db_file)["A-02"] == 25.0, "拒绝时不应改库"


def test_migrate_writes_report_json(cfg, tmp_path):
    c = _fresh_container(cfg)
    try:
        _simulate_legacy_coords(c)
    finally:
        c.close()
    out = tmp_path / "migrate_report.json"
    report = run_migration(cfg, dry_run=True, backup=False, report_path=out)
    assert out.is_file()
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["status"] == "planned"
    assert saved["changed_count"] == 80
    assert report["report_path"] == str(out)


def test_migrate_cli_dry_run(cfg, tmp_path):
    """CLI 外壳（tools/migrate_layout_v2.py）可执行且 --dry-run 不写库。"""
    import yaml
    from tools.migrate_layout_v2 import main as cli_main

    c = _fresh_container(cfg)
    try:
        _simulate_legacy_coords(c)
    finally:
        c.close()
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["paths"] = {
        "data_dir": str(cfg.paths.data_dir),
        "db_file": str(cfg.paths.db_file),
        "photos_dir": str(cfg.paths.photos_dir),
        "reports_dir": str(cfg.paths.reports_dir),
        "plates_dataset": str(cfg.paths.plates_dataset),
        "web_admin": str(cfg.paths.web_admin),
        "web_sim": str(cfg.paths.web_sim),
    }
    cfg_path = tmp_path / "config_migrate.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    report_path = tmp_path / "cli_report.json"
    code = cli_main(["--config", str(cfg_path), "--dry-run",
                     "--report", str(report_path)])
    assert code == 0
    assert report_path.is_file()
    assert _db_coords(cfg.paths.db_file)["A-01"] == 25.0


# ---------------------------------------------------------------------------
# 6. allocator 可选 chooser（route-v2 可选增强，默认行为不变）
# ---------------------------------------------------------------------------
def test_allocator_default_behavior_unchanged(container):
    with container.db.scope():
        sp = container.allocator.allocate("琼A12345", "blue")
    assert sp is not None and sp.space_no == "A-01"
    assert container.allocator.chooser is None


def test_allocator_chooser_optional(container):
    def choose_last(candidates, plate, color):
        return candidates[-1] if candidates else None

    alloc = SpaceAllocator(container.space_repo, None, chooser=choose_last)
    with container.db.scope():
        sp = alloc.allocate("琼A54321", "blue")
    assert sp is not None
    assert sp.space_no == "D-20"   # 候选末尾 = 优先级最低的 D 区末位
    assert sp.zone == "D"


def test_allocator_chooser_invalid_return_falls_back(container):
    def choose_none(candidates, plate, color):
        return None

    alloc = SpaceAllocator(container.space_repo, None, chooser=choose_none)
    with container.db.scope():
        sp = alloc.allocate("琼A99999", "blue")
    assert sp is not None and sp.space_no == "A-01"   # 回退默认“每区第一个空位”


