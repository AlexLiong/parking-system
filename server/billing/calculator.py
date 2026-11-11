"""计费算法（billing/calculator.py）。

对应设计文档 §3.4 与 FR-5。纯函数式计算器：输入（在场记录 + 出场时间 + 费率
快照）输出 ``FeeBreakdown``，不碰数据库、不发事件，便于确定性单测。

计费规则（全部可配置，取值来自**进场时点生效的 FeeRule 快照**，FR-5.3）：
1. ``total_minutes = ceil((exit - entry)/60s)``（负数按 0）；
2. 免费时长：``total ≤ free_minutes`` → 0 元；否则扣除免费额度后计费；
3. 阶梯：首个小时（60 分钟）按 ``first_hour_price`` 一口价；其后每
   ``unit_minutes`` 收 ``unit_price``，不足一单位按 ``round_up`` 决定是否进位；
4. 单日封顶：每自然日封顶 ``daily_cap``，跨天按自然日数累计封顶；
5. 新能源折扣：绿牌乘 ``ne_discount``（默认 0.9，设 1.0 关闭）。

金额统一四舍五入到分（2 位小数）。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional, Union

from ..core.domain import FeeBreakdown
from ..core.timeutil import minutes_between, to_utc

# rule 可为 ORM models.FeeRule 或 domain.FeeRule（字段同名，鸭子类型）
RuleLike = Union["object"]


def _round2(x: float) -> float:
    # 半进位到分，规避浮点误差
    return math.floor(x * 100 + 0.5) / 100


class FeeCalculator:
    """按费率快照计算停车费。无状态，可安全共享。"""

    def compute(self, record, exit_time: datetime, rule) -> FeeBreakdown:
        entry = to_utc(record.entry_time)
        exit_ = to_utc(exit_time)
        total_minutes = minutes_between(entry, exit_)
        color = (getattr(record, "plate_color", "blue") or "blue").lower()
        benefit = getattr(record, "benefit_type", None)

        if benefit in ("white", "monthly"):
            label = "白名单" if benefit == "white" else "月租"
            return FeeBreakdown(
                minutes=total_minutes, free_minutes_applied=total_minutes,
                billable_minutes=0, billable_units=0, amount_due=0.0,
                detail=f"停放 {total_minutes} 分钟，进场时有效{label}，本次免费")

        free_minutes = int(getattr(rule, "free_minutes", 0) or 0)
        first_hour_price = float(getattr(rule, "first_hour_price", 0.0) or 0.0)
        unit_minutes = int(getattr(rule, "unit_minutes", 30) or 30)
        unit_price = float(getattr(rule, "unit_price", 0.0) or 0.0)
        daily_cap = float(getattr(rule, "daily_cap", 0.0) or 0.0)
        ne_discount = float(getattr(rule, "ne_discount", 1.0) or 1.0)
        round_up = bool(getattr(rule, "round_up", True))

        # 1) 免费额度
        if total_minutes <= free_minutes:
            return FeeBreakdown(
                minutes=total_minutes, free_minutes_applied=total_minutes,
                billable_minutes=0, billable_units=0, unit_price=unit_price,
                first_hour_price=first_hour_price, daily_cap_applied=False,
                new_energy_discount=1.0, amount_due=0.0,
                detail=f"停放 {total_minutes} 分钟 ≤ 免费 {free_minutes} 分钟，免费放行")

        free_applied = free_minutes
        billable_minutes = max(0, total_minutes - free_applied)

        # 2) 阶梯计费（首小时一口价 + 之后按单位）
        gross, units = self._tiered(billable_minutes, first_hour_price,
                                    unit_minutes, unit_price, round_up)

        # 3) 单日封顶（跨自然日累计）
        cap_applied = False
        if daily_cap and daily_cap > 0:
            n_days = self._natural_days(entry, exit_)
            cap_total = daily_cap * n_days
            if gross > cap_total:
                gross = cap_total
                cap_applied = True

        # 4) 新能源折扣
        discount = ne_discount if color == "green" else 1.0
        amount = _round2(gross * discount)

        detail = self._detail(total_minutes, free_applied, billable_minutes, units,
                              first_hour_price, unit_minutes, unit_price,
                              cap_applied, daily_cap, discount, amount)
        return FeeBreakdown(
            minutes=total_minutes, free_minutes_applied=free_applied,
            billable_minutes=billable_minutes, billable_units=units,
            unit_price=unit_price, first_hour_price=first_hour_price,
            daily_cap_applied=cap_applied, new_energy_discount=discount,
            amount_due=amount, detail=detail)

    # ----------------------------- 内部 -----------------------------
    @staticmethod
    def _tiered(billable_minutes: int, first_hour_price: float,
                unit_minutes: int, unit_price: float,
                round_up: bool) -> tuple[float, int]:
        """返回 (未封顶未打折金额, 计费单位数)。"""
        if billable_minutes <= 0:
            return 0.0, 0
        if billable_minutes <= 60:
            return first_hour_price, 0
        amount = first_hour_price
        remaining = billable_minutes - 60
        if unit_minutes <= 0:
            return amount, 0
        if round_up:
            units = math.ceil(remaining / unit_minutes)
        else:
            units = remaining // unit_minutes
        amount += units * unit_price
        return amount, int(units)

    @staticmethod
    def _natural_days(entry: Optional[datetime], exit_: Optional[datetime]) -> int:
        """每日封顶的自然日天数（H2：固定按 UTC+8 业务日，不做多时区）。"""
        if entry is None or exit_ is None:
            return 1
        offset = timedelta(hours=8)          # 固定 UTC+8（与系统展示/统计口径一致）
        span = ((exit_ + offset).date() - (entry + offset).date()).days + 1
        return max(1, span)

    @staticmethod
    def _detail(total, free_applied, billable, units, fhp, um, up,
                cap_applied, cap, discount, amount) -> str:
        parts = [f"停放 {total} 分钟"]
        if free_applied:
            parts.append(f"免费 {free_applied} 分钟")
        parts.append(f"计费 {billable} 分钟")
        parts.append(f"首小时 {fhp:.2f} 元")
        if units:
            parts.append(f"其后 {units}×{um}分钟×{up:.2f} 元")
        if cap_applied:
            parts.append(f"触发单日封顶 {cap:.2f} 元/天")
        if discount != 1.0:
            parts.append(f"新能源 {discount*10:.1f} 折")
        parts.append(f"应收 {amount:.2f} 元")
        return "，".join(parts)
