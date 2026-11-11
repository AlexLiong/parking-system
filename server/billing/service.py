"""计费服务（billing/service.py）。

对外门面，实现 ``IFeeRuleProvider``（供 access 取进场费率快照）。职责：
- 费率版本管理（FR-5.3）：``ensure_default_rule`` / ``add_rule`` / ``list_rules``；
- 出口报价 ``quote`` / ``quote_exit``（时长 + 费用 + 照片，FR-3.2）；
- 缴费 ``pay``：**幂等**（一单一付，PaymentRecord 唯一约束），事务内写收费记录，
  事务内发布关键 PaymentCompleted —— 出场记录状态迁移、车位释放与抬杆由 access
  模块在 PaymentCompleted 处理器里完成；失败整笔回滚，抬杆仅在提交后执行。

费率取值：优先用记录里的 ``fee_rule_id`` 快照；缺失时回退到"进场时点生效"的费率
（FR-5.3：改费率不影响在场车辆）。
"""
from __future__ import annotations

from ..repository.db import synchronized

import logging
import math
from datetime import datetime
from typing import Optional

from ..core.config import AppConfig, get_config
from ..core.domain import FeeRule as FeeRuleDomain
from ..core.domain import Quote
from ..core.errors import NoParkingRecordError, NotFound, ValidationError
from ..core.events import EventBus, PaymentCompleted
from ..core.timeutil import to_utc, utcnow
from ..repository.fee_repo import FeeRuleRepo
from ..repository.models import PaymentRecord
from ..repository.parking_repo import ParkingRecordRepo, PaymentRepo
from .calculator import FeeCalculator

log = logging.getLogger("billing")


