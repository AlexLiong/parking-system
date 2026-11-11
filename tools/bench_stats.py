"""Isolated reproducible 100k history benchmark; never opens the demo database.

P1-13：门禁阈值不再硬编码；默认值仍为 2.0s（不静默放宽），可由
``tools/bench_limits.json`` 的平台配置 / ``PARKING_BENCH_MAX_SECONDS`` /
``--limit-seconds`` 显式覆盖，并在输出 JSON 中记录 ``limit_seconds`` / ``limit_source``。
功能断言（10 万行进/出场计数、营收、分页）保持不变。
"""
from __future__ import annotations

import argparse
import copy
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
sys.path.insert(0, str(TOOLS))
from sqlalchemy import insert  # noqa: E402

from ci_support import bench_limit_for  # noqa: E402
from server.core.config import load_config, set_config  # noqa: E402
from server.gateway.context import Container  # noqa: E402
from server.repository.models import ParkingRecord, PaymentRecord  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="100k 历史数据统计基准（隔离临时库）")
    parser.add_argument("out", nargs="?",
                        default="../_qa_audit/repair/T5/evidence",
                        help="证据输出目录（默认 ../_qa_audit/repair/T5/evidence）")
    parser.add_argument("--limit-seconds", type=float, default=None,
                        help="显式阈值覆盖（默认读 tools/bench_limits.json，2.0s）")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    limit, limit_source = bench_limit_for(platform_name=platform.system(),
                                          override=args.limit_seconds)
    with tempfile.TemporaryDirectory(prefix="parking-bench-") as tmp:
        cfg = copy.deepcopy(load_config())
        root = Path(tmp)
        cfg.paths.data_dir = root
        cfg.paths.db_file = root / "benchmark.db"
        cfg.paths.photos_dir = root / "photos"
        cfg.paths.reports_dir = root / "reports"
        cfg.paths.logs_dir = root / "logs"
        cfg.ensure_dirs()
        set_config(cfg)
        c = Container(cfg)
        c.bootstrap()
        try:
            start = datetime(2026, 1, 1)
            for batch in range(10):
                records, payments = [], []
                for i in range(batch * 10000, (batch + 1) * 10000):
                    entry = start + timedelta(minutes=i * 4)
                    exit_ = entry + timedelta(minutes=60)
                    plate = "京A" + f"{i:05X}"
                    records.append(dict(id=i + 1, plate=plate, plate_color="blue",
                                        entry_time=entry, exit_time=exit_, status="out",
                                        created_at=entry, temporary=False, manual_entry=False))
                    payments.append(dict(parking_record_id=i + 1, plate=plate,
                                         amount_due=5, amount_paid=5, paid_at=exit_,
                                         minutes=60, billable_units=1, method="mock_scan"))
                with c.db.scope():
                    c.db.session.execute(insert(ParkingRecord), records)
                    c.db.session.execute(insert(PaymentRecord), payments)
            timings: dict[str, float] = {}
            values: dict[str, dict] = {}
            for period in ["day", "month", "year"]:
                c.db.remove()
                at = time.perf_counter()
                value = c.stats.stats(period, "2026-05-10")
                timings[period] = time.perf_counter() - at
                values[period] = value.model_dump(mode="json")
            c.db.remove()
            at = time.perf_counter()
            page = c.stats.search_records(plate="京A", page_size=200)
            timings["search"] = time.perf_counter() - at
            assert page["total"] == 100000 and len(page["items"]) == 200
            assert values["year"]["count_in"] == 100000
            assert values["year"]["count_out"] == 100000
            assert values["year"]["revenue"] == 500000
            max_seconds = max(timings.values())
            result = {
                "rows": 100000,
                "seed": "fixed four minute interval",
                "environment": platform.platform(),
                "python": sys.version,
                "seconds": timings,
                "limit_seconds": limit,
                "limit_source": limit_source,
                "max_seconds": max_seconds,
                "year": values["year"],
                "search_count": page["total"],
                "pass": max_seconds <= limit,
            }
            (out / "stats-100k.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"rows": 100000, "seconds": timings,
                              "limit_seconds": limit, "limit_source": limit_source,
                              "max_seconds": round(max_seconds, 4),
                              "pass": result["pass"]}, ensure_ascii=False))
            return 0 if result["pass"] else 1
        finally:
            c.close()


if __name__ == "__main__":
    raise SystemExit(main())
