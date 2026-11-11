"""异常事件服务（recovery/incident.py）。

对应设计文档 §3.6 / §10。异常事件的**登记—处理闭环—面板数据源**。同时作为其它
模块（如道闸 FAULT）通过 EventBus 抛出的 ``IncidentRaised`` 的**持久化落点**：

- 本服务主动 ``raise_incident`` → 先落库拿到 id，再发布带 id 的事件（自身处理器
  据 id 非空跳过，避免重复落库）；
- 其它模块直接发布的 ``IncidentRaised``（incident_id 为空）→ 本服务订阅后静默落库，
  不再转发（网关广播器已收到原始事件）。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.domain import Incident, IncidentKind
from ..core.events import EventBus, IncidentRaised, IncidentResolved
from ..core.timeutil import from_iso
from ..repository.log_repo import IncidentRepo

log = logging.getLogger("recovery.incident")


def _to_kind(kind: str) -> IncidentKind:
    try:
        return IncidentKind(str(kind).lower())
    except ValueError:
        return IncidentKind.DATA


class IncidentService:
    def __init__(self, incident_repo: IncidentRepo, bus: EventBus, db,
                 *, subscribe: bool = True):
        self.repo = incident_repo
        self.bus = bus
        self.db = db
        if subscribe:
            self.bus.subscribe(IncidentRaised, self._on_external_incident)

    # ----------------------------- 登记 / 处理 -----------------------------
    def raise_incident(self, kind: str, gate: Optional[str] = None,
                       detail: Optional[dict] = None) -> Incident:
        detail = detail or {}
        with self.db.scope():
            row = self.repo.raise_incident(kind=kind, gate=gate, detail=detail)
            inc = self._to_domain(row)
            # 带 id 发布；outbox 与故障行同事务提交，自身处理器按 id 跳过。
            self.bus.publish(IncidentRaised(kind=kind, gate=gate, detail=detail,
                                            incident_id=inc.id))
        log.warning("登记异常事件 #%s kind=%s gate=%s detail=%s",
                    inc.id, kind, gate, detail)
        return inc

    def resolve(self, incident_id: int, operator: str,
                resolution: str = "") -> Optional[Incident]:
        with self.db.scope():
            row = self.repo.resolve(incident_id, operator=operator,
                                    resolution=resolution)
            inc = self._to_domain(row) if row else None
            if inc is not None:
                self.bus.publish(IncidentResolved(kind=inc.kind.value,
                                                  incident_id=incident_id,
                                                  resolution=resolution))
        if inc is not None:
            log.info("异常事件 #%s 已处理 by %s", incident_id, operator)
        return inc

    # ----------------------------- 查询（面板数据源） -----------------------------
    def list(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        return self.repo.list(status=status, limit=limit)

    def list_open(self, kind: Optional[str] = None,
                  gate: Optional[str] = None) -> list[dict]:
        if kind:
            return [self._view(r) for r in self.repo.open_by_kind(kind, gate)]
        return self.repo.list(status="open", limit=200)

    def has_open(self, kind: str, gate: Optional[str] = None) -> bool:
        return bool(self.repo.open_by_kind(kind, gate))

    def get(self, incident_id: int) -> Optional[dict]:
        row = self.repo.get(incident_id)
        return self._view(row) if row else None

    # ----------------------------- 内部 -----------------------------
    def _on_external_incident(self, e: IncidentRaised) -> None:
        """持久化其它模块直接发布的异常事件（无 incident_id）。"""
        if e.incident_id is not None:
            return                      # 本服务已落库并发布，跳过
        try:
            with self.db.scope():
                self.repo.raise_incident(kind=e.kind, gate=e.gate, detail=e.detail)
            log.warning("落库外部异常事件 kind=%s gate=%s", e.kind, e.gate)
        except Exception:  # noqa: BLE001 - 落库失败不应影响主流程
            log.exception("落库外部异常事件失败")

    @staticmethod
    def _view(row) -> dict:
        return IncidentRepo._view(row)

    @staticmethod
    def _to_domain(row) -> Incident:
        v = IncidentRepo._view(row)
        return Incident(
            id=v["id"], kind=_to_kind(v["kind"]), gate=v.get("gate"),
            detail=v.get("detail") or {}, status=v.get("status", "open"),
            ts=from_iso(v.get("ts")), resolved_by=v.get("resolved_by"),
            resolved_at=from_iso(v.get("resolved_at")),
            resolution=v.get("resolution"))
