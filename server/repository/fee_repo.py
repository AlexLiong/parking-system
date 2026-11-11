"""费率仓储（repository/fee_repo.py）——支持按时间点取快照（FR-5.3）。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select

from ..core.domain import FeeRule as FeeRuleModel
from ..core.timeutil import iso_local, to_utc, utcnow
from ..repository.db import Database
from ..repository.models import FeeRule


class FeeRuleRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def current_rule(self, at: Optional[datetime] = None) -> Optional[FeeRule]:
        """取在 ``at`` 时点生效的费率（effective_from <= at 的最新一条）。"""
        at = to_utc(at) or utcnow()
        stmt = (select(FeeRule).where(FeeRule.effective_from <= at)
                .order_by(FeeRule.effective_from.desc(), FeeRule.id.desc()).limit(1))
        rule = self.s.execute(stmt).scalar_one_or_none()
        if rule is None:
            # 兜底：取最早的一条
            stmt = select(FeeRule).order_by(FeeRule.effective_from, FeeRule.id).limit(1)
            rule = self.s.execute(stmt).scalar_one_or_none()
        return rule

    def get(self, rule_id: int) -> Optional[FeeRule]:
        return self.s.get(FeeRule, rule_id)

    def list_rules(self) -> list[FeeRule]:
        stmt = select(FeeRule).order_by(FeeRule.effective_from.desc(), FeeRule.id.desc())
        return list(self.s.execute(stmt).scalars().all())

    def create(self, *, free_minutes: int, first_hour_price: float, unit_minutes: int,
               unit_price: float, daily_cap: float, ne_discount: float, round_up: bool,
               operator: Optional[str] = None, note: Optional[str] = None,
               effective_from: Optional[datetime] = None) -> FeeRule:
        rule = FeeRule(
            effective_from=to_utc(effective_from) or utcnow(),
            created_at=utcnow(),
            free_minutes=free_minutes, first_hour_price=first_hour_price,
            unit_minutes=unit_minutes, unit_price=unit_price, daily_cap=daily_cap,
            ne_discount=ne_discount, round_up=round_up, operator=operator, note=note,
        )
        self.s.add(rule)
        self.s.flush()
        return rule

    @staticmethod
    def to_domain(rule: FeeRule) -> FeeRuleModel:
        return FeeRuleModel(
            id=rule.id, effective_from=rule.effective_from,
            free_minutes=rule.free_minutes, first_hour_price=rule.first_hour_price,
            unit_minutes=rule.unit_minutes, unit_price=rule.unit_price,
            daily_cap=rule.daily_cap, ne_discount=rule.ne_discount,
            round_up=bool(rule.round_up), operator=rule.operator, note=rule.note,
        )

    @staticmethod
    def to_view(rule: FeeRule, effective_to: Optional[datetime] = None) -> dict:
        """费率版本视图（N2：含 effective_to/created_at，供管理端对比历史）。"""
        created = getattr(rule, "created_at", None) or rule.effective_from
        return {
            "id": rule.id,
            "effective_from": iso_local(rule.effective_from),
            "effective_to": iso_local(effective_to) if effective_to else None,
            "created_at": iso_local(created),
            "free_minutes": rule.free_minutes, "first_hour_price": rule.first_hour_price,
            "unit_minutes": rule.unit_minutes, "unit_price": rule.unit_price,
            "daily_cap": rule.daily_cap, "ne_discount": rule.ne_discount,
            "round_up": bool(rule.round_up), "operator": rule.operator, "note": rule.note,
        }

    def list_views(self) -> list[dict]:
        """全部历史版本（最新在前），每版带 effective_to=下一版生效时间或 null。"""
        rules = list(self.s.execute(select(FeeRule).order_by(
            FeeRule.effective_from, FeeRule.id)).scalars())
        views = [self.to_view(rule, rules[i + 1].effective_from if i + 1 < len(rules) else None)
                 for i, rule in enumerate(rules)]
        return list(reversed(views))

    def current_view(self, at: Optional[datetime] = None) -> Optional[dict]:
        rule = self.current_rule(at)
        if rule is None:
            return None
        next_from = self.s.execute(select(FeeRule.effective_from).where(
            FeeRule.effective_from > rule.effective_from).order_by(
            FeeRule.effective_from).limit(1)).scalar_one_or_none()
        return self.to_view(rule, next_from)
