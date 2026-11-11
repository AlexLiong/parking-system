"""一致性与幂等防线（access/guard.py）。

对应设计文档 §3.3 / §10（RR-2.4、FR-9.6）。两道职责：

1. **运行时幂等校验**（进出场业务前置）：
   - ``check_entry``：同车牌已有未出场记录 → ``DuplicateEntryError``（重复进场）；
   - ``check_exit``：无在场记录 → ``NoParkingRecordError``（出场无记录，转异常流）。

2. **启动/周期对账** ``reconcile``：比对"被占车位"与"在场记录"两个真相，
   找出不一致（半事务遗留、崩溃恢复后的脏数据），产出报告供 recovery 模块决策。
   本方法**只读**、不改数据，修复动作由 recovery 显式执行（可审计）。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.errors import DuplicateEntryError, NoParkingRecordError
from ..repository.models import ParkingRecord
from ..repository.parking_repo import ParkingRecordRepo
from ..repository.space_repo import SpaceRepo

log = logging.getLogger("access.guard")


class ConsistencyGuard:
    def __init__(self, parking_repo: ParkingRecordRepo, space_repo: SpaceRepo):
        self.parking = parking_repo
        self.spaces = space_repo

    # ----------------------------- 运行时校验 -----------------------------
    def check_entry(self, plate: str) -> None:
        """重复进场拦截：该车牌若已有未出场记录则拒绝再次进场。

        temporary 是无入场记录的人工出场账单，不是真实在场记录，不能阻止同牌
        再次入场（P1-09）。
        """
        open_rec = self.parking.find_open_by_plate(
            plate, include_temporary=False)
        if open_rec is not None:
            raise DuplicateEntryError(plate)

    def check_exit(self, plate: str) -> ParkingRecord:
        """出场校验：返回该车牌的在场记录；无则抛 NoParkingRecordError。"""
        rec = self.parking.find_open_by_plate(plate)
        if rec is None:
            raise NoParkingRecordError(plate)
        return rec

    # ----------------------------- 对账（只读） -----------------------------
    def reconcile(self) -> dict:
        """比对车位占用与在场记录，返回不一致清单（不修改数据）。

        - ``space_without_record``：车位被占但无对应在场记录（记录丢失/半事务）；
        - ``record_without_space``：在场记录声明的车位已不处于占用/或车位不存在；
        - ``plate_mismatch``：车位 current_plate 与记录 plate 不一致。
        """
        open_recs = [r for r in self.parking.all_open() if not r.temporary]
        rec_by_space: dict[str, ParkingRecord] = {}
        rec_plates: set[str] = set()
        for r in open_recs:
            rec_plates.add(r.plate)
            if r.space_no:
                rec_by_space[r.space_no] = r

        occupied = [sp for sp in self.spaces.list_active()
                    if sp.status == "occupied"]

        space_without_record: list[str] = []
        plate_mismatch: list[dict] = []
        for sp in occupied:
            r = rec_by_space.get(sp.space_no)
            if r is None:
                space_without_record.append(sp.space_no)
            elif sp.current_plate and r.plate != sp.current_plate:
                plate_mismatch.append({
                    "space_no": sp.space_no,
                    "space_plate": sp.current_plate,
                    "record_plate": r.plate,
                })

        record_without_space: list[dict] = []
        for r in open_recs:
            if not r.space_no:
                record_without_space.append({"record_id": r.id, "plate": r.plate,
                                             "reason": "no_space"})
                continue
            sp = self.spaces.get(r.space_no)
            if sp is None or sp.disabled:
                record_without_space.append({"record_id": r.id, "plate": r.plate,
                                             "space_no": r.space_no,
                                             "reason": "space_missing"})
            elif sp.status != "occupied":
                record_without_space.append({"record_id": r.id, "plate": r.plate,
                                             "space_no": r.space_no,
                                             "reason": "space_not_occupied"})

        report = {
            "open_records": len(open_recs),
            "occupied_spaces": len(occupied),
            "space_without_record": space_without_record,
            "record_without_space": record_without_space,
            "plate_mismatch": plate_mismatch,
            "consistent": not (space_without_record or record_without_space
                               or plate_mismatch),
        }
        if not report["consistent"]:
            log.warning("对账发现不一致: %s", {
                k: report[k] for k in ("space_without_record",
                                       "record_without_space", "plate_mismatch")})
        return report
