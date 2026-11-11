"""演示数据种子（tools/seed_demo.py）。

向数据库灌入一批**贴近真实运营**的演示数据，便于开箱即用地体验管理端各视图：
- 名单：白名单 / 月租 / 黑名单若干（FR-10.2/10.3）；
- 历史进出场记录：跨过去 N 天，含已缴费出场（走真实计费算法 FeeCalculator）；
- 当前在场记录：占用若干车位（保证车位状态与在场记录一致，FR-2）；
- 记录尽量绑定 ``datasets/demo-lpr`` 里的演示车牌照片（车牌与照片一致），
  体现"识别输入与 3D 建模解耦"（prompt / FR-1.3）。

**幂等/安全**：默认追加；``--reset`` 先清空业务数据（记录/收费/识别日志/异常/操作日志/名单），
但**保留**布局、费率与管理员账号（这些由 bootstrap 负责）。

数据直接经 Container 的仓储写入（不经 HTTP），复用与线上一致的计费/车位逻辑。

运行示例：
    python tools/seed_demo.py                       # 追加演示数据
    python tools/seed_demo.py --reset               # 先清空业务数据再灌
    python tools/seed_demo.py --days 30 --records 80 --in 6 --seed 7
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import delete  # noqa: E402

from server.core.config import load_config  # noqa: E402
from server.core.domain import PlateColor  # noqa: E402
from server.core.timeutil import utcnow  # noqa: E402
from server.gateway.context import Container  # noqa: E402
from server.lpr import charset  # noqa: E402
from server.repository.models import (IncidentLog, OpLog, ParkingRecord,  # noqa: E402
                                      PaymentRecord, RecognitionLog, VehicleList)


def _log(msg: str) -> None:
    print(f"[seed] {msg}", flush=True)


def load_plate_samples(cfg) -> list[dict]:
    """从 demo-lpr manifest（或旧 labels.csv）读取演示车牌样本。

    新目录为标准 ``datasets/demo-lpr``（MIT 样式生成演示图）；旧
    ``datasets/plates`` 已删除，不再作为来源。
    """
    root = Path(cfg.paths.plates_dataset)
    manifest = root / "manifest.json"
    samples: list[dict] = []
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for row in data.get("samples", []):
            fp = root / row["file"]
            if fp.exists():
                samples.append({
                    "plate": row["plate"],
                    "color": row.get("color") or (
                        "green" if str(row.get("style", "")).startswith("green") else "blue"),
                    # 相对项目根，前端 mediaUrl 会映射到 /datasets/...
                    "photo": f"datasets/demo-lpr/{row['file']}",
                })
    return samples


def reset_business_data(container: Container) -> None:
    """清空业务数据（保留布局/费率/管理员）。"""
    with container.db.scope() as s:
        for model in (PaymentRecord, RecognitionLog, IncidentLog, OpLog,
                      ParkingRecord, VehicleList):
            s.execute(delete(model))
        # 释放所有车位占用（布局本身保留）
        for sp in container.space_repo.list_all():
            sp.status = "free"
            sp.current_plate = None
    _log("已清空业务数据（记录/收费/识别日志/异常/操作日志/名单/车位占用）")


def seed_lists(container: Container, rng: random.Random, samples: list[dict]) -> dict:
    """灌入名单：白/月租/黑。优先用样本里的真实车牌。"""
    created = {"white": 0, "monthly": 0, "black": 0}
    plan = [("white", 3, "内部公务车，免费放行"),
            ("monthly", 3, "月租包月车辆"),
            ("black", 2, "欠费/违规，禁止入场")]
    pool = list(samples)
    rng.shuffle(pool)
    sample_plates = {s["plate"] for s in samples}
    idx = 0
    with container.db.scope():
        for list_type, count, note in plan:
            for _ in range(count):
                if list_type == "black":
                    # 黑名单不能落在演示剧本车辆上，否则 E1-E6 会被拦在入口。
                    plate = _random_plate(rng, PlateColor.BLUE)
                    while plate in sample_plates:
                        plate = _random_plate(rng, PlateColor.BLUE)
                elif pool and idx < len(pool):
                    plate = pool[idx]["plate"]
                    idx += 1
                else:
                    plate = _random_plate(rng, PlateColor.BLUE)
                valid_until = None
                if list_type == "monthly":
                    valid_until = utcnow() + timedelta(days=rng.randint(20, 90))
                container.list_repo.upsert(plate=plate, list_type=list_type,
                                           valid_until=valid_until, note=note)
                created[list_type] += 1
    return created


def _random_plate(rng: random.Random, color: PlateColor) -> str:
    """无字体依赖地生成语法合法号牌（城市码来自省份→发牌机关映射）。"""
    return charset.generate_plate_type(color.value, rng)


def _pick(rng: random.Random, samples: list[dict], color: PlateColor) -> dict:
    """从样本里挑一个匹配颜色的车牌；无样本则合成。"""
    if samples:
        matched = [s for s in samples if s["color"] == color.value]
        if matched:
            return rng.choice(matched)
        return rng.choice(samples)
    return {"plate": _random_plate(rng, color), "color": color.value, "photo": None}


def seed_history(container: Container, rng: random.Random, samples: list[dict],
                 *, days: int, records: int) -> dict:
    """灌入历史已完成记录（已缴费出场），费用走真实 FeeCalculator。"""
    now = utcnow()
    rule = container.billing.current_rule()          # domain 视图，含各字段
    rule_orm = container.fee_repo.get(rule.id) if rule.id else None
    calc = container.billing.calc
    zones = sorted({sp.zone for sp in container.space_repo.list_active()}) or ["A"]
    made = {"records": 0, "payments": 0, "revenue": 0.0}

    with container.db.scope():
        for _ in range(records):
            color = PlateColor.GREEN if rng.random() < 0.28 else PlateColor.BLUE
            s = _pick(rng, samples, color)
            # 进场时间：过去 days 天内随机
            back = timedelta(days=rng.uniform(0, max(1, days)),
                             hours=rng.uniform(6, 22))
            entry = now - back
            # 停车时长：多为 0.5~6h，少量跨天
            r = rng.random()
            if r < 0.12:
                dur = timedelta(hours=rng.uniform(12, 40))     # 跨天，触发封顶
            elif r < 0.22:
                dur = timedelta(minutes=rng.uniform(3, 14))    # 免费时长内
            else:
                dur = timedelta(minutes=rng.uniform(35, 360))
            exit_t = entry + dur
            zone = rng.choice(zones)
            space_no = f"{zone}-{rng.randint(1, 20):02d}"
            rec = container.parking_repo.create(
                plate=s["plate"], plate_color=s["color"], entry_time=entry,
                space_no=space_no, entry_photo=s["photo"], entry_plate_crop=None,
                manual_entry=(rng.random() < 0.06), fee_rule_id=rule.id, status="in")
            container.parking_repo.mark_exit(rec, exit_time=exit_t,
                                             exit_photo=s["photo"], status="out")
            # 计费（用与出场一致的规则快照）
            bd = calc.compute(rec, exit_t, rule_orm or rule)
            container.payment_repo.create(
                parking_record_id=rec.id, plate=rec.plate, minutes=bd.minutes,
                billable_units=bd.billable_units, amount_due=bd.amount_due,
                amount_paid=bd.amount_due, method=rng.choice(["mock_scan", "mock_scan", "cash"]),
                operator="admin", breakdown=bd, paid_at=exit_t)
            made["records"] += 1
            made["payments"] += 1
            made["revenue"] += bd.amount_due
    return made


def seed_inhouse(container: Container, rng: random.Random, samples: list[dict],
                 *, count: int) -> dict:
    """灌入当前在场记录并占用车位（保持车位状态一致，FR-2）。"""
    now = utcnow()
    rule = container.billing.current_rule()
    spaces = container.space_repo.list_active()
    free = [sp for sp in spaces if sp.status == "free"]
    rng.shuffle(free)
    # 同牌只允许一条 open 记录（DB 有 uq_open_plate 部分唯一索引）：在场车牌去重，
    # 避免随机种子在同一批在场车里抽到同一块车牌（reset_runtime --resetdata 会用到）。
    used_plates = {sp.current_plate for sp in spaces if sp.current_plate}
    made = {"in": 0, "skipped_duplicate_plate": 0}
    with container.db.scope():
        for i in range(min(count, len(free))):
            color = PlateColor.GREEN if rng.random() < 0.3 else PlateColor.BLUE
            # 新能源优先落 C 区（若存在 ne_prefer 位）
            sp = free[i]
            s = _pick(rng, samples, color)
            attempts = 0
            while s["plate"] in used_plates and attempts < 50:
                s = _pick(rng, samples, color)
                attempts += 1
            if s["plate"] in used_plates:
                made["skipped_duplicate_plate"] += 1
                continue
            used_plates.add(s["plate"])
            entry = now - timedelta(minutes=rng.uniform(5, 300))
            container.parking_repo.create(
                plate=s["plate"], plate_color=s["color"], entry_time=entry,
                space_no=sp.space_no, entry_photo=s["photo"], fee_rule_id=rule.id,
                status="in")
            container.space_repo.occupy(sp.space_no, s["plate"])
            made["in"] += 1
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description="灌入演示数据")
    ap.add_argument("--days", type=int, default=30, help="历史记录回溯天数")
    ap.add_argument("--records", type=int, default=60, help="历史已完成记录数")
    ap.add_argument("--in", dest="inhouse", type=int, default=6, help="当前在场记录数")
    ap.add_argument("--reset", action="store_true", help="先清空业务数据再灌")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（可复现）")
    args = ap.parse_args()

    cfg = load_config()
    cfg.ensure_dirs()
    rng = random.Random(args.seed)

    container = Container(cfg)
    try:
        summary = container.bootstrap()      # 布局/费率/管理员
        _log(f"bootstrap: {summary}")

        if args.reset:
            reset_business_data(container)

        samples = load_plate_samples(cfg)
        _log(f"车牌照片样本：{len(samples)} 张"
             + ("" if samples else "（datasets/demo-lpr 未生成，将合成车牌号，照片留空）"))

        lists = seed_lists(container, rng, samples)
        hist = seed_history(container, rng, samples, days=args.days, records=args.records)
        inhouse = seed_inhouse(container, rng, samples, count=args.inhouse)

        _log("—— 演示数据灌入完成 ——")
        _log(f"名单   : 白 {lists['white']} / 月租 {lists['monthly']} / 黑 {lists['black']}")
        _log(f"历史记录: {hist['records']} 条（已缴费 {hist['payments']}，"
             f"累计营收 ¥{hist['revenue']:.2f}）")
        _log(f"当前在场: {inhouse['in']} 条（已占用车位）")
        _log(f"数据库  : {cfg.paths.db_file}")
        return 0
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
