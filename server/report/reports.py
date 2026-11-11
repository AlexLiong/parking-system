"""N2 报表服务（report/reports.py）。

只统计完整记录：``status='out' AND entry_time IS NOT NULL AND exit_time IS NOT NULL``
（黑名单 aborted/异常不计）。按周期生成管理端预览与 CSV：

- day：level=vehicle_day，by_day=[1 天]，每天带 vehicles 明细；
- week：level=day，by_day=7 天，每天带 vehicles；
- month：level=week，weeks=周汇总（无车辆）；
- quarter：level=month，months=3 个月汇总（无车辆）；
- year：level=month，months=12 个月汇总（无车辆）。
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from ..core.timeutil import (date_range, iso_local, minutes_between,
                             to_local, utcnow)
from ..repository.parking_repo import ParkingRecordRepo, PaymentRepo

log = logging.getLogger("report.reports")

PERIODS = ("day", "week", "month", "quarter", "year")
LEVELS = {"day": "vehicle_day", "week": "day",
          "month": "week", "quarter": "month", "year": "month"}
MAX_VEHICLES = 2000

# 展示层翻译（仅 PDF/CSV；preview 数据保持 raw，前端已有自己的翻译）
PAYMENT_METHOD_LABELS = {
    "auto": "自动扣费",
    "mock_scan": "扫码支付",
    "free": "免费",
    "monthly": "月租",
    "cash": "现金",
}
PAYMENT_METHOD_LABELS_EN = {
    "auto": "Auto-paid",
    "mock_scan": "Scan pay",
    "free": "Free",
    "monthly": "Monthly",
    "cash": "Cash",
}
# 文件名语言后缀：中文 _cn、英文 _en
LANG_SUFFIX = {"zh": "cn", "en": "en"}


def payment_method_label(value, lang: str = "zh") -> str:
    """支付方式展示文案：已知值按语言翻译，未知值原样返回。"""
    text = str(value or "").strip()
    table = PAYMENT_METHOD_LABELS_EN if lang == "en" else PAYMENT_METHOD_LABELS
    return table.get(text, text)


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
        raise ValueError("anchor 应为 YYYY-MM-DD") from None


class ReportService:
    """报表预览/CSV（只读，不修改数据）。"""

    def __init__(self, parking_repo: ParkingRecordRepo, payment_repo: PaymentRepo):
        self.parking = parking_repo
        self.payment = payment_repo

    # ----------------------------- 预览 -----------------------------
    def preview(self, period: str, anchor=None,
                vehicle_limit: int = 500) -> dict:
        if period not in PERIODS:
            raise ValueError("period 应为 day/week/month/quarter/year")
        parsed = _parse_anchor(anchor)
        limit = max(1, min(int(vehicle_limit or 500), MAX_VEHICLES))
        start, end = date_range(period, parsed)
        local_start, local_end = to_local(start), to_local(end)
        if period in ("day", "week"):
            result = self._vehicle_preview(period, parsed, start, end,
                                           local_start, local_end, limit)
        elif period == "month":
            result = self._weeks_preview(period, parsed, start, end,
                                         local_start, local_end)
        else:  # quarter / year
            result = self._months_preview(period, parsed, start, end,
                                          local_start, local_end)
        result["generated_at"] = iso_local(utcnow())
        return result

    # ----------------------------- 内部：数据/汇总 -----------------------------
    def _complete_data(self, start: datetime, end: datetime):
        records = self.parking.complete_by_exit(start, end)
        payments = self.payment.by_record_ids([r.id for r in records])
        vehicles = []
        for rec in records:
            pay = payments.get(rec.id)
            vehicles.append({
                "record_id": rec.id,
                "plate": rec.plate,
                "entry_time": iso_local(rec.entry_time),
                "exit_time": iso_local(rec.exit_time),
                "duration_minutes": minutes_between(rec.entry_time, rec.exit_time),
                "amount": round(float(pay.amount_paid or 0.0) if pay else 0.0, 2),
                "payment_method": (pay.method if pay else None),
                "space_no": rec.space_no,
                "_entry_dt": to_local(rec.entry_time),
                "_exit_dt": to_local(rec.exit_time),
            })
        entries = self.parking.complete_by_entry(start, end)
        overlapping = self.parking.complete_overlapping(start, end)
        peak = self._peak(overlapping, start, end)
        revenue = round(sum(v["amount"] for v in vehicles), 2)
        avg = (round(sum(v["duration_minutes"] for v in vehicles) / len(vehicles), 2)
               if vehicles else 0.0)
        summary = {
            "count_in": len(entries),
            "count_out": len(vehicles),
            "revenue": revenue,
            "avg_minutes": avg,
            "peak_occupancy": peak,
        }
        return summary, vehicles, entries

    @staticmethod
    def _peak(records, start: datetime, end: datetime) -> int:
        events: list[tuple[datetime, int]] = []
        for rec in records:
            entry = max(rec.entry_time, start)
            exit_ = min(rec.exit_time or end, end)
            if exit_ <= entry:
                continue
            events.append((entry, 1))
            events.append((exit_, -1))
        events.sort(key=lambda item: (item[0], item[1]))
        current = peak = 0
        for _, delta in events:
            current += delta
            if current > peak:
                peak = current
        return peak

    @staticmethod
    def _base(period, anchor, start, end, local_start, local_end, summary, level):
        return {
            "period": period,
            "anchor": (anchor or local_start.date()).isoformat(),
            "start": iso_local(start),
            "end": iso_local(end),
            "level": level,
            "summary": summary,
            "truncated": False,
        }

    def _vehicle_preview(self, period, anchor, start, end,
                         local_start, local_end, limit) -> dict:
        summary, vehicles, entries = self._complete_data(start, end)
        base = self._base(period, anchor, start, end, local_start, local_end,
                          summary, LEVELS[period])
        days = []
        if period == "day":
            day_items = [day for day in (local_start.date(),)]
        else:
            day_items = [local_start.date() + timedelta(days=offset)
                         for offset in range(7)]
        entries_by_day: dict[str, int] = {}
        for rec in entries:
            key = to_local(rec.entry_time).date().isoformat()
            entries_by_day[key] = entries_by_day.get(key, 0) + 1
        truncated = False
        for day in day_items:
            key = day.isoformat()
            day_records = [v for v in vehicles if v["_exit_dt"].date() == day]
            shown = [self._public_vehicle(v) for v in day_records]
            if len(shown) > limit:
                shown = shown[:limit]
                truncated = True
            days.append({
                "date": key,
                "count_in": entries_by_day.get(key, 0),
                "count_out": len(day_records),
                # 汇总/营收以全部完整记录为准；limit 仅截断 vehicles 展示
                "revenue": round(sum(v["amount"] for v in day_records), 2),
                "vehicles": shown,
            })
        base["by_day"] = days
        base["truncated"] = truncated
        return base

    def _weeks_preview(self, period, anchor, start, end,
                       local_start, local_end) -> dict:
        summary, vehicles, entries = self._complete_data(start, end)
        base = self._base(period, anchor, start, end, local_start, local_end,
                          summary, LEVELS[period])
        weeks = []
        cursor = local_start.date()
        last = local_end.date()
        while cursor < last:
            monday = cursor - timedelta(days=cursor.weekday())
            sunday = monday + timedelta(days=6)
            week_start = max(monday, local_start.date())
            week_end = min(sunday, last - timedelta(days=1))
            bucket_in = [r for r in entries
                         if week_start <= to_local(r.entry_time).date() <= week_end]
            bucket_out = [v for v in vehicles
                          if week_start <= v["_exit_dt"].date() <= week_end]
            weeks.append({
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "count_in": len(bucket_in),
                "count_out": len(bucket_out),
                "revenue": round(sum(v["amount"] for v in bucket_out), 2),
            })
            cursor = sunday + timedelta(days=1)
        base["weeks"] = weeks
        return base

    def _months_preview(self, period, anchor, start, end,
                        local_start, local_end) -> dict:
        summary, vehicles, entries = self._complete_data(start, end)
        base = self._base(period, anchor, start, end, local_start, local_end,
                          summary, LEVELS[period])
        months = []
        cursor = local_start.date().replace(day=1)
        last = local_end.date()
        while cursor < last:
            next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_end = min(next_month - timedelta(days=1), last - timedelta(days=1))
            bucket_in = [r for r in entries
                         if cursor <= to_local(r.entry_time).date() <= month_end]
            bucket_out = [v for v in vehicles
                          if cursor <= v["_exit_dt"].date() <= month_end]
            months.append({
                "month": cursor.strftime("%Y-%m"),
                "count_in": len(bucket_in),
                "count_out": len(bucket_out),
                "revenue": round(sum(v["amount"] for v in bucket_out), 2),
            })
            cursor = next_month
        base["months"] = months
        return base

    @staticmethod
    def _public_vehicle(v: dict) -> dict:
        return {k: v[k] for k in ("plate", "entry_time", "exit_time",
                                  "duration_minutes", "amount",
                                  "payment_method", "space_no")}

    # ----------------------------- CSV -----------------------------
    HEADERS = {
        "zh": {
            "day": ["车牌", "入场时间", "出场时间", "停放分钟", "金额(元)",
                    "支付方式", "车位号"],
            "week": ["日期", "车牌", "入场时间", "出场时间", "停放分钟", "金额(元)",
                     "支付方式", "车位号"],
            "month": ["周起始", "周结束", "进场车次", "出场车次", "营收(元)"],
            # M1：季度数据在 months（3 个月汇总），表头用月份
            "quarter": ["月份", "进场车次", "出场车次", "营收(元)"],
            "year": ["月份", "进场车次", "出场车次", "营收(元)"],
        },
        "en": {
            "day": ["Plate", "Entry time", "Exit time", "Duration (min)",
                    "Amount (CNY)", "Payment method", "Space no."],
            "week": ["Date", "Plate", "Entry time", "Exit time",
                     "Duration (min)", "Amount (CNY)", "Payment method",
                     "Space no."],
            "month": ["Week start", "Week end", "Entries", "Exits",
                      "Revenue (CNY)"],
            "quarter": ["Month", "Entries", "Exits", "Revenue (CNY)"],
            "year": ["Month", "Entries", "Exits", "Revenue (CNY)"],
        },
    }

    def csv_text(self, preview: dict, lang: str = "zh") -> str:
        period = preview["period"]
        lang = "en" if lang == "en" else "zh"
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(self.HEADERS[lang][period])
        if period == "day":
            for day in preview.get("by_day", []):
                for v in day.get("vehicles", []):
                    writer.writerow([v["plate"], v["entry_time"], v["exit_time"],
                                     v["duration_minutes"], f"{v['amount']:.2f}",
                                     payment_method_label(v["payment_method"], lang),
                                     v["space_no"] or ""])
        elif period == "week":
            for day in preview.get("by_day", []):
                for v in day.get("vehicles", []):
                    writer.writerow([day["date"], v["plate"], v["entry_time"],
                                     v["exit_time"], v["duration_minutes"],
                                     f"{v['amount']:.2f}",
                                     payment_method_label(v["payment_method"], lang),
                                     v["space_no"] or ""])
        elif period == "month":
            for week in preview.get("weeks", []):
                writer.writerow([week["week_start"], week["week_end"],
                                 week["count_in"], week["count_out"],
                                 f"{week['revenue']:.2f}"])
        elif period == "quarter":
            # M1：季度为 3 个月汇总（preview.months），不是 weeks
            for month in preview.get("months", []):
                writer.writerow([month["month"], month["count_in"],
                                 month["count_out"], f"{month['revenue']:.2f}"])
        else:
            for month in preview.get("months", []):
                writer.writerow([month["month"], month["count_in"],
                                 month["count_out"], f"{month['revenue']:.2f}"])
        return buf.getvalue()

    @staticmethod
    def filename(period: str, preview: dict, lang: str = "zh",
                 ext: str = "csv") -> str:
        """文件名语言后缀：中文 _cn、英文 _en（默认 zh 保持旧行为+_cn）。"""
        suffix = LANG_SUFFIX.get("en" if lang == "en" else "zh", "cn")
        return f"report_{period}_{preview['anchor']}_{suffix}.{ext}"
