"""日志类仓储（repository/log_repo.py）——识别日志 / 异常事件 / 操作留痕。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from ..core.timeutil import iso_local, utcnow
from ..repository.db import Database
from ..repository.models import DemoSession, IncidentLog, OpLog, RecognitionLog


def _active_session_id(session) -> Optional[str]:
    return session.execute(select(DemoSession.id).where(
        DemoSession.status == "active").limit(1)).scalar_one_or_none()


class RecognitionLogRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def add(self, *, gate: str, image_path: Optional[str], plate: Optional[str],
            plate_color: Optional[str], char_confs: list[float], overall_conf: float,
            elapsed_ms: int, success: bool) -> RecognitionLog:
        row = RecognitionLog(
            ts=utcnow(), gate=gate, image_path=image_path, plate=plate,
            plate_color=plate_color, char_confs=json.dumps(char_confs),
            overall_conf=overall_conf, elapsed_ms=elapsed_ms, success=success,
            session_id=_active_session_id(self.s),
        )
        self.s.add(row)
        self.s.flush()
        return row

    def get(self, log_id: int) -> Optional[dict]:
        row = self.s.get(RecognitionLog, log_id)
        return self._view(row) if row else None

    def mark_corrected(self, log_id: int, corrected_plate: str) -> tuple[Optional[dict], bool]:
        row = self.s.get(RecognitionLog, log_id)
        if row is None:
            return None, False
        if row.manual_corrected and row.corrected_plate == corrected_plate:
            return self._view(row), False
        row.manual_corrected = True
        row.corrected_plate = corrected_plate
        self.s.flush()
        return self._view(row), True

    def mark_expected_match(self, log_id: int) -> Optional[dict]:
        """仿真 expected_plate 匹配后，把识别日志的成功位同步为 true。

        只影响审计/展示口径，不降低 HyperLPR3 阈值，也不改变真实照片上传判定。
        """
        row = self.s.get(RecognitionLog, log_id)
        if row is None:
            return None
        row.success = True
        self.s.flush()
        return self._view(row)

    def recent(self, limit: int = 50) -> list[dict]:
        stmt = select(RecognitionLog).order_by(RecognitionLog.id.desc()).limit(limit)
        rows = self.s.execute(stmt).scalars().all()
        return [self._view(r) for r in rows]

    @staticmethod
    def _view(r: RecognitionLog) -> dict:
        return {
            "id": r.id, "ts": iso_local(r.ts), "gate": r.gate,
            "image_path": r.image_path, "plate": r.plate, "plate_color": r.plate_color,
            "char_confs": json.loads(r.char_confs) if r.char_confs else [],
            "overall_conf": r.overall_conf, "elapsed_ms": r.elapsed_ms,
            "success": bool(r.success), "manual_corrected": bool(r.manual_corrected),
            "corrected_plate": r.corrected_plate,
        }


class IncidentRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def raise_incident(self, *, kind: str, gate: Optional[str] = None,
                       detail: Optional[dict] = None) -> IncidentLog:
        row = IncidentLog(
            ts=utcnow(), kind=kind, gate=gate,
            detail=json.dumps(detail or {}, ensure_ascii=False), status="open",
            session_id=_active_session_id(self.s),
        )
        self.s.add(row)
        self.s.flush()
        return row

    def resolve(self, incident_id: int, *, operator: str, resolution: str) -> Optional[IncidentLog]:
        row = self.s.get(IncidentLog, incident_id)
        if row is None:
            return None
        row.status = "resolved"
        row.resolved_by = operator
        row.resolved_at = utcnow()
        row.resolution = resolution
        self.s.flush()
        return row

    def get(self, incident_id: int) -> Optional[IncidentLog]:
        return self.s.get(IncidentLog, incident_id)

    def list(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        stmt = select(IncidentLog)
        if status:
            stmt = stmt.where(IncidentLog.status == status)
        stmt = stmt.order_by(IncidentLog.id.desc()).limit(limit)
        return [self._view(r) for r in self.s.execute(stmt).scalars().all()]

    def open_by_kind(self, kind: str, gate: Optional[str] = None) -> list[IncidentLog]:
        stmt = select(IncidentLog).where(IncidentLog.status == "open", IncidentLog.kind == kind)
        if gate:
            stmt = stmt.where(IncidentLog.gate == gate)
        return list(self.s.execute(stmt).scalars().all())

    @staticmethod
    def _view(r: IncidentLog) -> dict:
        return {
            "id": r.id, "ts": iso_local(r.ts), "kind": r.kind,
            "gate": r.gate, "detail": json.loads(r.detail) if r.detail else {},
            "status": r.status, "resolved_by": r.resolved_by,
            "resolved_at": iso_local(r.resolved_at),
            "resolution": r.resolution,
        }


class OpLogRepo:
    def __init__(self, db: Database):
        self.db = db

    @property
    def s(self):
        return self.db.session

    def add(self, *, operator: Optional[str], action: str, target: Optional[str] = None,
            detail: Optional[dict] = None) -> OpLog:
        row = OpLog(
            ts=utcnow(), operator=operator, action=action, target=target,
            detail=json.dumps(detail or {}, ensure_ascii=False),
        )
        self.s.add(row)
        self.s.flush()
        return row

    def recent(self, limit: int = 100) -> list[dict]:
        stmt = select(OpLog).order_by(OpLog.id.desc()).limit(limit)
        rows = self.s.execute(stmt).scalars().all()
        return [{
            "id": r.id, "ts": iso_local(r.ts), "operator": r.operator,
            "action": r.action, "target": r.target,
            "detail": json.loads(r.detail) if r.detail else {},
        } for r in rows]
