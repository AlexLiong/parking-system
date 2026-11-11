"""B/C 后端：上传/相机/视频抽帧保留期清理测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from server.retention import cleanup_photos


def _file(path: Path, *, age_days: float = 0.0, size: int = 1024,
          content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if content is not None else b"x" * size)
    mtime = time.time() - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


def test_age_cleanup_keeps_recent_files(cfg):
    cfg.retention.max_age_days = 7
    cfg.retention.keep_recent_files = 1
    cfg.retention.max_total_mb = 2048
    root = Path(cfg.paths.photos_dir) / "uploads"
    old1 = _file(root / "old1.png", age_days=10)
    old2 = _file(root / "old2.png", age_days=9)
    new = _file(root / "new.png", age_days=1)
    report = cleanup_photos(cfg, dry_run=False)
    assert not old1.exists() and not old2.exists()
    assert new.exists()
    assert report.deleted_age == 2
    assert report.bytes_freed > 0
    assert report.kept_files == 1


def test_keep_recent_protects_small_directory(cfg):
    cfg.retention.max_age_days = 7
    cfg.retention.keep_recent_files = 1
    old = _file(Path(cfg.paths.photos_dir) / "crops" / "only.png", age_days=30)
    report = cleanup_photos(cfg, dry_run=False)
    assert old.exists()
    assert report.deleted_age == 0
    # 显式不保留时才会被年龄策略清理
    cfg.retention.keep_recent_files = 0
    report = cleanup_photos(cfg, dry_run=False)
    assert not old.exists()
    assert report.deleted_age == 1


def test_capacity_cleanup_oldest_first(cfg):
    cfg.retention.max_age_days = 365
    cfg.retention.keep_recent_files = 0
    cfg.retention.min_age_hours_for_capacity = 0
    cfg.retention.max_total_mb = 1
    root = Path(cfg.paths.photos_dir) / "camera"
    a = _file(root / "a.png", age_days=3, size=600 * 1024)
    b = _file(root / "b.png", age_days=2, size=600 * 1024)
    c = _file(root / "c.png", age_days=1, size=600 * 1024)
    report = cleanup_photos(cfg, dry_run=False)
    assert not a.exists() and not b.exists()
    assert c.exists()
    assert report.deleted_capacity >= 2
    assert report.kept_bytes <= 1024 * 1024


def test_camera_video_frame_dirs_cleaned_and_empty_dirs_pruned(cfg):
    cfg.retention.max_age_days = 7
    cfg.retention.keep_recent_files = 0
    video_dir = Path(cfg.paths.photos_dir) / "camera" / "video_batch"
    old_frame = _file(video_dir / "frame_00.png", age_days=10)
    report = cleanup_photos(cfg, dry_run=False)
    assert not old_frame.exists()
    assert not video_dir.exists()
    assert report.deleted_age == 1


def test_unknown_files_and_symlinks_untouched(cfg):
    cfg.retention.max_age_days = 7
    cfg.retention.keep_recent_files = 0
    root = Path(cfg.paths.photos_dir) / "uploads"
    note = _file(root / "note.txt", age_days=30)
    report = cleanup_photos(cfg, dry_run=False)
    assert note.exists()
    assert report.ignored_files >= 1


def test_datasets_are_not_touched(cfg, tmp_path):
    cfg.retention.max_age_days = 7
    cfg.retention.keep_recent_files = 0
    dataset = tmp_path / "datasets" / "demo-lpr"
    cfg.paths.plates_dataset = dataset
    keep = _file(dataset / "demo.png", age_days=30)
    old_photo = _file(Path(cfg.paths.photos_dir) / "uploads" / "old.png", age_days=30)
    report = cleanup_photos(cfg, dry_run=False)
    assert keep.exists() and not old_photo.exists()
    assert report.deleted_age == 1


def test_disabled_and_dry_run(cfg):
    old = _file(Path(cfg.paths.photos_dir) / "uploads" / "old.png", age_days=30)
    cfg.retention.enabled = False
    report = cleanup_photos(cfg, dry_run=False)
    assert old.exists()
    assert report.skipped_reason == "retention.enabled=false"
    cfg.retention.enabled = True
    cfg.retention.max_age_days = 7
    cfg.retention.keep_recent_files = 0
    report = cleanup_photos(cfg, dry_run=True)
    assert old.exists()
    assert report.deleted_age == 1 and report.bytes_freed > 0


def test_startup_cleanup_runs(cfg):
    """FastAPI lifespan 启动时执行一次保守清理。"""
    from fastapi.testclient import TestClient
    from server.main import create_app
    cfg.retention.max_age_days = 7
    cfg.retention.keep_recent_files = 0
    old = _file(Path(cfg.paths.photos_dir) / "uploads" / "old_startup.png",
                age_days=30)
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/health").status_code == 200
    assert not old.exists()


def test_cli_dry_run(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "cleanup_photos.py"),
         "--dry-run", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env=env, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["dry_run"] is True
    assert "scanned_files" in data and "deleted_age" in data
