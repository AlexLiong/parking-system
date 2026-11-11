"""CLI：按保留期/容量清理上传、裁剪、相机截图与视频抽帧目录。

只清理 ``data/photos/{uploads,crops,camera}``，不会触碰 datasets/演示源图、日志、
reports 证据。默认读取 config.yaml 的 retention 配置；可临时覆盖参数。

示例：
  python tools/cleanup_photos.py --dry-run
  python tools/cleanup_photos.py --days 3 --max-mb 512
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.core.config import load_config  # noqa: E402
from server.repository.db import init_db  # noqa: E402
from server.retention import cleanup_photos  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="照片/视频抽帧保留期清理")
    parser.add_argument("--config", default=None, help="配置文件（默认 config.yaml）")
    parser.add_argument("--days", type=float, default=None,
                        help="覆盖 retention.max_age_days（默认 7）")
    parser.add_argument("--max-mb", type=int, default=None,
                        help="覆盖 retention.max_total_mb（默认 2048）")
    parser.add_argument("--keep-recent", type=int, default=None,
                        help="每个子目录保留最新 N 个文件")
    parser.add_argument("--min-age-hours", type=int, default=None,
                        help="容量清理保护的小时数")
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    args = parser.parse_args(argv)

    cfg = load_config(args.config, force=True)
    override = {
        "max_age_days": args.days,
        "max_total_mb": args.max_mb,
        "keep_recent_files": args.keep_recent,
        "min_age_hours_for_capacity": args.min_age_hours,
    }
    for key, value in override.items():
        if value is not None:
            setattr(cfg.retention, key, value)

    # M3：若数据库存在，清理文件后同步清除 parking_record 中的照片引用
    db = None
    try:
        if Path(cfg.paths.db_file).exists():
            db = init_db(cfg.paths.db_file)
        report = cleanup_photos(cfg, dry_run=args.dry_run, db=db)
    finally:
        if db is not None:
            db.close()
    data = report.to_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"enabled={data['enabled']} dry_run={data['dry_run']} "
              f"scanned={data['scanned_files']} deleted_age={data['deleted_age']} "
              f"deleted_capacity={data['deleted_capacity']} "
              f"freed={data['bytes_freed'] / (1024 * 1024):.2f}MiB "
              f"kept={data['kept_files']} ({data['kept_bytes'] / (1024 * 1024):.2f}MiB)")
        if data["errors"]:
            for error in data["errors"]:
                print(f"WARN {error}", file=sys.stderr)
    return 0 if not data["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
