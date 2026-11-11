"""进出记录与收费记录仓储（repository/parking_repo.py）。

写方法只做 ``add + flush``（拿到自增 id），提交由调用方的 ``db.scope()``
统一控制，以保证"创建记录 + 占用车位"等多表操作的原子性与"先落库后动作"。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, case, cast, func, or_, select, text

from ..core.domain import FeeBreakdown
from ..core.timeutil import iso_local, utcnow
from ..repository.db import Database
from ..repository.models import DemoSession, ParkingRecord, PaymentRecord

# SQLite 3.42+ 的 unixepoch(x,'subsec') 对文本 datetime 解析为精确到微秒的浮点秒；
# 旧版 SQLite 退回 julianday（双精度约几十微秒误差），用 50µs 容差消除边界误进位。
_UNIXEPOCH_SUBSEC: bool | None = None


def _supports_unixepoch_subsec(session) -> bool:
    global _UNIXEPOCH_SUBSEC
    if _UNIXEPOCH_SUBSEC is None:
        try:
            value = session.execute(
                text("SELECT unixepoch('2020-01-01 00:00:00.500000','subsec')")).scalar()
            _UNIXEPOCH_SUBSEC = (value is not None
                                 and abs(float(value) - 1577836800.5) < 1e-6)
        except Exception:  # noqa: BLE001 - 老 SQLite 不认 modifier
            _UNIXEPOCH_SUBSEC = False
    return _UNIXEPOCH_SUBSEC


class ParkingRecordRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    # ----------------------------- 查询 -----------------------------
    def get(self, record_id: int) -> Optional[ParkingRecord]:
        return self.s.get(ParkingRecord, record_id)

    def latest_closed(self, *, vehicle_id: Optional[str] = None,
                      plate: Optional[str] = None,
                      session_id: Optional[str] = None) -> Optional[ParkingRecord]:
        """最近一条已结束记录（M2：重复/迟到出口幂等判定）。"""
        conds = [ParkingRecord.status.in_(("out", "abnormal")),
                 ParkingRecord.exit_time.is_not(None)]
        if session_id:
            conds.append(ParkingRecord.session_id == session_id)
        if vehicle_id:
            conds.append(ParkingRecord.vehicle_id == vehicle_id)
        elif plate:
            conds.append(ParkingRecord.plate == plate)
        else:
            return None
        stmt = (select(ParkingRecord).where(*conds)
                .order_by(ParkingRecord.exit_time.desc(), ParkingRecord.id.desc())
                .limit(1))
        return self.s.execute(stmt).scalar_one_or_none()

    def find_open_by_vehicle(self, vehicle_id: str) -> Optional[ParkingRecord]:
        """按 vehicle_id 找当前 open 记录（H4 车辆驶离兜底）。"""
        if not vehicle_id:
            return None
        stmt = (select(ParkingRecord)
                .where(ParkingRecord.vehicle_id == vehicle_id,
                       ParkingRecord.status == "in")
                .order_by(ParkingRecord.entry_time.desc()).limit(1))
        return self.s.execute(stmt).scalar_one_or_none()

    def find_open_by_plate(self, plate: str, *, include_temporary: bool = True) -> Optional[ParkingRecord]:
        """查该车牌未出场记录（status='in'）。

        默认保留 temporary（兼容人工出场账单查询）；入场重复检查、在场车牌、
        HUD 计数等需要排除临时账单时显式传 ``include_temporary=False``。
        """
        stmt = (select(ParkingRecord)
                .where(ParkingRecord.plate == plate, ParkingRecord.status == "in")
                .order_by(ParkingRecord.entry_time.desc()).limit(1))
        if not include_temporary:
            stmt = stmt.where(ParkingRecord.temporary == False)
        return self.s.execute(stmt).scalar_one_or_none()

    def open_plates(self) -> list[str]:
        """当前所有真实在场车牌（排除临时账单，RR-1.2）。"""
        stmt = (select(ParkingRecord.plate)
                .where(ParkingRecord.status == "in", ParkingRecord.temporary == False)
                .distinct())
        return [r[0] for r in self.s.execute(stmt).all()]

    def count_open(self) -> int:
        stmt = (select(func.count()).select_from(ParkingRecord)
                .where(ParkingRecord.status == "in", ParkingRecord.temporary == False))
        return int(self.s.execute(stmt).scalar() or 0)

    def search(self, plate: Optional[str] = None, start: Optional[datetime] = None,
               end: Optional[datetime] = None, status: Optional[str] = None,
               page: int = 1, page_size: int = 20) -> tuple[list[ParkingRecord], int]:
        conds = []
        if plate:
            conds.append(ParkingRecord.plate.like(f"%{plate}%"))
        if start:
            conds.append(ParkingRecord.entry_time >= start)
        if end:
            conds.append(ParkingRecord.entry_time < end)
        if status:
            conds.append(ParkingRecord.status == status)
        base = select(ParkingRecord)
        count_stmt = select(func.count()).select_from(ParkingRecord)
        for c in conds:
            base = base.where(c)
            count_stmt = count_stmt.where(c)
        total = int(self.s.execute(count_stmt).scalar() or 0)
        stmt = base.order_by(ParkingRecord.entry_time.desc()).offset((page - 1) * page_size).limit(page_size)
        rows = list(self.s.execute(stmt).scalars().all())
        return rows, total

    def records_in_range(self, start: datetime, end: datetime) -> list[ParkingRecord]:
        stmt = (select(ParkingRecord)
                .where(ParkingRecord.entry_time >= start, ParkingRecord.entry_time < end,
                       ParkingRecord.temporary == False)
                .order_by(ParkingRecord.entry_time))
        return list(self.s.execute(stmt).scalars().all())

    def exits_in_range(self, start: datetime, end: datetime) -> list[ParkingRecord]:
        stmt = (select(ParkingRecord)
                .where(ParkingRecord.exit_time.is_not(None),
                       ParkingRecord.exit_time >= start, ParkingRecord.exit_time < end))
        return list(self.s.execute(stmt).scalars().all())

    def all_open(self) -> list[ParkingRecord]:
        stmt = select(ParkingRecord).where(ParkingRecord.status == "in")
        return list(self.s.execute(stmt).scalars().all())

    def overlapping(self, start: datetime, end: datetime) -> list[ParkingRecord]:
        """与 [start, end) 有时间重叠的记录（用于峰值在场统计）。

        条件：进场时间 < end 且（未出场 或 出场时间 >= start）。
        """
        stmt = select(ParkingRecord).where(
            ParkingRecord.entry_time < end, ParkingRecord.temporary == False,
            or_(ParkingRecord.exit_time.is_(None), ParkingRecord.exit_time >= start))
        return list(self.s.execute(stmt).scalars().all())

    def statistics_entry_count(self,start,end):
        return int(self.s.execute(select(func.count()).select_from(ParkingRecord).where(
            ParkingRecord.entry_time>=start,ParkingRecord.entry_time<end,ParkingRecord.temporary==False)).scalar() or 0)

    def statistics_day_counts(self,start,end,offset_minutes):
        def counts(column, extra=()):
            day=func.date(column,f'{offset_minutes:+d} minutes')
            return dict(self.s.execute(select(day,func.count()).where(
                column>=start,column<end,*extra).group_by(day)).all())
        return counts(ParkingRecord.entry_time,(ParkingRecord.temporary==False,)),counts(ParkingRecord.exit_time)

    def statistics_entries(self,start:datetime,end:datetime):
        return self.s.execute(select(ParkingRecord.entry_time).where(
            ParkingRecord.entry_time>=start,ParkingRecord.entry_time<end,
            ParkingRecord.temporary==False)).all()

    def statistics_exits(self,start:datetime,end:datetime):
        return self.s.execute(select(ParkingRecord.entry_time,ParkingRecord.exit_time,
            ParkingRecord.temporary).where(ParkingRecord.exit_time>=start,
            ParkingRecord.exit_time<end)).all()

    def statistics_intervals(self,start:datetime,end:datetime):
        return self.s.execute(select(ParkingRecord.entry_time,ParkingRecord.exit_time).where(
            ParkingRecord.entry_time<end,ParkingRecord.temporary==False,
            or_(ParkingRecord.exit_time.is_(None),ParkingRecord.exit_time>=start))).all()

    def statistics_exit_summary(self, start: datetime, end: datetime) -> dict:
        """P1-13：出口次数 + 平均时长（SQL 聚合，避免把 100k 行逐行取回 Python）。

        语义与 ``len(statistics_exits(...))`` / ``StatisticsService._avg_minutes`` 完全一致：
        - count 为 exit_time ∈ [start,end) 的全部记录（含 temporary）；
        - 平均时长只统计非 temporary 且已出场的记录，逐条按 ``minutes_between`` 的
          “向上取整、<=0 记 0” 规则计算，最后 sum/count。
        """
        # 等价于 Python minutes_between：先取整秒（小数秒丢弃），再按 (secs+59)//60 向上取整，
        # <=0 记 0。用 unixepoch(subsec) 的文本解析避免浮点误差；旧 SQLite 退回 julianday。
        if _supports_unixepoch_subsec(self.s):
            seconds = (func.unixepoch(ParkingRecord.exit_time, "subsec")
                       - func.unixepoch(ParkingRecord.entry_time, "subsec"))
        else:
            seconds = ((func.julianday(ParkingRecord.exit_time)
                        - func.julianday(ParkingRecord.entry_time)) * 86400.0)
        secs_int = cast(seconds, Integer)
        minutes = case((secs_int <= 0, 0),
                       else_=cast((secs_int + 59) / 60, Integer))
        counted = case((ParkingRecord.temporary == False, 1), else_=0)  # noqa: E712
        summed = case((ParkingRecord.temporary == False, minutes), else_=0)  # noqa: E712
        stmt = select(
            func.count(ParkingRecord.id),
            func.coalesce(func.sum(counted), 0),
            func.coalesce(func.sum(summed), 0),
        ).where(ParkingRecord.exit_time >= start, ParkingRecord.exit_time < end)
        count, minutes_count, minutes_sum = self.s.execute(stmt).one()
        return {"count": int(count or 0),
                "minutes_count": int(minutes_count or 0),
                "minutes_sum": int(minutes_sum or 0)}

    def statistics_peak_occupancy(self, start: datetime, end: datetime) -> int:
        """P1-13：峰值在场（有序双路归并，等价于原 Python 排序扫描线）。

        原实现把 100k 条区间取回 Python 并构造 200k 事件后排序（100k 行基准约 0.5s）。
        这里利用覆盖索引 idx_entry_cover / idx_exit_cover 的两条有序索引流做线性归并：
        - 入场早于 start 的在途车按 start 计入初始在场；出场晚于 end/未出场在区间内不减员；
        - temporary 账单不视为在场；同一时刻先出场(-1)再入场(+1)；
        - 零/负时长记录（exit <= entry）整条忽略，与原实现 ex<=en 跳过的语义一致。
        """
        base = (ParkingRecord.temporary == False)  # noqa: E712
        open_or_after = or_(ParkingRecord.exit_time.is_(None),
                            ParkingRecord.exit_time > ParkingRecord.entry_time)
        initial = int(self.s.execute(
            select(func.count()).select_from(ParkingRecord).where(
                ParkingRecord.entry_time < start, base,
                or_(ParkingRecord.exit_time.is_(None),
                    ParkingRecord.exit_time > start))).scalar() or 0)
        entries = [row[0] for row in self.s.execute(
            select(ParkingRecord.entry_time).where(
                ParkingRecord.entry_time >= start, ParkingRecord.entry_time < end,
                base, open_or_after).order_by(ParkingRecord.entry_time)).all()]
        exits = [row[0] for row in self.s.execute(
            select(ParkingRecord.exit_time).where(
                ParkingRecord.exit_time >= start, ParkingRecord.exit_time < end,
                base, open_or_after).order_by(ParkingRecord.exit_time)).all()]
        cur = peak = initial
        i = j = 0
        while i < len(entries) or j < len(exits):
            # 同一时刻先处理出场（-1）再入场（+1），与原排序键 (ts, delta) 一致
            if j < len(exits) and (i >= len(entries) or exits[j] <= entries[i]):
                cur -= 1
                j += 1
            else:
                cur += 1
                if cur > peak:
                    peak = cur
                i += 1
        return peak

    # ----------------------------- N2 报表：完整记录 -----------------------------
    @staticmethod
    def _complete_conditions():
        """只统计完整记录：status=out 且 entry/exit 均非空（异常/aborted 不计）。"""
        return (ParkingRecord.status == "out",
                ParkingRecord.entry_time.is_not(None),
                ParkingRecord.exit_time.is_not(None))

    def complete_by_exit(self, start: datetime, end: datetime) -> list[ParkingRecord]:
        """出场时间落在 [start,end) 的完整记录（报表明细/汇总口径）。"""
        stmt = select(ParkingRecord).where(
            *self._complete_conditions(),
            ParkingRecord.exit_time >= start, ParkingRecord.exit_time < end,
        ).order_by(ParkingRecord.exit_time, ParkingRecord.id)
        return list(self.s.execute(stmt).scalars().all())

    def complete_by_entry(self, start: datetime, end: datetime) -> list[ParkingRecord]:
        """入场时间落在 [start,end) 的完整记录（报表 count_in 口径）。"""
        stmt = select(ParkingRecord).where(
            *self._complete_conditions(),
            ParkingRecord.entry_time >= start, ParkingRecord.entry_time < end,
        ).order_by(ParkingRecord.entry_time, ParkingRecord.id)
        return list(self.s.execute(stmt).scalars().all())

    def complete_overlapping(self, start: datetime, end: datetime) -> list[ParkingRecord]:
        """与 [start,end) 有重叠的完整记录（峰值在场扫描线）。"""
        stmt = select(ParkingRecord).where(
            *self._complete_conditions(),
            ParkingRecord.entry_time < end, ParkingRecord.exit_time > start,
        ).order_by(ParkingRecord.entry_time, ParkingRecord.id)
        return list(self.s.execute(stmt).scalars().all())

    # ----------------------------- 写入 -----------------------------
    def create(self, *, plate: str, plate_color: str, entry_time: datetime,
               space_no: Optional[str], vehicle_id: Optional[str] = None,
               entry_photo: Optional[str] = None, entry_plate_crop: Optional[str] = None,
               manual_entry: bool = False, fee_rule_id: Optional[int] = None,
               benefit_type: Optional[str] = None,
               temporary: bool = False, status: str = "in",
               entry_sim_sec: Optional[float] = None) -> ParkingRecord:
        rec = ParkingRecord(
            plate=plate, plate_color=plate_color, entry_time=entry_time,
            space_no=space_no, vehicle_id=vehicle_id, entry_photo=entry_photo,
            entry_plate_crop=entry_plate_crop, manual_entry=manual_entry,
            fee_rule_id=fee_rule_id, benefit_type=benefit_type,
            temporary=temporary, status=status, created_at=utcnow(),
            entry_sim_sec=entry_sim_sec,
            session_id=self.s.execute(select(DemoSession.id).where(
                DemoSession.status == "active").limit(1)).scalar_one_or_none(),
        )
        self.s.add(rec)
        self.s.flush()
        return rec

    def mark_exit(self, rec: ParkingRecord, *, exit_time: datetime,
                  exit_photo: Optional[str] = None,
                  exit_plate_crop: Optional[str] = None,
                  status: str = "out",
                  exit_sim_sec: Optional[float] = None,
                  duration_sim_sec: Optional[float] = None) -> ParkingRecord:
        rec.exit_time = exit_time
        rec.status = status
        if exit_sim_sec is not None:
            rec.exit_sim_sec = float(exit_sim_sec)
        if duration_sim_sec is not None:
            rec.duration_sim_sec = float(duration_sim_sec)
        if exit_photo:
            rec.exit_photo = exit_photo
        if exit_plate_crop:
            rec.exit_plate_crop = exit_plate_crop
        self.s.flush()
        return rec

    @staticmethod
    def to_view(rec: ParkingRecord) -> dict:
        return {
            "id": rec.id, "temporary": rec.temporary,
            "plate": rec.plate,
            "plate_color": rec.plate_color,
            "vehicle_id": rec.vehicle_id,
            "entry_time": iso_local(rec.entry_time),
            "exit_time": iso_local(rec.exit_time),
            "space_no": rec.space_no,
            "status": rec.status,
            "entry_photo": rec.entry_photo,
            "exit_photo": rec.exit_photo,
            "entry_plate_crop": rec.entry_plate_crop,
            "exit_plate_crop": rec.exit_plate_crop,
            "manual_entry": bool(rec.manual_entry),
            "fee_rule_id": rec.fee_rule_id,
            "benefit_type": rec.benefit_type,
            "session_id": rec.session_id,
            "aborted_reason": rec.aborted_reason,
            "entry_sim_sec": rec.entry_sim_sec,
            "exit_sim_sec": rec.exit_sim_sec,
            "duration_sim_sec": rec.duration_sim_sec,
        }


class PaymentRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def get_by_parking(self, parking_record_id: int) -> Optional[PaymentRecord]:
        stmt = select(PaymentRecord).where(
            PaymentRecord.parking_record_id == parking_record_id).limit(1)
        return self.s.execute(stmt).scalar_one_or_none()

    def by_record_ids(self, record_ids: list[int]) -> dict[int, PaymentRecord]:
        """批量取支付记录（报表用；SQLite 变量上限内分块）。"""
        found: dict[int, PaymentRecord] = {}
        ids = [int(i) for i in record_ids]
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            if not chunk:
                continue
            rows = self.s.execute(select(PaymentRecord).where(
                PaymentRecord.parking_record_id.in_(chunk))).scalars()
            found.update({row.parking_record_id: row for row in rows})
        return found

    def create(self, *, parking_record_id: int, plate: str, minutes: int,
               billable_units: int, amount_due: float, amount_paid: float,
               method: str, operator: Optional[str], breakdown: FeeBreakdown,
               paid_at: Optional[datetime] = None) -> PaymentRecord:
        pay = PaymentRecord(
            parking_record_id=parking_record_id, plate=plate, minutes=minutes,
            billable_units=billable_units, amount_due=amount_due,
            amount_paid=amount_paid, method=method, operator=operator,
            breakdown=breakdown.model_dump_json(), paid_at=paid_at or utcnow(),
            session_id=self.s.execute(select(ParkingRecord.session_id).where(
                ParkingRecord.id == parking_record_id)).scalar_one_or_none(),
        )
        self.s.add(pay)
        self.s.flush()
        return pay

    def revenue_in_range(self, start: datetime, end: datetime) -> float:
        stmt = (select(func.coalesce(func.sum(PaymentRecord.amount_paid), 0.0))
                .where(PaymentRecord.paid_at >= start, PaymentRecord.paid_at < end))
        return float(self.s.execute(stmt).scalar() or 0.0)

    def all_in_range(self, start: datetime, end: datetime) -> list[PaymentRecord]:
        stmt = (select(PaymentRecord)
                .where(PaymentRecord.paid_at >= start, PaymentRecord.paid_at < end))
        return list(self.s.execute(stmt).scalars().all())

    def statistics_day_revenue(self,start,end,offset_minutes):
        day=func.date(PaymentRecord.paid_at,f'{offset_minutes:+d} minutes')
        return dict(self.s.execute(select(day,func.sum(PaymentRecord.amount_paid)).where(
            PaymentRecord.paid_at>=start,PaymentRecord.paid_at<end).group_by(day)).all())

    def statistics_amounts(self,start:datetime,end:datetime):
        return self.s.execute(select(PaymentRecord.paid_at,PaymentRecord.amount_paid).where(
            PaymentRecord.paid_at>=start,PaymentRecord.paid_at<end)).all()

    @staticmethod
    def breakdown_of(pay: PaymentRecord) -> Optional[dict]:
        if not pay.breakdown:
            return None
        try:
            return json.loads(pay.breakdown)
        except json.JSONDecodeError:
            return None
