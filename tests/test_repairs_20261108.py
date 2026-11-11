"""修复 D（20261108）：M2 重复出口幂等 / M3 照片清理去引用 / M5 preflight /
M7 PDF 并发安全 / M8 demo 暴露防护。"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from server.core.domain import PlateColor, RecognitionResult
from server.core.events import IncidentRaised, VehicleArrived
from server.core.timeutil import utcnow
from server.retention import cleanup_photos
from server.repository.models import ParkingRecord, PaymentRecord
from sqlalchemy import select


def _recognizer(c, plate):
    c.access.recognizer = Mock()
    c.access.recognizer.recognize.return_value = RecognitionResult(
        plate=plate, color=PlateColor.BLUE, overall_conf=0.99,
        success=True, need_manual=False)


def _mk_file(path: Path, *, age_days: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    mtime = time.time() - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# M2：重复/迟到出口幂等
# ---------------------------------------------------------------------------
def test_duplicate_exit_is_idempotent_without_pending_or_incident(container):
    c = container
    c.access.authorize_entry("veh-m2", "京A11111")
    _recognizer(c, "京A11111")
    incidents = []
    c.bus.subscribe(IncidentRaised, incidents.append)
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-m2", gate="out", image_path="frames/out1.png"))
    # 第一次出口已自动结算 out
    with c.db.scope():
        recs = c.db.session.execute(select(ParkingRecord).where(
            ParkingRecord.plate == "京A11111")).scalars().all()
    assert len(recs) == 1 and recs[0].status == "out"
    payments_count = c.db.session.execute(
        select(PaymentRecord)).scalars().all().__len__()
    pending_before = c.access.list_pending("pending")
    incidents_before = len(incidents)
    # 重复出口（同 vehicle_id/plate）→ 幂等：不生成 pending、不新增 incident
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-m2", gate="out", image_path="frames/out2.png"))
    assert c.access.list_pending("pending") == pending_before
    assert len(incidents) == incidents_before
    assert ("out", "veh-m2") not in c.access._arrivals
    assert c.db.session.execute(
        select(PaymentRecord)).scalars().all().__len__() == payments_count


def test_new_entry_same_plate_then_exit_still_settles(container):
    """同牌新一次入场（有 open 记录）不能被误判为重复出口。"""
    c = container
    c.access.authorize_entry("veh-m2a", "京B22222")
    _recognizer(c, "京B22222")
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-m2a", gate="out", image_path="frames/a.png"))
    # 同牌新一次入场
    c.access.authorize_entry("veh-m2b", "京B22222")
    assert c.parking_repo.find_open_by_plate("京B22222") is not None
    c.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="veh-m2b", gate="out", image_path="frames/b.png"))
    with c.db.scope():
        from sqlalchemy import select
        recs = c.db.session.execute(select(ParkingRecord).where(
            ParkingRecord.plate == "京B22222")).scalars().all()
    assert len(recs) == 2
    assert all(r.status == "out" for r in recs)
    assert c.access.list_pending("pending") == []


# ---------------------------------------------------------------------------
# M3：照片清理后引用置 NULL
# ---------------------------------------------------------------------------
def test_retention_clears_db_photo_refs_and_detail_is_empty(client, admin_token):
    c = client.app.state.container
    photos = Path(c.cfg.paths.photos_dir)
    old_entry = _mk_file(photos / "crops" / "old_entry.png", age_days=10)
    old_exit = _mk_file(photos / "uploads" / "old_exit.png", age_days=10)
    kept_photo = _mk_file(photos / "crops" / "kept.png", age_days=0.01)
    with c.db.scope():
        old_rec = c.parking_repo.create(
            plate="京C33333", plate_color="blue", entry_time=utcnow() - timedelta(hours=2),
            space_no=None, status="in")
        old_rec.entry_photo = str(old_entry)
        old_rec.exit_photo = str(old_exit)
        old_rec.entry_plate_crop = str(old_entry)
        old_rec.exit_plate_crop = str(old_exit)
        old_id = old_rec.id
        kept_rec = c.parking_repo.create(
            plate="京C44444", plate_color="blue", entry_time=utcnow() - timedelta(hours=1),
            space_no=None, status="in")
        kept_rec.entry_photo = str(kept_photo)
        kept_id = kept_rec.id
    c.cfg.retention.max_age_days = 1
    c.cfg.retention.keep_recent_files = 0
    c.cfg.retention.max_total_mb = 2048
    c.cfg.retention.min_age_hours_for_capacity = 0
    report = cleanup_photos(c.cfg, now=datetime.now(), db=c.db)
    assert not old_entry.exists() and not old_exit.exists()
    assert report.record_refs_cleared >= 2
    rec = c.parking_repo.get(old_id)
    assert rec.entry_photo is None and rec.exit_photo is None
    assert rec.entry_plate_crop is None and rec.exit_plate_crop is None
    kept = c.parking_repo.get(kept_id)
    assert kept.entry_photo == str(kept_photo)
    # 管理端详情不 404，字段为空
    detail = client.get(f"/api/records/{old_id}", headers=admin_token)
    assert detail.status_code == 200
    body = detail.json()
    assert body["entry_photo"] is None and body["exit_photo"] is None
    # 幂等：再次清理没有可置空引用
    report2 = cleanup_photos(c.cfg, now=datetime.now(), db=c.db)
    assert report2.record_refs_cleared == 0
    assert report2.errors == []


def test_retention_same_photo_path_multiple_records_cleared(client):
    c = client.app.state.container
    shared = _mk_file(Path(c.cfg.paths.photos_dir) / "camera" / "shared.png",
                      age_days=10)
    ids = []
    with c.db.scope():
        for index in range(2):
            rec = c.parking_repo.create(
                plate=f"京D{index}4444", plate_color="blue",
                entry_time=utcnow() - timedelta(hours=1), space_no=None,
                status="in")
            rec.entry_photo = str(shared)
            ids.append(rec.id)
    c.cfg.retention.max_age_days = 1
    c.cfg.retention.keep_recent_files = 0
    c.cfg.retention.max_total_mb = 2048
    report = cleanup_photos(c.cfg, now=datetime.now(), db=c.db)
    assert report.record_refs_cleared >= 2
    assert all(c.parking_repo.get(i).entry_photo is None for i in ids)


# ---------------------------------------------------------------------------
# M5：preflight 外部模板检查
# ---------------------------------------------------------------------------
def test_preflight_external_generator_missing_and_present(tmp_path):
    from tools.preflight import check_external_generator

    with pytest.raises(RuntimeError, match="外部中文车牌生成器"):
        check_external_generator(tmp_path)          # 缺目录/文件 → 明确失败
    base = tmp_path / "datasets/external/chinese_license_plate_generator"
    base.mkdir(parents=True)
    (base / "plate_number.py").write_text("VALUE = 1\n", encoding="utf-8")
    (base / "LICENSE").write_text("MIT\n", encoding="utf-8")
    info = check_external_generator(tmp_path)
    assert info["external_generator"].endswith("chinese_license_plate_generator")
    # 真实仓库目录必须可导入
    from tools.preflight import ROOT
    real = check_external_generator(ROOT)
    assert real["external_generator"].endswith("chinese_license_plate_generator")


# ---------------------------------------------------------------------------
# M7：PDF 并发导出唯一文件且不互相覆盖
# ---------------------------------------------------------------------------
def test_concurrent_pdf_export_uses_unique_files(client, admin_token):
    c = client.app.state.container
    reports_dir = Path(c.cfg.paths.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in reports_dir.glob("*.pdf")}
    from server.gateway.api_admin import export_report

    def call(_index):
        return export_report(period="day", anchor="2026-11-08", c=c)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(call, range(4)))
    paths = []
    for response in responses:
        assert response.media_type == "application/pdf"
        path = Path(response.path)
        paths.append(path)
        assert path.exists() and path.stat().st_size > 0
        assert path.read_bytes().startswith(b"%PDF")
        # 响应后清理（模拟 FileResponse 完成）
        response.background.func(*response.background.args)
    assert len(set(paths)) == 4                     # 并发不互相覆盖
    gone = {p.name for p in reports_dir.glob("*.pdf")} - before
    assert gone == set()                            # 临时文件已清理


# ---------------------------------------------------------------------------
# M8：demo 暴露防护
# ---------------------------------------------------------------------------
def _cfg_with(monkeypatch, **server_overrides):
    import copy

    from server.core.config import load_config
    cfg = copy.deepcopy(load_config(force=True))
    for key, value in server_overrides.items():
        setattr(cfg.server, key, value)
    monkeypatch.delenv("PARKING_ALLOW_EXPOSED_DEMO", raising=False)
    return cfg


def test_demo_exposure_rejected_for_non_loopback(monkeypatch):
    from server.main import create_app, validate_demo_exposure
    cfg = _cfg_with(monkeypatch, demo_mode=True, host="0.0.0.0")
    with pytest.raises(RuntimeError, match="demo_mode|loopback"):
        validate_demo_exposure(cfg)
    with pytest.raises(RuntimeError, match="demo_mode|loopback"):
        create_app(cfg)


def test_demo_exposure_allowed_for_loopback(monkeypatch):
    from server.main import validate_demo_exposure
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        validate_demo_exposure(_cfg_with(monkeypatch, demo_mode=True, host=host))
    # 非 demo 模式监听 0.0.0.0 不受影响
    validate_demo_exposure(_cfg_with(monkeypatch, demo_mode=False, host="0.0.0.0"))
    # 显式环境变量允许暴露（先取 cfg 再设环境变量，避免 helper 清理 env）
    cfg = _cfg_with(monkeypatch, demo_mode=True, host="0.0.0.0")
    monkeypatch.setenv("PARKING_ALLOW_EXPOSED_DEMO", "1")
    validate_demo_exposure(cfg)


def test_demo_session_only_in_demo_mode(client):
    c = client.app.state.container
    c.cfg.server.demo_mode = True
    ok = client.post("/api/auth/demo-session")
    assert ok.status_code == 200 and ok.json()["token"]
    c.cfg.server.demo_mode = False
    denied = client.post("/api/auth/demo-session")
    assert denied.status_code == 403
