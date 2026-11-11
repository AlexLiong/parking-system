"""Persistence for manual recognition review items."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from ..core.timeutil import iso_local, utcnow
from .models import DemoSession, PendingManual


class PendingManualRepo:
    def __init__(self, db):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def current_session_id(self) -> str | None:
        return self.s.execute(select(DemoSession.id).where(
            DemoSession.status == "active").limit(1)).scalar_one_or_none()

    def get(self, item_id: int) -> PendingManual | None:
        return self.s.get(PendingManual, item_id)

    def confirmed_for_log(self, log_id: int) -> PendingManual | None:
        return self.s.execute(select(PendingManual).where(
            PendingManual.recognition_log_id == log_id,
            PendingManual.status == "confirmed").limit(1)).scalar_one_or_none()

    def list(self, *, session_id: str | None, status: str = "pending",
             limit: int = 100) -> list[dict]:
        query = select(PendingManual).where(PendingManual.session_id == session_id)
        if status != "all":
            query = query.where(PendingManual.status == status)
        rows = self.s.execute(query.order_by(PendingManual.id.desc()).limit(limit)).scalars()
        return [self.view(row) for row in rows]

    def create_or_refresh(self, *, gate: str, vehicle_id: str | None,
                          image_path: str | None, candidates: list[str],
                          source_plate: str | None, plate_color: str | None,
                          recognition_log_id: int | None, reason: str,
                          message: str, timeout_seconds: int,
                          sim_now: float | None = None,
                          speed: float = 1.0) -> tuple[PendingManual, bool]:
        session_id = self.current_session_id()
        now = utcnow()
        row = None
        if vehicle_id:
            row = self.s.execute(select(PendingManual).where(
                PendingManual.session_id == session_id,
                PendingManual.gate == gate,
                PendingManual.vehicle_id == vehicle_id,
                PendingManual.status == "pending")).scalar_one_or_none()
        created = row is None
        if created:
            row = PendingManual(session_id=session_id, gate=gate,
                                vehicle_id=vehicle_id, created_at=now)
            self.s.add(row)
        else:
            row.attempts += 1
        row.image_path = image_path
        row.candidates_json = json.dumps(candidates, ensure_ascii=False)
        row.source_plate = source_plate
        row.plate_color = plate_color
        row.recognition_log_id = recognition_log_id
        row.reason = reason
        row.message = message
        # 演示模式：主超时用仿真秒（sim_deadline_sec），真实 expires_at 按当前倍速折算
        # （非演示模式 sim_now=None，保持原真实秒超时语义）。
        timeout = max(1, int(timeout_seconds))
        if sim_now is not None:
            row.sim_deadline_sec = float(sim_now) + timeout
            row.expires_at = now + timedelta(seconds=timeout / max(0.25, float(speed or 1.0)))
        else:
            row.sim_deadline_sec = None
            row.expires_at = now + timedelta(seconds=timeout)
        self.s.flush()
        return row, created

    def resolve(self, row: PendingManual, *, status: str, operator: str,
                plate: str | None = None, resolution: str = "") -> None:
        row.status = status
        row.operator = operator
        row.confirmed_plate = plate
        row.resolution = resolution
        row.resolved_at = utcnow()
        self.s.flush()

    def expire_due(self, sim_now: float | None = None) -> list[PendingManual]:
        now = utcnow()
        rows = list(self.s.execute(select(PendingManual).where(
            PendingManual.status == "pending")).scalars())
        rows = [row for row in rows if self._is_due(row, sim_now, now)]
        for row in rows:
            self.resolve(row, status="expired", operator="system", resolution="timeout")
        return rows

    @staticmethod
    def _is_due(row: PendingManual, sim_now: float | None, now) -> bool:
        """演示模式优先按仿真秒；真实 expires_at 作为兜底（兼容旧数据/测试）。"""
        if sim_now is not None and row.sim_deadline_sec is not None:
            if float(sim_now) >= float(row.sim_deadline_sec):
                return True
        return bool(row.expires_at is not None and row.expires_at <= now)

    def shift_pending_deadlines(self, seconds: float,
                                session_id: str | None) -> int:
        """演示暂停/恢复时顺延 pending 的绝对到期时间（P1-05）。"""
        if seconds <= 0:
            return 0
        rows = list(self.s.execute(select(PendingManual).where(
            PendingManual.status == "pending",
            PendingManual.session_id == session_id)).scalars())
        delta = timedelta(seconds=float(seconds))
        for row in rows:
            row.expires_at += delta
        self.s.flush()
        return len(rows)

    @staticmethod
    def view(row: PendingManual) -> dict:
        return {
            "id": row.id, "session_id": row.session_id, "gate": row.gate,
            "vehicle_id": row.vehicle_id, "image_path": row.image_path,
            "candidates": json.loads(row.candidates_json or "[]"),
            "source_plate": row.source_plate, "plate_color": row.plate_color,
            "recognition_log_id": row.recognition_log_id,
            "reason": row.reason, "message": row.message,
            "attempts": row.attempts,
            "created_at": iso_local(row.created_at),
            "expires_at": iso_local(row.expires_at),
            "sim_deadline_sec": row.sim_deadline_sec,
            "status": row.status, "confirmed_plate": row.confirmed_plate,
            "operator": row.operator, "resolution": row.resolution,
            "resolved_at": iso_local(row.resolved_at),
        }