class BillingService:
    def __init__(self, *, db, fee_repo: FeeRuleRepo,
                 parking_repo: ParkingRecordRepo, payment_repo: PaymentRepo,
                 bus: EventBus, cfg: Optional[AppConfig] = None,
                 calculator: Optional[FeeCalculator] = None):
        self.db = db
        self.fee_repo = fee_repo
        self.parking = parking_repo
        self.payment = payment_repo
        self.bus = bus
        self.cfg = cfg or get_config()
        self.calc = calculator or FeeCalculator()
        self.simulation = None      # 实现 B：由 Container 注入演示时钟（业务时间计费）

    # ----------------------------- 费率版本（IFeeRuleProvider） -----------------------------
    def current_rule(self, at: Optional[datetime] = None) -> FeeRuleDomain:
        """取 ``at`` 时点生效的费率（domain 视图）。无则惰性创建默认费率。"""
        rule = self.fee_repo.current_rule(at)
        if rule is None:
            rule = self.ensure_default_rule()
        return FeeRuleRepo.to_domain(rule)

    def ensure_default_rule(self, operator: str = "system"):
        """若库中无任何费率，按 config.billing 创建默认费率（启动时调用）。"""
        existing = self.fee_repo.list_rules()
        if existing:
            return existing[0]
        b = self.cfg.billing
        with self.db.scope():
            rule = self.fee_repo.create(
                free_minutes=b.free_minutes, first_hour_price=b.first_hour_price,
                unit_minutes=b.unit_minutes, unit_price=b.unit_price,
                daily_cap=b.daily_cap, ne_discount=b.ne_discount,
                round_up=b.round_up, operator=operator, note="系统默认费率")
        log.info("已创建默认费率: 免费%s分/首小时%s元/每%s分%s元/封顶%s元/新能源%s折",
                 b.free_minutes, b.first_hour_price, b.unit_minutes, b.unit_price,
                 b.daily_cap, b.ne_discount)
        return rule

    def add_rule(self, *, free_minutes: int, first_hour_price: float,
                 unit_minutes: int, unit_price: float, daily_cap: float,
                 ne_discount: float, round_up: bool, operator: str,
                 note: str = "", effective_from: Optional[datetime] = None) -> FeeRuleDomain:
        """新增费率版本（生效于 effective_from，默认现在）。旧记录仍按各自快照计费。"""
        with self.db.scope():
            rule = self.fee_repo.create(
                free_minutes=free_minutes, first_hour_price=first_hour_price,
                unit_minutes=unit_minutes, unit_price=unit_price, daily_cap=daily_cap,
                ne_discount=ne_discount, round_up=round_up, operator=operator,
                note=note, effective_from=effective_from)
            domain = FeeRuleRepo.to_domain(rule)
        log.info("新增费率版本 #%s by %s", domain.id, operator)
        return domain

    def list_rules(self) -> list[dict]:
        """历史费率版本（最新在前，含 effective_to/created_at）。"""
        return self.fee_repo.list_views()

    def current_rule_view(self, at: Optional[datetime] = None) -> Optional[dict]:
        """当前生效费率视图（含 effective_to/created_at）。"""
        return self.fee_repo.current_view(at)

    def _rule_for(self, rec) -> FeeRuleDomain:
        """按记录取费率快照：优先 fee_rule_id，否则进场时点生效费率。"""
        if getattr(rec, "fee_rule_id", None):
            r = self.fee_repo.get(rec.fee_rule_id)
            if r is not None:
                return FeeRuleRepo.to_domain(r)
        r = self.fee_repo.current_rule(rec.entry_time)
        if r is None:
            r = self.ensure_default_rule()
        return FeeRuleRepo.to_domain(r)

    # ----------------------------- 报价 -----------------------------
    def _now(self) -> datetime:
        """业务当前时间：演示模式=仿真业务时间；否则真实墙钟。"""
        if self.simulation is not None:
            return self.simulation.business_now()
        return utcnow()

    def quote(self, plate: str, exit_time: Optional[datetime] = None) -> Quote:
        """按车牌报价（出口显示屏/管理端）。无在场记录 → NoParkingRecordError。"""
        rec = self.parking.find_open_by_plate(plate)
        if rec is None:
            raise NoParkingRecordError(plate)
        return self.quote_exit(rec, exit_time=exit_time)

    def quote_exit(self, rec, exit_time: Optional[datetime] = None,
                   exit_photo: Optional[str] = None,
                   exit_crop: Optional[str] = None) -> Quote:
        exit_time = to_utc(exit_time) or self._now()
        if exit_photo or exit_crop:
            with self.db.scope():
                if exit_photo: rec.exit_photo = exit_photo
                if exit_crop: rec.exit_plate_crop = exit_crop
        rule = self._rule_for(rec)
        bd = self.calc.compute(rec, exit_time, rule)
        paid = self.payment.get_by_parking(rec.id) is not None
        duration_sec = max(0.0, (exit_time - to_utc(rec.entry_time)).total_seconds())
        sim = getattr(self, "simulation", None)
        demo = bool(getattr(self.cfg.server, "demo_mode", False)) and sim is not None
        exit_sim = float(sim.sim_time) if demo else None
        entry_sim = rec.entry_sim_sec
        duration_sim = None
        if demo and entry_sim is not None and exit_sim is not None:
            duration_sim = round(exit_sim - float(entry_sim), 3)
        return Quote(
            record_id=rec.id, plate=rec.plate, plate_color=rec.plate_color,
            entry_time=rec.entry_time, exit_time=exit_time, minutes=bd.minutes,
            breakdown=bd, amount_due=bd.amount_due,
            entry_photo=rec.entry_photo, exit_photo=exit_photo or rec.exit_photo,
            space_no=rec.space_no, paid=paid,
            duration_sec=round(duration_sec, 3),
            entry_sim_sec=entry_sim, exit_sim_sec=exit_sim,
            duration_sim_sec=duration_sim)

    # ----------------------------- 缴费（幂等） -----------------------------
    @synchronized
    def pay(self, record_id: int, method: str = "mock_scan",
            operator: Optional[str] = None,
            exit_time: Optional[datetime] = None,
            amount_override: Optional[float] = None) -> PaymentRecord:
        """缴费：写收费记录（一单一付，幂等），事务内联动出场，提交后抬杆。

        ``method`` ∈ mock_scan/cash/free/monthly；``amount_override`` 用于人工议价/免费放行。
        重复调用返回已存在的收费记录，不再重复发布事件。
        """
        if amount_override is not None and (not math.isfinite(float(amount_override)) or amount_override < 0):
            raise ValidationError("实收金额必须为有限非负数")
        with self.db.scope():
            existing = self.payment.get_by_parking(record_id)
            if existing is not None:
                log.info("记录 #%s 已缴费（#%s），幂等返回", record_id, existing.id)
                return existing
            rec = self.parking.get(record_id)
            if rec is None:
                raise NotFound(f"在场记录 #{record_id} 不存在")
            q = self.quote_exit(rec, exit_time=exit_time)
            free_benefit = rec.benefit_type in ("white", "monthly")
            if free_benefit and amount_override not in (None, 0, 0.0):
                raise ValidationError("白名单/月租车本次免费，不能覆盖为收费金额")
            amount = max(0.0, q.amount_due if amount_override is None
                         else round(float(amount_override), 2))
            # 实现 A：自动结算显式 method=auto/system 时保留（不因白名单/月租改写），
            # 金额照常为 0；人工/剧本 pay 走原 free/monthly 语义。
            if free_benefit and method not in ("auto", "system"):
                method = "monthly" if rec.benefit_type == "monthly" else "free"
            pay = self.payment.create(
                parking_record_id=record_id, plate=rec.plate, minutes=q.minutes,
                billable_units=q.breakdown.billable_units, amount_due=q.amount_due,
                amount_paid=amount, method=method, operator=operator,
                breakdown=q.breakdown)
            plate, vehicle_id = rec.plate, rec.vehicle_id
            self.bus.publish(PaymentCompleted(
                record_id=record_id, plate=plate, amount=amount,
                method=method, vehicle_id=vehicle_id))
        log.info("缴费成功 record=#%s plate=%s 金额=%.2f 方式=%s",
                 record_id, plate, amount, method)
        return pay
