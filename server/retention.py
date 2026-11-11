"""上传/相机/视频抽帧文件保留期清理（B/C 后端项）。

只扫描 ``photos_dir/{uploads,crops,camera}`` 三个子目录（camera 下含 video 抽帧子目录），
不会触碰 ``datasets`` 演示源图、``data/logs`` 或 ``data/reports`` 证据。

策略（保守、可配置，见 ``config.yaml`` 的 ``retention``）：
1. 每个子目录按 mtime 保留最新 ``keep_recent_files`` 个文件（最近识别证据）；
2. 早于 ``max_age_days`` 的非保留文件按最旧优先删除；
3. 三个目录清理后的总容量超过 ``max_total_mb`` 时，继续按最旧优先删除，
   但只删早于 ``min_age_hours_for_capacity`` 且非保留文件；
4. 只处理常见图片/视频后缀，未知文件不碰；符号链接不跟随、不删除；
5. ``dry_run=True`` 只统计不落盘。

服务启动/后台由 ``server.main.lifespan`` 调用；CLI 为 ``tools/cleanup_photos.py``。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .core.config import AppConfig, RetentionConfig

log = logging.getLogger("retention")

PHOTO_SUBDIRS = ("uploads", "crops", "camera")
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
                  ".mp4", ".avi", ".mov", ".mkv", ".m4v", ".mpg", ".mpeg", ".webm"}
MIB = 1024 * 1024


@dataclass(frozen=True)
class _FileInfo:
    path: Path
    mtime: float
    size: int


@dataclass
class RetentionReport:
    enabled: bool = True
    dry_run: bool = False
    scanned_files: int = 0
    scanned_bytes: int = 0
    ignored_files: int = 0
    deleted_age: int = 0
    deleted_capacity: int = 0
    bytes_freed: int = 0
    kept_files: int = 0
    kept_bytes: int = 0
    # M3：清理文件后，把 parking_record 中对应照片/裁剪引用置 NULL 的行数
    record_refs_cleared: int = 0
    errors: list[str] = field(default_factory=list)
    deleted: list[dict] = field(default_factory=list)
    roots: dict[str, dict[str, int]] = field(default_factory=dict)
    skipped_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "scanned_files": self.scanned_files,
            "scanned_bytes": self.scanned_bytes,
            "ignored_files": self.ignored_files,
            "deleted_age": self.deleted_age,
            "deleted_capacity": self.deleted_capacity,
            "bytes_freed": self.bytes_freed,
            "kept_files": self.kept_files,
            "kept_bytes": self.kept_bytes,
            "record_refs_cleared": self.record_refs_cleared,
            "errors": list(self.errors),
            "deleted": list(self.deleted),
            "roots": dict(self.roots),
            "skipped_reason": self.skipped_reason,
        }


def _collect_files(root: Path) -> tuple[list[_FileInfo], int]:
    """递归收集常规图片/视频文件；返回 (files, ignored_count)。"""
    files: list[_FileInfo] = []
    ignored = 0
    if not root.exists() or not root.is_dir():
        return files, ignored
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not Path(dirpath, d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    ignored += 1
                    continue
                if path.suffix.lower() not in MEDIA_SUFFIXES:
                    ignored += 1
                    continue
                stat = path.stat(follow_symlinks=False)
            except OSError:
                ignored += 1
                continue
            files.append(_FileInfo(path=path, mtime=float(stat.st_mtime),
                                   size=int(stat.st_size)))
    return files, ignored


def _prune_empty_dirs(root: Path) -> None:
    """删除 root 下变空的嵌套目录（保留 root 本身，比如 camera/video_xxx）。"""
    if not root.exists():
        return
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False,
                                                  followlinks=False):
        current = Path(dirpath)
        if current == root or current.is_symlink():
            continue
        try:
            if not any(current.iterdir()):
                current.rmdir()
        except OSError:
            pass


def _delete_file(info: _FileInfo, report: RetentionReport, reason: str,
                 dry_run: bool) -> bool:
    try:
        if not dry_run:
            info.path.unlink(missing_ok=False)
        report.bytes_freed += info.size
        report.deleted.append({
            "path": str(info.path),
            "size": info.size,
            "age_sec": round(time.time() - info.mtime, 1),
            "reason": reason,
        })
        return True
    except OSError as exc:
        report.errors.append(f"{info.path}: {exc}")
        return False


_PHOTO_COLUMNS = ("entry_photo", "exit_photo", "entry_plate_crop",
                  "exit_plate_crop")


def _photo_path_candidates(value: str, cfg: AppConfig) -> set[str]:
    """同一文件可能被存为绝对路径/相对路径/URL，生成全部候选字符串（M3）。"""
    p = Path(value)
    candidates: set[str] = {str(p), p.as_posix()}
    photos_dir = Path(cfg.paths.photos_dir)
    data_dir = Path(cfg.paths.data_dir)
    for base, prefix in ((data_dir, ""), (photos_dir, ""),
                         (data_dir.parent, ""), (Path.cwd(), "")):
        try:
            rel = p.resolve().relative_to(Path(base).resolve())
        except Exception:  # noqa: BLE001
            continue
        candidates.add(str(rel))
        candidates.add(rel.as_posix())
        candidates.add(f"/media/{rel.as_posix()}")
    return candidates


def _clear_record_photo_refs(db, cfg: AppConfig,
                             deleted_paths: list[str]) -> int:
    """把已删除文件对应的 parking_record 照片/裁剪字段置 NULL（M3）。"""
    from sqlalchemy import update

    from .repository.models import ParkingRecord

    candidates: set[str] = set()
    for value in deleted_paths:
        candidates.update(_photo_path_candidates(value, cfg))
    if not candidates:
        return 0
    values = sorted(candidates)
    cleared = 0
    with db.scope():
        for start in range(0, len(values), 400):
            chunk = values[start:start + 400]
            for column in _PHOTO_COLUMNS:
                result = db.session.execute(
                    update(ParkingRecord)
                    .where(getattr(ParkingRecord, column).in_(chunk))
                    .values({column: None}))
                cleared += int(result.rowcount or 0)
    return cleared


def cleanup_photos(cfg: AppConfig, *, now: Optional[datetime] = None,
                   dry_run: bool = False, db=None) -> RetentionReport:
    """按 retention 配置清理 photos 下的上传/裁剪/相机目录。"""
    rcfg: RetentionConfig = getattr(cfg, "retention", None) or RetentionConfig()
    report = RetentionReport(enabled=bool(rcfg.enabled), dry_run=bool(dry_run))
    if not rcfg.enabled:
        report.skipped_reason = "retention.enabled=false"
        log.info("照片保留期清理已关闭")
        return report

    now_ts = (now.timestamp() if now is not None else time.time())
    age_seconds = max(0.0, float(rcfg.max_age_days)) * 86400.0
    age_cutoff = now_ts - age_seconds
    keep_recent = max(0, int(rcfg.keep_recent_files))
    min_age_seconds = max(0, int(rcfg.min_age_hours_for_capacity)) * 3600.0
    capacity_cutoff = now_ts - min_age_seconds
    max_bytes = max(0, int(rcfg.max_total_mb)) * MIB

    photos_dir = Path(cfg.paths.photos_dir)
    files_by_root: dict[Path, list[_FileInfo]] = {}
    protected: set[Path] = set()
    for name in PHOTO_SUBDIRS:
        root = photos_dir / name
        files, ignored = _collect_files(root)
        report.ignored_files += ignored
        report.scanned_files += len(files)
        report.scanned_bytes += sum(f.size for f in files)
        report.roots[name] = {"scanned": len(files), "deleted": 0, "kept": len(files)}
        files_by_root[root] = files
        if keep_recent:
            newest = sorted(files, key=lambda f: (f.mtime, str(f.path)),
                            reverse=True)[:keep_recent]
            protected.update(f.path for f in newest)

    # 1) 保留期清理：最旧优先，跳过每个目录的最新 N 个文件
    for root, files in files_by_root.items():
        for info in sorted(files, key=lambda f: (f.mtime, str(f.path))):
            if info.path in protected:
                continue
            if age_seconds > 0 and info.mtime < age_cutoff:
                if _delete_file(info, report, "age", dry_run):
                    report.deleted_age += 1
                    report.roots[root.name]["deleted"] += 1
                    report.roots[root.name]["kept"] -= 1

    # 2) 容量清理：总容量超限时按最旧优先，但保留 recent 与 min_age 内文件
    remaining = [f for files in files_by_root.values() for f in files
                 if f.path.exists() or dry_run]
    # dry_run 时上面按存在性过滤会漏掉计划删除的文件；按是否已记录删除重算
    deleted_paths = {item["path"] for item in report.deleted}
    remaining = [f for files in files_by_root.values() for f in files
                 if str(f.path) not in deleted_paths]
    total = sum(f.size for f in remaining)
    if total > max_bytes:
        eligible = [f for f in remaining
                    if f.path not in protected and f.mtime <= capacity_cutoff]
        for info in sorted(eligible, key=lambda f: (f.mtime, str(f.path))):
            if total <= max_bytes:
                break
            if _delete_file(info, report, "capacity", dry_run):
                report.deleted_capacity += 1
                # 更新 roots 计数
                for root, files in files_by_root.items():
                    if any(f.path == info.path for f in files):
                        report.roots[root.name]["deleted"] += 1
                        report.roots[root.name]["kept"] -= 1
                        break
                total -= info.size
        if total > max_bytes:
            report.errors.append(
                f"容量仍超过上限：{total / MIB:.1f} MiB > {max_bytes / MIB:.1f} MiB"
                "（剩余均为最近保留文件或 min_age 内文件）")

    # 3) 统计清理后保留量；非 dry-run 清空空目录
    deleted_paths = {item["path"] for item in report.deleted}
    kept = [f for files in files_by_root.values() for f in files
            if str(f.path) not in deleted_paths]
    report.kept_files = len(kept)
    report.kept_bytes = sum(f.size for f in kept)
    if not dry_run:
        for root in files_by_root:
            _prune_empty_dirs(root)
        if db is not None and report.deleted:
            try:
                report.record_refs_cleared = _clear_record_photo_refs(
                    db, cfg, [item["path"] for item in report.deleted])
            except Exception as exc:  # noqa: BLE001 - 引用清理失败不影响文件删除
                report.errors.append(f"清理照片引用失败: {exc}")
                log.exception("清理照片引用失败")

    log.info(
        "照片保留期清理%s: scanned=%s deleted_age=%s deleted_capacity=%s "
        "freed=%.2fMiB kept=%s bytes=%s errors=%s",
        "(dry-run)" if dry_run else "", report.scanned_files,
        report.deleted_age, report.deleted_capacity,
        report.bytes_freed / MIB, report.kept_files, report.kept_bytes,
        len(report.errors))
    return report
