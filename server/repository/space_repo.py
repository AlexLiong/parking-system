"""车位仓储（repository/space_repo.py）——布局唯一事实源的读写封装。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select

from ..repository.db import Database
from ..repository.models import ParkingSpace


class SpaceRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    # ----------------------------- 查询 -----------------------------
    def get(self, space_no: str) -> Optional[ParkingSpace]:
        return self.s.get(ParkingSpace, space_no)

    def list_active(self) -> list[ParkingSpace]:
        stmt = select(ParkingSpace).where(ParkingSpace.disabled == False).order_by(  # noqa: E712
            ParkingSpace.zone, ParkingSpace.space_no)
        return list(self.s.execute(stmt).scalars().all())

    def list_all(self) -> list[ParkingSpace]:
        stmt = select(ParkingSpace).order_by(ParkingSpace.space_no)
        return list(self.s.execute(stmt).scalars().all())

    def free_in_zone(self, zone: str) -> list[ParkingSpace]:
        stmt = (select(ParkingSpace)
                .where(ParkingSpace.zone == zone, ParkingSpace.status == "free",
                       ParkingSpace.disabled == False)  # noqa: E712
                .order_by(ParkingSpace.space_no))
        return list(self.s.execute(stmt).scalars().all())

    def free_count_by_zone(self) -> dict[str, int]:
        stmt = (select(ParkingSpace.zone, func.count())
                .where(ParkingSpace.status == "free", ParkingSpace.disabled == False)  # noqa: E712
                .group_by(ParkingSpace.zone))
        return {z: int(c) for z, c in self.s.execute(stmt).all()}

    def total_count_by_zone(self) -> dict[str, int]:
        stmt = (select(ParkingSpace.zone, func.count())
                .where(ParkingSpace.disabled == False)  # noqa: E712
                .group_by(ParkingSpace.zone))
        return {z: int(c) for z, c in self.s.execute(stmt).all()}

    def free_total(self) -> int:
        stmt = (select(func.count()).select_from(ParkingSpace)
                .where(ParkingSpace.status == "free", ParkingSpace.disabled == False))  # noqa: E712
        return int(self.s.execute(stmt).scalar() or 0)

    def active_total(self) -> int:
        stmt = (select(func.count()).select_from(ParkingSpace)
                .where(ParkingSpace.disabled == False))  # noqa: E712
        return int(self.s.execute(stmt).scalar() or 0)

    def occupied_count(self) -> int:
        stmt = (select(func.count()).select_from(ParkingSpace)
                .where(ParkingSpace.status == "occupied", ParkingSpace.disabled == False))  # noqa: E712
        return int(self.s.execute(stmt).scalar() or 0)

    # ----------------------------- 分配 / 释放 -----------------------------
    def first_free(self, zone: str, *, respect_c_reserve: bool = False,
                   c_reserve: int = 0) -> Optional[ParkingSpace]:
        """取某区第一个空位。

        ``respect_c_reserve`` 为 True（蓝牌进 C 区）时，若 C 区剩余空位 <= 保留配额，
        则不把 C 区的最后配额分给蓝牌（FR-2.5 新能源保留）。
        """
        frees = self.free_in_zone(zone)
        if not frees:
            return None
        if respect_c_reserve and c_reserve > 0 and len(frees) <= c_reserve:
            return None
        return frees[0]

    def occupy(self, space_no: str, plate: str) -> ParkingSpace:
        sp = self.get(space_no)
        if sp is None:
            raise KeyError(space_no)
        sp.status = "occupied"
        sp.current_plate = plate
        self.s.flush()
        return sp

    def release(self, space_no: Optional[str]) -> None:
        if not space_no:
            return
        sp = self.get(space_no)
        if sp is None:
            return
        sp.status = "free"
        sp.current_plate = None
        self.s.flush()

    def find_by_plate(self, plate: str) -> Optional[ParkingSpace]:
        stmt = (select(ParkingSpace)
                .where(ParkingSpace.current_plate == plate,
                       ParkingSpace.disabled == False).limit(1))  # noqa: E712
        return self.s.execute(stmt).scalar_one_or_none()

    # ----------------------------- 布局同步（供 LayoutService 用） -----------------------------
    def upsert(self, spec: dict) -> ParkingSpace:
        """插入或更新一个车位（布局同步）。已存在则更新几何/类型，不动占用状态。"""
        sp = self.get(spec["space_no"])
        if sp is None:
            sp = ParkingSpace(
                space_no=spec["space_no"], zone=spec["zone"], kind=spec.get("kind", "normal"),
                x=spec.get("x", 0.0), z=spec.get("z", 0.0),
                rotation=spec.get("rotation", 0.0), status="free", disabled=False,
            )
            self.s.add(sp)
        else:
            sp.zone = spec["zone"]
            sp.kind = spec.get("kind", sp.kind)
            sp.x = spec.get("x", sp.x)
            sp.z = spec.get("z", sp.z)
            sp.rotation = spec.get("rotation", sp.rotation)
            sp.disabled = False   # 若之前被软删除，重新声明则恢复
        self.s.flush()
        return sp

    def disable(self, space_no: str) -> None:
        """软删除：停用但保留历史引用（FR-2.6）。"""
        sp = self.get(space_no)
        if sp is None:
            return
        sp.disabled = True
        sp.status = "free"
        sp.current_plate = None
        self.s.flush()

    @staticmethod
    def to_spec(sp: ParkingSpace) -> dict:
        return {
            "space_no": sp.space_no, "zone": sp.zone, "kind": sp.kind,
            "x": sp.x, "z": sp.z, "rotation": sp.rotation,
            "status": sp.status, "disabled": bool(sp.disabled),
            "current_plate": sp.current_plate,
        }
