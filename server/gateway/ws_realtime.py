"""实时广播（gateway/ws_realtime.py）。

对应设计文档 §3.7 / §11.2。订阅 EventBus 上的**所有业务事件**，映射为约定的
WebSocket JSON 消息广播给全部已连接前端（3D 端 / 管理端）。实现 IBroadcaster。

关键工程点：
- **线程安全**：事件可能在 FastAPI 线程池（同步路由）里发布，发送协程通过
  ``run_coroutine_threadsafe`` 投递到主事件循环，避免跨线程操作 WebSocket；
- **断连兜底**（FR-9.5）：无连接期间事件进入有界缓冲；客户端重连后一次性
  下发 ``sync`` 消息追平；连接数归零时回调 ``on_empty``（access 锁定道闸安全侧）；
- 业务模块**不碰网络**：只发事件，广播由本网关组件完成（低耦合）。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable, Optional

from fastapi import WebSocket

from ..core.config import AppConfig
from ..core.timeutil import iso_local
from ..core.events import (ArrivalRetryRequested, BarrierCommand, BarrierStateChanged,
                           EntryDenied, Event, EventBus, ExitAuthorized, FeeQuoted,
                           IncidentRaised,
                           IncidentResolved, LayoutChanged, ManualAuthorized,
                           PendingCreated, PendingResolved, PlateRecognized,
                           SpaceAssigned, SimulationChanged, PaymentCompleted,
                           SpaceUpdate)

log = logging.getLogger("gateway.ws")


def event_to_message(e: Event) -> Optional[dict]:
    """业务事件 → WS 协议消息（§11.2）。未知事件返回 None（不广播）。"""
    if isinstance(e, PaymentCompleted):
        return {"type":"payment_completed","record_id":e.record_id,"plate":e.plate,
                "vehicle_id":e.vehicle_id,"amount":e.amount,"method":e.method}
    if isinstance(e, ArrivalRetryRequested):
        # 旧帧无法识别：请求 3D 重新抓拍并上报 arrival（前端可立即重试）
        return {"type": "arrival_retry_requested", "vehicle_id": e.vehicle_id,
                "gate": e.gate, "reason": e.reason, "message": e.message}
    if isinstance(e, EntryDenied):
        # 黑名单/拒绝入场：3D 端据此"无后车倒车离场 / 有后车进场后直接出场"
        return {"type": "entry_denied", "vehicle_id": e.vehicle_id,
                "gate": e.gate, "plate": e.plate, "reason": e.reason,
                "message": e.message, "expected_plate": e.expected_plate,
                "image_path": e.image_path,
                "mode": getattr(e, "mode", "") or (
                    "blacklist" if e.reason == "blacklist" else "denied")}
    if isinstance(e, ExitAuthorized):
        # 人工授权出场（结算/免费/作废）→ 3D 驶离，出口道闸由 PaymentCompleted 流程打开
        return {"type": "exit_authorized", "vehicle_id": e.vehicle_id,
                "plate": e.plate, "gate": e.gate, "record_id": e.record_id,
                "action": e.action, "amount_paid": e.amount_paid,
                "space_no": e.space_no, "reason": e.reason}
    if isinstance(e, ManualAuthorized):
        # 人工授权入场 → 3D/管理端同步手动放行车辆状态
        return {"type": "manual_authorized", "vehicle_id": e.vehicle_id,
                "plate": e.plate, "gate": e.gate, "record_id": e.record_id,
                "space_no": e.space_no, "action": e.action, "reason": e.reason}
    if isinstance(e, SimulationChanged):
        return {"type":"simulation_state", **e.model_dump(exclude={"type","event_id","ts"})}
    if isinstance(e, PlateRecognized):
        return {"type": "plate_recognized", "gate": e.gate,
                "vehicle_id": e.vehicle_id, "plate": e.plate, "color": e.color,
                "conf": round(e.overall_conf, 4), "need_manual": e.need_manual,
                "candidates": e.candidates, "reason": e.reason,
                "message": e.message,
                # 识别输入图片（本次识别实际使用的图，区别于 3D 车身车牌渲染）
                "image_path": e.image_path, "image_url": e.image_url,
                "plate_crop_url": e.plate_crop_url,
                "image_source": e.image_source, "image_synthetic": e.image_synthetic,
                "source": e.source, "expected_plate": e.expected_plate,
                "plate_match": e.plate_match}
    if isinstance(e, PendingCreated):
        return {"type": "pending_created", "pending_id": e.pending_id,
                "gate": e.gate, "vehicle_id": e.vehicle_id,
                "reason": e.reason, "source_plate": e.source_plate,
                "candidates": e.candidates,
                "expires_at": iso_local(e.expires_at)}
    if isinstance(e, PendingResolved):
        return {"type": "pending_resolved", "pending_id": e.pending_id,
                "gate": e.gate, "vehicle_id": e.vehicle_id,
                "status": e.status, "plate": e.plate,
                "message": e.message}
    if isinstance(e, FeeQuoted):
        return {"type": "fee_quote", "record_id": e.record_id,
                "vehicle_id": e.vehicle_id, "plate": e.plate,
                "minutes": e.minutes, "amount": e.amount,
                "entry_photo": e.entry_photo, "exit_photo": e.exit_photo}
    if isinstance(e, BarrierCommand):
        return {"type": "barrier_cmd", "gate": e.gate, "cmd": e.cmd,
                "reason": e.reason, "cmd_id": e.cmd_id, "target": e.target,
                "duration_sec": e.duration_sec,
                # 实现 A：物理可通行/人工操作/授权来源（manual ≠ 业务授权）
                "manual": e.manual, "passable": e.passable,
                "authorization": e.authorization}
    if isinstance(e, BarrierStateChanged):
        return {"type": "barrier_state", "gate": e.gate, "state": e.state,
                "cmd_id": e.cmd_id, "transient": e.transient,
                "reason": e.reason, "keep_open": e.keep_open,
                "arm_position": e.arm_position,
                "manual": e.manual, "passable": e.passable,
                "authorization": e.authorization}
    if isinstance(e, SpaceUpdate):
        free = {"total": e.free_total, **(e.by_zone or {})}
        return {"type": "space_update", "free": free, "recommend": e.recommend}
    if isinstance(e, SpaceAssigned):
        return {"type": "space_assigned", "vehicle_id": e.vehicle_id,
                "plate": e.plate, "space_no": e.space_no, "zone": e.zone}
    if isinstance(e, LayoutChanged):
        return {"type": "layout_changed", "version": e.version, "diff": e.diff}
    if isinstance(e, IncidentRaised):
        return {"type": "incident", "kind": e.kind, "gate": e.gate,
                "detail": e.detail, "incident_id": e.incident_id}
    if isinstance(e, IncidentResolved):
        return {"type": "incident_resolved", "kind": e.kind,
                "incident_id": e.incident_id, "resolution": e.resolution}
    return None


class Broadcaster:
    """WebSocket 连接管理 + 事件广播。"""

    def __init__(self, bus: EventBus, cfg: AppConfig,
                 *, on_empty: Optional[Callable[[], None]] = None,
                 buffer_size: int = 500, state=None):
        self.bus = bus
        self.cfg = cfg
        self.on_empty = on_empty
        self._conns: set[WebSocket] = set()
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribed = False
        self.state = state
        self._v2: dict[WebSocket, int] = {}
        self._v2_locks: dict[WebSocket, asyncio.Lock] = {}

    # ----------------------------- 生命周期 -----------------------------
    def attach(self, loop: asyncio.AbstractEventLoop, *, subscribe: bool = True) -> None:
        self._loop = loop
        if subscribe and not self._subscribed:
            self.bus.subscribe_all(self._on_event)
            self._subscribed = True

    @property
    def connection_count(self) -> int:
        return len(self._conns) + len(self._v2)

    async def connect(self, ws: WebSocket, *, v2: bool = False,
                      since: int = 0, session: str | None = None,
                      subprotocol: str | None = None) -> None:
        await ws.accept(subprotocol=subprotocol)
        if v2 and self.state is not None:
            self._v2[ws] = 0
            lock = asyncio.Lock()
            self._v2_locks[ws] = lock
            async with lock:
                snap = self.state.snapshot()
                self.state.db.remove()
                await ws.send_json({"type": "welcome", "protocol": 2,
                                    "session": snap["session"]["id"] if snap["session"] else None,
                                    "head_seq": snap["seq"],
                                    "heartbeat_sec": self.cfg.recovery.ws_heartbeat_sec})
                current = snap["session"]["id"] if snap["session"] else None
                if 0 < since <= snap["seq"] and (session is None or session == current):
                    self._v2[ws] = since
                else:
                    await ws.send_json({"type": "snapshot", "data": snap})
                    self._v2[ws] = snap["seq"]
            await self._catch_up(ws)
            return
        first = len(self._conns) == 0
        self._conns.add(ws)
        log.info("WS 连接建立，当前 %d 个客户端", len(self._conns))
        # 重连追平：把断连期间缓冲的事件一次性下发
        if self._buffer:
            pending = list(self._buffer)
            self._buffer.clear()
            try:
                await ws.send_json({"type": "sync", "events": pending})
            except Exception:  # noqa: BLE001
                log.exception("下发 sync 失败")
        if first:
            try:
                await ws.send_json({"type": "welcome",
                                    "heartbeat_sec": self.cfg.recovery.ws_heartbeat_sec})
            except Exception:  # noqa: BLE001
                pass

    async def disconnect(self, ws: WebSocket) -> None:
        self._conns.discard(ws)
        self._v2.pop(ws, None)
        self._v2_locks.pop(ws, None)
        log.info("WS 连接断开，剩余 %d 个客户端", self.connection_count)
        if self.connection_count == 0 and self.on_empty is not None:
            try:
                self.on_empty()          # 全部断开 → 道闸锁定安全侧
            except Exception:  # noqa: BLE001
                log.exception("on_empty 回调失败")

    # ----------------------------- 广播 -----------------------------
    def _on_event(self, e: Event) -> None:
        msg = event_to_message(e)
        if msg is None:
            return
        self.publish_message(msg)

    def publish_message(self, msg: dict) -> None:
        """线程安全地把消息投递到事件循环广播；无连接时进入缓冲。"""
        if not self._conns:
            self._buffer.append(msg)
            return
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_all(msg), self._loop)
        except RuntimeError:
            # 循环已关闭（进程退出中）
            self._buffer.append(msg)

    def publish_event(self, event: dict, legacy: Optional[dict] = None) -> None:
        """Notify clients after a durable event commits; v2 reads by seq."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast_event(event, legacy),
                                             self._loop)
        except RuntimeError:
            log.exception("事件广播循环不可用；可通过 seq 补发")

    async def _broadcast_event(self, event: dict, legacy: Optional[dict]) -> None:
        # run_coroutine_threadsafe inherits the publishing HTTP request context.
        # A broadcast must never remove or reuse that request's live transaction.
        from ..repository.db import request_scope
        from uuid import uuid4
        key = request_scope.set(uuid4().hex)
        try:
            if legacy is not None:
                await self._send_all(legacy)
            for ws in list(self._v2):
                await self._catch_up(ws)
        finally:
            if self.state is not None:
                self.state.db.remove()
            request_scope.reset(key)

    async def _catch_up(self, ws: WebSocket) -> None:
        lock = self._v2_locks.get(ws)
        if lock is None or self.state is None:
            return
        async with lock:
            while ws in self._v2:
                batch = self.state.events(self._v2[ws], 500)
                self.state.db.remove()
                items = batch["items"]
                if not items:
                    break
                try:
                    if len(items) == 1:
                        await ws.send_json(items[0])
                    else:
                        await ws.send_json({"type": "batch", "events": items})
                except Exception:  # noqa: BLE001
                    await self.disconnect(ws)
                    break
                self._v2[ws] = items[-1]["seq"]
                if not batch["has_more"]:
                    break

    async def broadcast(self, message: dict) -> None:
        """IBroadcaster 接口：直接在事件循环内广播。"""
        await self._send_all(message)

    async def _send_all(self, msg: dict) -> None:
        if not self._conns:
            self._buffer.append(msg)
            return
        dead: list[WebSocket] = []
        for ws in list(self._conns):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 - 单连接失败不影响其它连接
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)
