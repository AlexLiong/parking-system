"""统计服务（report/statistics.py）。

对应设计文档 §3.5 与 FR-6。按周期（天/周/月/季/年）统计车次、营收、平均时长、
在场峰值，并提供明细分页查询。所有时间以 naive UTC 计算（core/timeutil）。

只读服务：仅通过 Repository 读取，不改数据、不发事件（低耦合）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from ..core.domain import DayStat, PeriodStats
from ..core.timeutil import business_zone, date_range, minutes_between, to_local, utcnow
from ..repository.parking_repo import ParkingRecordRepo, PaymentRepo

log = logging.getLogger("report")


def _parse_anchor(anchor) -> Optional[date]:
    if anchor is None:
        return None
    if isinstance(anchor, datetime):
        return anchor.date()
    if isinstance(anchor, date):
        return anchor
    s = str(anchor).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        log.warning("无法解析 anchor=%r，使用当前日期", anchor)
        return None


class StatisticsService:
    def __init__(self, parking_repo: ParkingRecordRepo, payment_repo: PaymentRepo):
        self.parking = parking_repo
        self.payment = payment_repo

    def stats(self, period: str, anchor=None) -> PeriodStats:
        start, end = date_range(period, _parse_anchor(anchor))
        # Modern mainland business calendar has a fixed UTC offset. Other zones
        # retain exact ZoneInfo conversion, including DST changes within a year.
        fixed = business_zone().key in ('UTC','Etc/UTC','Asia/Shanghai','Asia/Chongqing','Asia/Hong_Kong') and start.year>=1993
        entries = [] if fixed else self.parking.statistics_entries(start, end)     # 按进场时间
        revenue = self.payment.revenue_in_range(start, end)

        # P1-13：固定时区（大陆业务时区）走 SQL 聚合，避免 100k 行级基准把全部出场记录
        # 取回 Python 再算 count/avg；非固定时区保留原 Python 路径（输出语义一致）。
        if fixed:
            count_in = self.parking.statistics_entry_count(start,end)
            summary = self.parking.statistics_exit_summary(start, end)
            count_out = summary["count"]
            avg_minutes = ((summary["minutes_sum"] / summary["minutes_count"])
                           if summary["minutes_count"] else 0.0)
            exits = None
        else:
            exits = self.parking.statistics_exits(start, end)         # 按出场时间
            count_in = len(entries)
            count_out = len(exits)
            avg_minutes = self._avg_minutes(exits)
        peak = self.parking.statistics_peak_occupancy(start, end)
        by_day = self._by_day_sql(start,end) if fixed else self._by_day(start, end, entries, exits)

        return PeriodStats(
            period=period,
            anchor=(_parse_anchor(anchor) or to_local(start).date()).isoformat(),
            start=start, end=end,
            count_in=count_in, count_out=count_out,
            revenue=round(revenue, 2), avg_minutes=round(avg_minutes, 2),
            peak_occupancy=peak, by_day=by_day, generated_at=utcnow())

    def search_records(self, plate: Optional[str] = None,
                       start: Optional[datetime] = None,
                       end: Optional[datetime] = None,
                       status: Optional[str] = None,
                       page: int = 1, page_size: int = 20) -> dict:
        """明细分页查询（FR-6.2）：按车牌/时间范围/状态筛选。"""
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 20)))
        rows, total = self.parking.search(
            plate=plate, start=start, end=end, status=status,
            page=page, page_size=page_size)
        return {
            "items": [ParkingRecordRepo.to_view(r) for r in rows],
            "total": total, "page": page, "page_size": page_size,
        }

    # ----------------------------- 内部 -----------------------------
    @staticmethod
    def _avg_minutes(exits) -> float:
        vals = [minutes_between(r.entry_time, r.exit_time)
                for r in exits if r.exit_time is not None and not r.temporary]
        return (sum(vals) / len(vals)) if vals else 0.0

    def _peak_occupancy(self, start: datetime, end: datetime) -> int:
        """扫描线求 [start,end) 内最大同时在场数。"""
        recs = self.parking.statistics_intervals(start, end)
        events: list[tuple[datetime, int]] = []
        for r in recs:
            en = max(r.entry_time, start)
            ex = min(r.exit_time or end, end)
            if ex <= en:
                continue
            events.append((en, 1))
            events.append((ex, -1))
        # 同一时刻先处理离场(-1)再处理进场(+1)，避免边界重复计数
        events.sort(key=lambda t: (t[0], t[1]))
        cur = peak = 0
        for _, d in events:
            cur += d
            if cur > peak:
                peak = cur
        return peak

    def _by_day_sql(self,start,end):
        offset=int(to_local(start).utcoffset().total_seconds()//60)
        incoming,outgoing=self.parking.statistics_day_counts(start,end,offset)
        revenue=self.payment.statistics_day_revenue(start,end,offset)
        result=[];day=to_local(start).date();last=to_local(end).date()
        while day<last:
            key=day.isoformat()
            result.append(DayStat(date=key,count_in=incoming.get(key,0),count_out=outgoing.get(key,0),revenue=round(revenue.get(key,0),2)))
            day+=timedelta(days=1)
        return result

    def _by_day(self, start, end, entries, exits) -> list[DayStat]:
        in_by_day: dict[date, int] = {}
        out_by_day: dict[date, int] = {}
        for r in entries:
            day = to_local(r.entry_time).date()
            in_by_day[day] = in_by_day.get(day, 0) + 1
        for r in exits:
            if r.exit_time:
                day = to_local(r.exit_time).date()
                out_by_day[day] = out_by_day.get(day, 0) + 1
        rev_by_day: dict[date, float] = {}
        for p in self.payment.statistics_amounts(start, end):
            d = to_local(p.paid_at).date()
            rev_by_day[d] = rev_by_day.get(d, 0.0) + float(p.amount_paid or 0.0)

        out: list[DayStat] = []
        d, end_d = to_local(start).date(), to_local(end).date()
        while d < end_d:
            out.append(DayStat(
                date=d.isoformat(),
                count_in=in_by_day.get(d, 0),
                count_out=out_by_day.get(d, 0),
                revenue=round(rev_by_day.get(d, 0.0), 2)))
            d += timedelta(days=1)
        return out
