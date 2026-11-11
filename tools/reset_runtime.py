#!/usr/bin/env python3
"""演示运行时重置 CLI（实现 B）。

两种模式，均**不要求后端服务在运行**（直接操作数据库/文件）：

    python tools/reset_runtime.py --reset
        只清"开放运行时状态"：当前会话在场记录标 aborted（保留审计）、释放车位、
        清待办/异常/车辆运行态、道闸关闭、开新会话、仿真时钟归零；
        保留已完成历史、费率、布局、名单与 data/scenario.json。

    python tools/reset_runtime.py --resetdata
        先备份（数据库 + scenario + 运行目录清单）到 data/backups/resetdata-<ts>/，
        再完整重建数据库并重新灌演示数据（名单/历史/在场），剧本恢复内置默认。

退出码：0 成功；1 失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from server.core.config import AppConfig, load_config  # noqa: E402
from server.gateway.context import Container  # noqa: E402


def _log(msg: str) -> None:
    print(f"[reset] {msg}", flush=True)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _copy_db_family(src: Path, dst_dir: Path) -> list[dict]:
    """备份 SQLite 主文件 + WAL/SHM（存在才复制），返回清单。"""
    copied: list[dict] = []
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(src) + suffix)
        if not p.exists():
            continue
        dst = dst_dir / p.name
        shutil.copy2(p, dst)
        copied.append({"file": p.name, "bytes": dst.stat().st_size,
                       "sha256": _sha256(dst)})
    return copied


def backup_data(cfg: AppConfig) -> Path:
    """备份数据库 + 场景 + 关键目录清单到 data/backups/resetdata-<ts>/。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = Path(cfg.paths.data_dir) / "backups" / f"resetdata-{ts}"
    target.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"created_at": ts, "db_file": str(cfg.paths.db_file),
                      "backup_dir": str(target), "files": []}
    db_path = Path(cfg.paths.db_file)
    if db_path.exists():
        manifest["files"] += _copy_db_family(db_path, target)
    scenario = Path(cfg.paths.data_dir) / "scenario.json"
    if scenario.exists():
        dst = target / "scenario.json"
        shutil.copy2(scenario, dst)
        manifest["files"].append({"file": "scenario.json",
                                  "bytes": dst.stat().st_size,
                                  "sha256": _sha256(dst)})
    for sub in ("photos", "reports", "logs"):
        src = Path(cfg.paths.data_dir) / sub
        if src.is_dir():
            count = sum(1 for _ in src.rglob("*") if _.is_file())
            manifest.setdefault("dirs", {})[sub] = {"path": str(src), "files": count}
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"已备份到 {target}（{len(manifest['files'])} 个文件）")
    return target


def _remove_db_family(cfg: AppConfig) -> None:
    db_path = Path(cfg.paths.db_file)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
            _log(f"已删除 {p.name}")


def _runtime_summary(container: Container) -> dict:
    session = container.state.current_session()
    return {
        "session_id": session.id if session else None,
        "open_records": container.parking_repo.count_open(),
        "free_total": container.space_repo.free_total(),
        "pending": len(container.access.list_pending("pending")),
        "sim_time": float(container.simulation.sim_time),
        "layout_version": container.layout.current_version(),
    }


def reset_runtime(cfg: AppConfig, *, reason: str = "cli_reset") -> dict:
    """--reset：清开放运行时状态，保留已完成历史/费率/布局/名单/场景文件。"""
    cfg.ensure_dirs()
    container = Container(cfg)
    try:
        container.bootstrap()
        container.start()
        before = _runtime_summary(container)
        report = container.state.reset(
            request_id=f"cli-reset-{uuid4().hex[:12]}", mode="soft",
            operator="cli", reason=reason, force=True)
        after = _runtime_summary(container)
        _log(f"runtime reset 完成: {json.dumps(report, ensure_ascii=False)}")
        return {"mode": "reset", "before": before, "after": after,
                "reset_report": report}
    finally:
        container.close()


def resetdata(cfg: AppConfig, *, days: int = 7, records: int = 60,
              inhouse: int = 6, seed: int = 42) -> dict:
    """--resetdata：备份 → 完整重建数据库 → bootstrap → 重新灌演示数据。"""
    cfg.ensure_dirs()
    backup_dir = backup_data(cfg)
    _remove_db_family(cfg)
    # 剧本恢复内置默认（data/scenario.json 已在备份中）
    scenario = Path(cfg.paths.data_dir) / "scenario.json"
    if scenario.exists():
        scenario.unlink()
        _log("已删除 data/scenario.json（恢复内置默认剧本）")

    # 重建 + seed（复用 seed_demo 的真实计费/车位逻辑）
    import seed_demo  # noqa: E402 - tools 目录已加入 sys.path

    container = Container(cfg)
    try:
        summary = container.bootstrap()
        _log(f"数据库重建完成: {summary}")
        rng = random.Random(seed)
        samples = seed_demo.load_plate_samples(cfg)
        # 注入"新入场"记录时按仿真业务时间（与在线行为一致）
        lists = seed_demo.seed_lists(container, rng, samples)
        hist = seed_demo.seed_history(container, rng, samples, days=days,
                                      records=records)
        inhouse_result = seed_demo.seed_inhouse(container, rng, samples, count=inhouse)
        report = {
            "mode": "resetdata", "backup_dir": str(backup_dir),
            "db_file": str(cfg.paths.db_file),
            "bootstrap": summary,
            "lists": lists, "history": hist, "inhouse": inhouse_result,
            "runtime": _runtime_summary(container),
        }
        _log(f"演示数据重灌完成: {json.dumps({k: report[k] for k in ('lists','history','inhouse')}, ensure_ascii=False)}")
        return report
    finally:
        container.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="演示运行时重置（--reset / --resetdata）")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--reset", action="store_true",
                      help="只清开放记录/运行时状态（保留已完成历史）")
    mode.add_argument("--resetdata", action="store_true",
                      help="备份后完整重建数据库并重灌演示数据")
    ap.add_argument("--reason", default="cli_reset", help="操作原因（写审计）")
    ap.add_argument("--days", type=int, default=7, help="--resetdata 历史回溯天数")
    ap.add_argument("--records", type=int, default=60, help="--resetdata 历史记录数")
    ap.add_argument("--in", dest="inhouse", type=int, default=6,
                    help="--resetdata 当前在场记录数")
    ap.add_argument("--seed", type=int, default=42, help="--resetdata 随机种子")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    args = ap.parse_args(argv)

    cfg = load_config()
    cfg.ensure_dirs()
    try:
        if args.resetdata:
            result = resetdata(cfg, days=args.days, records=args.records,
                               inhouse=args.inhouse, seed=args.seed)
        else:
            result = reset_runtime(cfg, reason=args.reason)
    except Exception as exc:  # noqa: BLE001 - CLI 需给出可读失败原因
        print(f"[reset] 失败: {exc}", file=sys.stderr, flush=True)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    else:
        _log("完成，服务可安全启动（--reset 与 --resetdata 均可离线运行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
