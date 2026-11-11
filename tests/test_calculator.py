"""计费算法单元测试（billing/calculator.py）。

覆盖：免费时长、首小时一口价、阶梯计费、进位、单日封顶（跨自然日）、新能源折扣。
FeeCalculator 为纯函数式，用 SimpleNamespace 充当记录/费率即可，无需数据库。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

from server.billing.calculator import FeeCalculator

calc = FeeCalculator()


def _rule(**over):
    base = dict(free_minutes=15, first_hour_price=5.0, unit_minutes=30,
                unit_price=2.0, daily_cap=40.0, ne_discount=0.9, round_up=True)
    base.update(over)
    return NS(**base)


def _rec(entry, color="blue"):
    return NS(entry_time=entry, plate_color=color)


def test_within_free_minutes():
    entry = datetime(2026, 1, 1, 10, 0)
    bd = calc.compute(_rec(entry), entry.replace(minute=10), _rule())  # 10 分钟
    assert bd.minutes == 10
    assert bd.amount_due == 0.0
    assert bd.free_minutes_applied == 10


def test_first_hour_flat_price():
    entry = datetime(2026, 1, 1, 10, 0)
    exit_ = datetime(2026, 1, 1, 11, 0)   # 60 分钟
    bd = calc.compute(_rec(entry), exit_, _rule())
    # billable = 60 - 15 = 45 ≤ 60 → 首小时一口价
    assert bd.billable_minutes == 45
    assert bd.amount_due == 5.0


def test_tiered_units_with_round_up():
    entry = datetime(2026, 1, 1, 10, 0)
    exit_ = datetime(2026, 1, 1, 11, 30)  # 90 分钟
    bd = calc.compute(_rec(entry), exit_, _rule())
    # billable = 75；首小时 5 + 剩 15 分钟进位 1 单位 ×2 = 7
    assert bd.billable_minutes == 75
    assert bd.billable_units == 1
    assert bd.amount_due == 7.0


def test_round_up_disabled():
    entry = datetime(2026, 1, 1, 10, 0)
    exit_ = datetime(2026, 1, 1, 11, 30)  # billable 75 → 剩 15 分钟
    bd = calc.compute(_rec(entry), exit_, _rule(round_up=False))
    # 不进位：15 // 30 = 0 单位 → 仅首小时
    assert bd.billable_units == 0
    assert bd.amount_due == 5.0


def test_two_units():
    entry = datetime(2026, 1, 1, 10, 0)
    exit_ = datetime(2026, 1, 1, 12, 0)   # 120 分钟
    bd = calc.compute(_rec(entry), exit_, _rule())
    # billable 105 → 5 + ceil(45/30)=2 ×2 = 9
    assert bd.billable_units == 2
    assert bd.amount_due == 9.0


def test_new_energy_discount():
    entry = datetime(2026, 1, 1, 10, 0)
    exit_ = datetime(2026, 1, 1, 11, 30)  # 应收 7.0
    bd = calc.compute(_rec(entry, "green"), exit_, _rule())
    assert bd.new_energy_discount == 0.9
    assert bd.amount_due == round(7.0 * 0.9, 2)  # 6.3


def test_daily_cap_across_natural_days():
    entry = datetime(2026, 1, 1, 0, 0)
    exit_ = datetime(2026, 1, 2, 12, 0)   # 36 小时，跨 2 个自然日
    bd = calc.compute(_rec(entry), exit_, _rule())
    # 2 天封顶 = 80；未封顶毛额远大于 80
    assert bd.daily_cap_applied is True
    assert bd.amount_due == 80.0


def test_zero_duration():
    entry = datetime(2026, 1, 1, 10, 0)
    bd = calc.compute(_rec(entry), entry, _rule())
    assert bd.minutes == 0
    assert bd.amount_due == 0.0


def test_negative_duration_clamped():
    entry = datetime(2026, 1, 1, 10, 0)
    bd = calc.compute(_rec(entry), datetime(2026, 1, 1, 9, 0), _rule())
    assert bd.minutes == 0
    assert bd.amount_due == 0.0


def test_discount_disabled_when_one():
    entry = datetime(2026, 1, 1, 10, 0)
    exit_ = datetime(2026, 1, 1, 11, 30)
    bd = calc.compute(_rec(entry, "green"), exit_, _rule(ne_discount=1.0))
    assert bd.amount_due == 7.0
