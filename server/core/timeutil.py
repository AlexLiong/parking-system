"""Store naive UTC; convert at API and business-calendar boundaries."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import get_config


def business_zone() -> ZoneInfo:
    return ZoneInfo(get_config().server.timezone)


def utcnow() -> datetime:
    """当前 UTC 时间（naive，去掉 tzinfo）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """把任意 datetime 归一化为 naive UTC。 naive 视为已是 UTC。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Interpret stored naive values as UTC and return an aware business-local time."""
    if dt is None:
        return None
    value = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return value.astimezone(business_zone())


def iso_local(dt: Optional[datetime]) -> Optional[str]:
    value = to_local(dt)
    return value.isoformat() if value else None


def parse_local_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse API input; naive strings use the configured business timezone."""
    if not s:
        return None
    try:
        value = datetime.fromisoformat(s)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=business_zone())
    return to_utc(value)


def from_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return to_utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def minutes_between(start: datetime, end: datetime) -> int:
    """两个时间点之间的分钟数（向上取整，负数按 0）。"""
    delta = (to_utc(end) - to_utc(start))
    secs = delta.total_seconds()
    if secs <= 0:
        return 0
    return int((secs + 59) // 60)  # ceil


def date_range(period: str, anchor: Optional[date] = None) -> tuple[datetime, datetime]:
    """Return UTC query boundaries for a business-local calendar period."""
    anchor = anchor or to_local(utcnow()).date()
    if period == "day":
        start_d = anchor
        end_d = start_d + timedelta(days=1)
    elif period == "week":
        start_d = anchor - timedelta(days=anchor.weekday())  # 周一
        end_d = start_d + timedelta(days=7)
    elif period == "month":
        start_d = date(anchor.year, anchor.month, 1)
        end_d = (start_d + timedelta(days=32)).replace(day=1)
    elif period == "quarter":
        q = (anchor.month - 1) // 3
        start_d = date(anchor.year, q * 3 + 1, 1)
        end_month = q * 3 + 4
        end_d = date(anchor.year + (1 if end_month > 12 else 0),
                       ((end_month - 1) % 12) + 1, 1)
    elif period == "year":
        start_d = date(anchor.year, 1, 1)
        end_d = date(anchor.year + 1, 1, 1)
    else:
        raise ValueError(f"未知统计周期: {period}")
    zone = business_zone()
    return (to_utc(datetime.combine(start_d, datetime.min.time(), zone)),
            to_utc(datetime.combine(end_d, datetime.min.time(), zone)))


def iter_days(start: datetime, end: datetime):
    """逐日迭代 [start, end)。"""
    cur = start
    while cur < end:
        yield cur
        cur += timedelta(days=1)
