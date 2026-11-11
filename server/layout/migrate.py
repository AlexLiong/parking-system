"""旧库布局迁移（layout/migrate.py）——route-v2 车位坐标一次性迁移。

背景（审查报告 §8.10.2）：route-v2 只平移车位排 y 坐标、不改表结构；旧库中
``parking_space.x/z/rotation`` 仍是旧坐标。迁移步骤：

1. **备份**：用 SQLite 在线备份 API 把当前 DB 一致性复制到
   ``<data_dir>/backups/layout_migrate_<时间戳>/parking.db``（WAL 安全）；
2. **导出声明**：从当前 DB 聚合 ``zone → count/kind``（等价
   ``GET /api/admin/layout/export``）；空库回退 ``config.default_layout``；
3. **重建车位**：``LayoutService.apply_layout`` 用新 manifest 几何 upsert 80 个
   同编号车位（同 ``space_no``，不新增/删除），并写 ``LayoutHistory`` 版本留痕；
4. **校验**：坐标与期望逐位比对（容差 1e-3 m）、在场占用/车牌保持、版本 +1；
5. **幂等**：再次运行若差异为 0 则直接返回 ``already_current``（不写版本、不再备份）；
6. ``--dry-run``：只计算差异与报告，不写库；``--empty-lot``：存在在场记录时拒绝迁移
   （报告 §8.10.2：在库车辆车位坐标会变、视觉跳位，建议空场迁移）。

本模块可被测试直接调用（``run_migration``），CLI 外壳见
``tools/migrate_layout_v2.py``。
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.config import AppConfig, load_config, set_config
from ..core.timeutil import utcnow
from .generator import LayoutGenerator, decls_from_dicts
from .manifest import Manifest, get_manifest

log = logging.getLogger("layout.migrate")

#: 坐标比对容差（米）。
TOL = 1e-3

#: 状态 → 进程退出码（CLI 使用）。
EXIT_CODES = {
    "migrated": 0,
    "already_current": 0,
    "planned": 0,
    "refused_occupied": 2,
    "no_manifest": 1,
    "error": 1,
}


# ---------------------------------------------------------------------------
# 导出声明 / 规划差异
# ---------------------------------------------------------------------------
def export_declaration(container) -> list[dict]:
    """从当前 DB 聚合布局声明（zone/count/kind）；空库回退 config.default_layout。"""
    zones: dict[str, dict] = {}
    for sp in container.space_repo.list_active():
        item = zones.setdefault(sp.zone, {"zone": sp.zone, "count": 0,
                                          "kind": sp.kind or "normal"})
        item["count"] += 1
    if not zones:
        return [dict(item) for item in (container.cfg.default_layout or [])]
    return [zones[k] for k in sorted(zones)]


def _coord_diff(before, after) -> dict:
    return {
        "space_no": after.space_no,
        "zone": after.zone,
        "old": {"x": before.x, "z": before.z, "rotation": before.rotation},
        "new": {"x": after.x, "z": after.z, "rotation": after.rotation},
        "dx": round(after.x - before.x, 4),
        "dz": round(after.z - before.z, 4),
    }


def plan_migration(container, decls: Optional[list[dict]] = None) -> dict:
    """计算迁移差异（只读，不写库）。"""
    decls = decls if decls is not None else export_declaration(container)
    gen: LayoutGenerator = container.layout.gen
    expected = {s.space_no: s for s in gen.generate(decls_from_dicts(decls))}
    current = container.layout.specs_from_db()
    added = sorted(k for k in expected if k not in current)
    removed = sorted(k for k in current if k not in expected)
    changed = sorted(
        k for k in expected
        if k in current and (
            abs(current[k].x - expected[k].x) > TOL
            or abs(current[k].z - expected[k].z) > TOL
            or abs(current[k].rotation - expected[k].rotation) > TOL))
    samples = [_coord_diff(current[k], expected[k]) for k in changed[:8]]
    return {
        "declaration": decls,
        "expected": expected,
        "current": current,
        "added": added,
        "removed": removed,
        "changed": changed,
        "changed_samples": samples,
        "unchanged": len(expected) - len(added) - len(changed),
    }


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def backup_database(db_file: str | Path, data_dir: str | Path,
                    tag: str = "layout_migrate") -> Optional[Path]:
    """SQLite 在线一致性备份；返回备份 DB 路径（源库不存在时返回 None）。"""
    src_path = Path(db_file)
    if not src_path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = Path(data_dir) / "backups" / f"{tag}_{stamp}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "parking.db"
    # 用 sqlite3 在线备份（WAL 模式下也一致），而不是直接 copy 文件
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    # 顺带保存 manifest 快照，便于事后追溯迁移依据
    manifest = get_manifest()
    if manifest is not None and manifest.path.is_file():
        try:
            shutil.copy2(manifest.path, dest_dir / "lot_v2.manifest.json")
        except OSError:  # pragma: no cover - 只影响留档
            log.warning("备份 manifest 失败: %s", manifest.path)
    log.info("已备份数据库 → %s", dest)
    return dest


# ---------------------------------------------------------------------------
# 执行迁移
# ---------------------------------------------------------------------------
def _occupied_snapshot(container) -> dict[str, Optional[str]]:
    return {sp.space_no: sp.current_plate
            for sp in container.space_repo.list_active() if sp.status == "occupied"}


def _verify(container, expected: Mapping[str, Any]) -> dict:
    """迁移后校验：坐标、数量、占用/车牌保持。"""
    actual = container.layout.specs_from_db()
    mismatches = []
    for space_no, want in expected.items():
        got = actual.get(space_no)
        if got is None:
            mismatches.append({"space_no": space_no, "reason": "missing"})
            continue
        if (abs(got.x - want.x) > TOL or abs(got.z - want.z) > TOL
                or abs(got.rotation - want.rotation) > TOL):
            mismatches.append({
                "space_no": space_no, "reason": "coord_mismatch",
                "got": {"x": got.x, "z": got.z, "rotation": got.rotation},
                "want": {"x": want.x, "z": want.z, "rotation": want.rotation}})
    extra = sorted(k for k in actual if k not in expected)
    return {
        "expected_spaces": len(expected),
        "actual_spaces": len(actual),
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
        "extra_spaces": extra[:20],
        "consistent": not mismatches and not extra,
    }


def run_migration(cfg: Optional[AppConfig] = None, *,
                  dry_run: bool = False,
                  empty_lot: bool = False,
                  backup: bool = True,
                  operator: str = "migrate_layout_v2",
                  report_path: Optional[str | Path] = None,
                  container=None) -> dict:
    """执行（或演练）旧布局 → route-v2 manifest 坐标迁移。

    返回报告 dict（含 ``status`` 与 ``exit_code``）；不抛业务异常，异常写入
    ``status='error'``（CLI 据此返回非 0）。
    """
    from ..gateway.context import Container  # 延迟导入，避免 layout 包 → gateway 循环

    started = utcnow()
    cfg = cfg or load_config(force=True)
    set_config(cfg)
    report: dict = {
        "tool": "migrate_layout_v2",
        "started_at": started.isoformat() + "Z",
        "dry_run": bool(dry_run),
        "empty_lot": bool(empty_lot),
        "db_file": str(cfg.paths.db_file),
        "data_dir": str(cfg.paths.data_dir),
        "manifest": {},
        "status": "error",
        "exit_code": 1,
        "messages": [],
    }
    own_container = container is None
    try:
        c = container if container is not None else Container(cfg)
        try:
            if own_container and not dry_run:
                c.bootstrap()
            manifest: Optional[Manifest] = get_manifest(cfg) or c.layout.manifest
            report["manifest"] = (manifest.meta() if manifest is not None
                                  else {"layout_source": "legacy"})
            if manifest is None:
                report["status"] = "no_manifest"
                report["messages"].append(
                    "未找到/校验失败的布局 manifest：迁移需要 route-v2 manifest，"
                    "已中止（请先提供 assets_3d/layout/lot_v2.json）")
                return _finish(report, report_path)
            report["manifest"]["hash"] = manifest.manifest_hash
            report["manifest"]["road_graph_version"] = manifest.road_graph_version

            # 1) 在场车辆检查（建议空场迁移）
            opens = c.parking_repo.all_open()
            report["occupied_records"] = [
                {"record_id": r.id, "plate": r.plate, "space_no": r.space_no}
                for r in opens]
            if opens and empty_lot:
                report["status"] = "refused_occupied"
                report["messages"].append(
                    f"检测到 {len(opens)} 条在场记录，--empty-lot 要求空场后迁移；已中止。"
                    "（可先人工/正常出场，或去掉 --empty-lot 承担车位坐标跳位风险）")
                return _finish(report, report_path)
            if opens:
                report["messages"].append(
                    f"警告：有 {len(opens)} 条在场记录参与迁移，迁移后这些车辆的车位坐标"
                    "会变化（视觉跳位），建议空场迁移")
            report["occupied_before"] = _occupied_snapshot(c)

            # 2) 规划差异
            old_version = c.layout.current_version()
            decls = export_declaration(c)
            plan = plan_migration(c, decls)
            report.update({
                "old_version": old_version,
                "declaration": plan["declaration"],
                "added": plan["added"][:20],
                "removed": plan["removed"][:20],
                "changed_count": len(plan["changed"]),
                "changed_samples": plan["changed_samples"],
                "unchanged_count": plan["unchanged"],
                "added_count": len(plan["added"]),
                "removed_count": len(plan["removed"]),
                "zone_count": len(plan["declaration"]),
                "space_count": len(plan["expected"]),
            })
            no_change = not plan["added"] and not plan["removed"] and not plan["changed"]
            if dry_run:
                report["status"] = "planned"
                report["messages"].append(
                    "dry-run：以上为将发生的变更，未写库、未备份")
                return _finish(report, report_path)
            if no_change:
                report["status"] = "already_current"
                report["new_version"] = old_version
                report["verification"] = _verify(c, plan["expected"])
                report["messages"].append(
                    "当前布局已与 manifest 一致（差异 0），无需迁移；未写 LayoutHistory")
                return _finish(report, report_path)

            # 3) 备份
            backup_path = None
            if backup:
                backup_path = backup_database(cfg.paths.db_file, cfg.paths.data_dir)
                report["backup_path"] = str(backup_path) if backup_path else None

            # 4) 重建车位（先落库; apply_layout 内部含安全校验与 LayoutHistory 留痕）
            with c.db.scope():
                diff = c.layout.apply_layout(plan["declaration"],
                                             operator=operator, publish=False)
                c.db.commit()
            new_version = c.layout.current_version()
            report.update({
                "status": "migrated",
                "new_version": new_version,
                "diff": diff.summary,
                "history_recorded": new_version == old_version + 1,
            })
            # 5) 校验
            verification = _verify(c, plan["expected"])
            occupied_after = _occupied_snapshot(c)
            preserved = {
                space_no: plate
                for space_no, plate in report.get("occupied_before", {}).items()
                if occupied_after.get(space_no) == plate}
            verification["occupied_preserved"] = (
                len(preserved) == len(report.get("occupied_before", {})))
            verification["occupied_before_count"] = len(report.get("occupied_before", {}))
            verification["occupied_after_count"] = len(occupied_after)
            report["verification"] = verification
            if not verification["consistent"]:
                report["status"] = "error"
                report["messages"].append("迁移后校验失败，请用备份回滚后排查")
                return _finish(report, report_path)
            # 6) 广播（离线 CLI 无订阅者；保留事件语义，便于未来常驻迁移服务）
            try:
                from ..core.events import LayoutChanged
                c.bus.publish(LayoutChanged(version=new_version, diff=diff.summary))
            except Exception:  # noqa: BLE001 - 广播失败不影响迁移结果
                log.exception("迁移后广播 LayoutChanged 失败")
            report["messages"].append(
                f"迁移完成：版本 v{old_version} → v{new_version}，"
                f"{len(plan['changed'])} 个车位坐标更新，校验通过")
            return _finish(report, report_path)
        finally:
            if own_container:
                c.close()
    except Exception as exc:  # noqa: BLE001 - CLI 需要结构化错误
        log.exception("布局迁移失败")
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["messages"].append("迁移失败：" + report["error"])
        return _finish(report, report_path)


def _finish(report: dict, report_path: Optional[str | Path]) -> dict:
    report["finished_at"] = utcnow().isoformat() + "Z"
    report["exit_code"] = EXIT_CODES.get(report.get("status", "error"), 1)
    if report_path:
        out = Path(report_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        report["report_path"] = str(out)
    return report


def format_report(report: Mapping[str, Any]) -> str:
    """人类可读摘要（CLI 输出）。"""
    lines = [
        f"[migrate_layout_v2] status={report.get('status')} "
        f"(exit_code={report.get('exit_code')})",
        f"  manifest: {report.get('manifest', {}).get('manifest_path')} "
        f"v{report.get('manifest', {}).get('manifest_version')} "
        f"{report.get('manifest', {}).get('manifest_hash')}",
        f"  db: {report.get('db_file')}",
        f"  版本: v{report.get('old_version')} → v{report.get('new_version')}",
        f"  车位: {report.get('space_count')} 个 / 声明 {report.get('zone_count')} 区 / "
        f"变更 {report.get('changed_count')} / 新增 {report.get('added_count')} / "
        f"移除 {report.get('removed_count')} / 未变 {report.get('unchanged_count')}",
    ]
    if report.get("changed_samples"):
        lines.append("  变更样例（前 5）:")
        for s in report["changed_samples"][:5]:
            lines.append(
                f"    {s['space_no']}: z {s['old']['z']} → {s['new']['z']} "
                f"(dz={s['dz']}), rot {s['old']['rotation']} → {s['new']['rotation']}")
    if report.get("backup_path"):
        lines.append(f"  备份: {report['backup_path']}")
    verification = report.get("verification")
    if verification:
        lines.append(
            f"  校验: consistent={verification.get('consistent')} "
            f"mismatch={verification.get('mismatch_count')} "
            f"occupied_preserved={verification.get('occupied_preserved')}")
    for msg in report.get("messages") or []:
        lines.append(f"  说明: {msg}")
    return "\n".join(lines)

