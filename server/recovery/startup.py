"""启动恢复（recovery/startup.py）。

对应设计文档 §10.1（FR-9.1 / RR-4.1）。系统启动时顺序执行，把崩溃/断电遗留的
不一致拉回安全一致态，并输出 ``RecoveryReport``：

1. **修复半截交易**：已存在收费记录但停车记录仍 ``status='in'`` → 以收费时间为准
   标记出场（exit_time=paid_at, status='out'）并释放车位，登记 ``power`` 异常；
2. **道闸状态恢复**：一律回 CLOSED（安全侧），广播状态对账（由 access 执行）；
3. **车位对账**：以在场记录为真相源，修正车位占用/车牌；无记录的占用车位释放；
4. 汇总 ``RecoveryReport``，管理端提示"系统从异常中断中恢复，N 项已修复"。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..access.guard import ConsistencyGuard
from ..core.domain import RecoveryReport
from ..core.timeutil import utcnow
from ..repository.parking_repo import ParkingRecordRepo, PaymentRepo
from ..repository.space_repo import SpaceRepo
from .incident import IncidentService

log = logging.getLogger("recovery.startup")


class StartupRecovery:
    def __init__(self, *, db, parking_repo: ParkingRecordRepo,
                 payment_repo: PaymentRepo, space_repo: SpaceRepo,
                 incident_service: IncidentService,
                 guard: Optional[ConsistencyGuard] = None,
                 access_service=None):
        self.db = db
        self.parking = parking_repo
        self.payment = payment_repo
        self.spaces = space_repo
        self.incidents = incident_service
        self.guard = guard or ConsistencyGuard(parking_repo, space_repo)
        self.access = access_service

    def recover(self) -> RecoveryReport:
        messages: list[str] = []
        fixed_half = self._fix_half_transactions(messages)
        barrier_reset = self._reset_barriers(messages)
        reconciled = self._reconcile_spaces(messages)

        if fixed_half:
            # 半截交易属于断电/崩溃遗留，登记 power 异常供管理端知晓
            self.incidents.raise_incident(
                "power", None,
                {"reason": "startup_half_transaction", "count": fixed_half})

        report = RecoveryReport(
            fixed_half_transactions=fixed_half,
            barrier_reset=barrier_reset,
            space_reconciled=reconciled,
            messages=messages,
            recovered_at=utcnow())
        total = fixed_half + reconciled
        if total:
            log.warning("启动恢复完成：修复 %d 项（半截交易 %d，车位对账 %d）",
                        total, fixed_half, reconciled)
            messages.insert(0, f"系统从异常中断中恢复，{total} 项已修复")
            report.messages = messages
        else:
            log.info("启动恢复完成：无异常遗留")
        return report

    # ----------------------------- 步骤 -----------------------------
    def _fix_half_transactions(self, messages: list[str]) -> int:
        """收费已存在但记录仍在场 → 标记出场 + 释放车位。"""
        fixed = 0
        with self.db.scope():
            for rec in self.parking.all_open():
                pay = self.payment.get_by_parking(rec.id)
                if pay is None:
                    continue
                self.parking.mark_exit(rec, exit_time=pay.paid_at, status="out")
                if rec.space_no:
                    self.spaces.release(rec.space_no)
                fixed += 1
        if fixed:
            messages.append(f"修复半截交易 {fixed} 笔（已收费未出场）")
        return fixed

    def _reset_barriers(self, messages: list[str]) -> list[str]:
        """恢复终态；运动中命令标为故障并保留审计。"""
        if self.access is None:
            return []
        try:
            self.access.restore_barriers()
            gates = sorted(self.access.barriers.keys())
            messages.append(f"道闸持久状态已恢复：{gates}")
            return gates
        except Exception:  # noqa: BLE001
            log.exception("道闸复位失败")
            return []

    def _reconcile_spaces(self, messages: list[str]) -> int:
        """以在场记录为真相源修正车位状态。"""
        report = self.guard.reconcile()
        if report["consistent"]:
            return 0
        fixed = 0
        with self.db.scope():
            # 1) 车位被占但无在场记录 → 释放
            for space_no in report["space_without_record"]:
                self.spaces.release(space_no)
                fixed += 1
            # 2) 在场记录对应的车位未占用/车牌不符 → 以记录为准重建占用
            for rec in self.parking.all_open():
                if not rec.space_no:
                    continue
                sp = self.spaces.get(rec.space_no)
                if sp is None or sp.disabled:
                    continue
                if sp.status != "occupied" or sp.current_plate != rec.plate:
                    self.spaces.occupy(sp.space_no, rec.plate)
                    fixed += 1
            # 3) 车牌不一致（车位有记录但 plate 不符）→ 以记录为准（上一步已覆盖）
        messages.append(
            f"车位对账修正 {fixed} 处（无记录占用释放 "
            f"{len(report['space_without_record'])}，记录重建占用若干）")
        return fixed
