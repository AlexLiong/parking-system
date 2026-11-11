"""车位分配（access/allocator.py）。

对应设计文档 §3.3。规则（FR-2.5，C 区为新能源优先区，但**任何车可停任何区**）：
- 绿牌（新能源）优先序：C → A → B（→ D 兜底）；
- 蓝牌优先序：A → B → C（→ D 兜底），且受 ``c_reserve`` 约束：蓝牌最多把 C 区
  用到剩 ``c_reserve`` 个空位，为绿牌保留；
- 满位返回 None（入口据此不予抬杆，FR-2.3）。

分配/释放后发布 ``SpaceUpdate``（余位屏 / 管理端）。车位实际读写委托 SpaceRepo，
本类只做"策略"，保持低耦合、可单测。

route-v2 可选扩展：``chooser`` 回调（默认 None = 保持旧行为“每区第一个空位”）。
三维路由上线后可按“离入口/目标支路最近”等启发式选位，例如
``SpaceAllocator(repo, bus, chooser=nearest_to_portal)``；不传时行为完全不变。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ..core.events import EventBus, SpaceUpdate
from ..repository.models import ParkingSpace
from ..repository.space_repo import SpaceRepo

log = logging.getLogger("access.allocator")

GREEN_PREFS = ["C", "A", "B", "D"]
BLUE_PREFS = ["A", "B", "C", "D"]

#: 选位回调：输入（按优先级排列的空位列表, 车牌, 颜色）→ 选中的车位或 None。
Chooser = Callable[[list[ParkingSpace], str, str], Optional[ParkingSpace]]


class SpaceAllocator:
    def __init__(self, space_repo: SpaceRepo, bus: Optional[EventBus] = None,
                 *, c_reserve: int = 0, chooser: Optional[Chooser] = None):
        self.repo = space_repo
        self.bus = bus
        self.c_reserve = max(0, int(c_reserve))
        self.chooser = chooser

    # ----------------------------- 分配 / 释放 -----------------------------
    def allocate(self, plate: str, plate_color: str,
                 *, vehicle_id: Optional[str] = None) -> Optional[ParkingSpace]:
        """为车辆分配车位并占用；满位返回 None。"""
        is_green = (plate_color or "").lower() == "green"
        prefs = GREEN_PREFS if is_green else BLUE_PREFS
        # 已占则幂等返回原车位（防重复分配，RR-2.4）
        existing = self.repo.find_by_plate(plate)
        if existing is not None:
            return existing
        if self.chooser is not None:
            candidates = self._free_candidates(prefs, is_green)
            chosen = self.chooser(candidates, plate, plate_color) if candidates else None
            if chosen is not None and chosen in candidates:
                self.repo.occupy(chosen.space_no, plate)
                log.info("分配车位 %s → %s(%s, chooser)",
                         chosen.space_no, plate, plate_color)
                self._publish_update(recommend=chosen.space_no)
                return chosen
            # 回调未选中（或返回非法值）→ 回退默认策略，避免“有位置却不分配”
        for zone in prefs:
            sp = self.repo.first_free(
                zone, respect_c_reserve=not is_green, c_reserve=self.c_reserve)
            if sp is not None:
                self.repo.occupy(sp.space_no, plate)
                log.info("分配车位 %s → %s(%s)", sp.space_no, plate, plate_color)
                self._publish_update(recommend=sp.space_no)
                return sp
        log.warning("满位，无法为 %s 分配车位", plate)
        self._publish_update()
        return None

    def _free_candidates(self, prefs: list[str], is_green: bool) -> list[ParkingSpace]:
        """按优先级汇总候选空位（供 ``chooser`` 使用；保持区优先序）。"""
        candidates: list[ParkingSpace] = []
        for zone in prefs:
            frees = self.repo.free_in_zone(zone)
            if not is_green and self.c_reserve > 0 and len(frees) <= self.c_reserve:
                continue
            candidates.extend(frees)
        return candidates

    def release(self, space_no: Optional[str]) -> None:
        if not space_no:
            return
        self.repo.release(space_no)
        log.info("释放车位 %s", space_no)
        self._publish_update()

    def recommend(self, plate_color: str) -> Optional[str]:
        """推荐一个空位号（余位屏 / 导航），不占用。"""
        is_green = (plate_color or "").lower() == "green"
        prefs = GREEN_PREFS if is_green else BLUE_PREFS
        for zone in prefs:
            sp = self.repo.first_free(
                zone, respect_c_reserve=not is_green, c_reserve=self.c_reserve)
            if sp is not None:
                return sp.space_no
        return None

    # ----------------------------- 查询 -----------------------------
    def free_count(self) -> dict:
        by_zone = self.repo.free_count_by_zone()
        return {"total": self.repo.free_total(), "by_zone": by_zone}

    def _publish_update(self, recommend: Optional[str] = None) -> None:
        if self.bus is None:
            return
        self.bus.publish(SpaceUpdate(
            free_total=self.repo.free_total(),
            by_zone=self.repo.free_count_by_zone(),
            recommend=recommend,
        ))
