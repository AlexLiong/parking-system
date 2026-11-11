"""旧库布局迁移 CLI（tools/migrate_layout_v2.py）——route-v2 车位坐标一次性迁移。

库实现见 ``server/layout/migrate.py``；本脚本只做参数解析 / 环境准备 / 输出。

用法（项目根执行）：
    python tools/migrate_layout_v2.py --dry-run              # 演练：只看差异，不写库
    python tools/migrate_layout_v2.py                        # 备份 → 迁移 → 校验
    python tools/migrate_layout_v2.py --empty-lot            # 仅空场时迁移（有在场记录则退出码 2）
    python tools/migrate_layout_v2.py --no-backup --report data/reports/migrate.json

退出码：0=成功/已是最新/演练完成；1=未找到 manifest 或执行失败；2=--empty-lot 拒绝。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.config import load_config, set_config  # noqa: E402
from server.layout.migrate import format_report, run_migration  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="route-v2 布局迁移：备份 DB → 导出声明 → 按 manifest 重建车位 → "
                    "LayoutHistory 留痕 → 校验（幂等）")
    ap.add_argument("--config", type=str, default=None,
                    help="配置文件路径（默认 PARKING_CONFIG 或项目根 config.yaml）")
    ap.add_argument("--db", type=str, default=None,
                    help="覆盖配置中的 db_file（迁移临时库/副本时使用）")
    ap.add_argument("--dry-run", action="store_true", help="只演练：计算差异，不写库")
    ap.add_argument("--empty-lot", action="store_true",
                    help="要求空场迁移：存在在场记录时拒绝（退出码 2）")
    ap.add_argument("--no-backup", action="store_true", help="跳过 DB 备份（不建议）")
    ap.add_argument("--operator", type=str, default="migrate_layout_v2",
                    help="写入 LayoutHistory 的操作人标识")
    ap.add_argument("--report", type=str, default=None,
                    help="把完整迁移报告写入该 JSON 路径")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印完整报告")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, force=True)
    if args.db:
        db_path = Path(args.db)
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        cfg.paths.db_file = db_path
        cfg.paths.data_dir = db_path.parent
    set_config(cfg)

    report = run_migration(
        cfg,
        dry_run=args.dry_run,
        empty_lot=args.empty_lot,
        backup=not args.no_backup,
        operator=args.operator,
        report_path=args.report,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return int(report.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
