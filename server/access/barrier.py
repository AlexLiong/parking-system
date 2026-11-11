"""道闸状态机 + 防砸/跟车（access/barrier.py）。

对应设计文档 §6。每个闸口（in/out）一个 ``BarrierController`` 实例。后端只做
**逻辑裁决**（谁可通行、何时保持开启、何时落杆、落杆是否被打断），物理动画由
前端 ``BarrierGate`` 执行并在完成后回执 ``barrier_ack``。

状态机（BarrierState）：
    CLOSED --request_open--> OPENING --ack(open)--> KEEP_OPEN
    KEEP_OPEN --计时到期且杆区清空--> CLOSING --ack(closed)--> CLOSED
    CLOSING --ZoneOccupied(true)/interrupt_close--> OPENING（落杆打断，立即复抬）
    任意 --指令超时无回执且重试耗尽--> FAULT（锁定，转人工放行）

设计约束：
- **纯 dt 驱动**：所有计时靠 ``tick(dt)`` 累加，不依赖墙钟，便于确定性单测；
- 不直接碰网络：状态变化发 ``BarrierStateChanged``，指令发 ``BarrierCommand``，
  由 gateway 的 Broadcaster 转成 WebSocket 下发（低耦合，RR-4.2）；
- **断连兜底**：``force_safe()`` 锁定 KEEP_OPEN（安全侧，§6.2）；恢复时 ``reset()``
  一律回 CLOSED（安全侧，§10.1）。
"""
from __future__ import annotations

import logging
from uuid import uuid4
from typing import Optional

from ..core.config import AccessConfig
from ..core.domain import BarrierState
from ..core.events import (BarrierCommand, BarrierStateChanged, EventBus,
                           IncidentRaised)

log = logging.getLogger("access.barrier")


class TailgatingPolicy:
    """跟车判定：决定 KEEP_OPEN 计时到期后是否因为有合法后车而继续保持开启。

    后端只掌握"是否还有已授权但未通过的车辆"（permit 计数），物理间距由前端
    TrafficScheduler 负责（§6.2）。``pending_provider(gate)`` 返回该闸已授权待
    通过车辆数。
    """

    def __init__(self, pending_provider=None):
        self._pending = pending_provider or (lambda gate: 0)

    def next_vehicle_legit(self, gate: str) -> bool:
        """排队的后车是否已识别/已缴费（有 permit）。"""
        try:
            return int(self._pending(gate)) > 0
        except Exception:  # noqa: BLE001 - provider 异常不应影响落杆裁决
            log.exception("TailgatingPolicy pending_provider 异常")
            return False

    def should_keep_open(self, gate: str) -> bool:
        return self.next_vehicle_legit(gate)


class BarrierController:
    """单闸道闸状态机。"""

    def __init__(self, gate: str, cfg: AccessConfig, bus: EventBus,
                 policy: Optional[TailgatingPolicy] = None):
        self.gate = gate
        self.cfg = cfg
        self.bus = bus
        self.policy = policy or TailgatingPolicy()
        self.state = BarrierState.CLOSED
        self.zone_occupied = False          # 杆区（虚拟地感）是否有车
        self._keep_timer = 0.0              # KEEP_OPEN 保持计时
        self._close_delay = 0.0             # 杆区清空后延时落杆
        self._ack_pending: Optional[str] = None   # "open"/"closed" 等待回执
        self._ack_timer = 0.0
        self._retries_left = 0
        self._last_reason = ""
        self._ack_cmd_id: Optional[str] = None
        self.keep_open = False
        self.fault_mode: Optional[str] = None
        # 实现 A：可通行语义（物理可通行 ≠ 业务已授权）
        self._manual = False
        self._authorization = ""
        self._passable = False

    # ----------------------------- 对外指令 -----------------------------
    def is_passable(self) -> bool:
        """物理上是否可通行（供 API/快照/3D 判定）。"""
        return self.state in (BarrierState.OPENING, BarrierState.KEEP_OPEN)

    def request_open(self, reason: str = "", *, force: bool = False,
                     manual: bool = False, authorization: Optional[str] = None) -> None:
        """业务许可通行 → 抬杆。FAULT 锁定下拒绝（需走 manual_lift 人工抬杆）。"""
        self._last_reason = reason
        self._manual = bool(manual)
        self._authorization = authorization if authorization is not None else (reason or "business")
        if self.state == BarrierState.FAULT:
            log.warning("[%s] FAULT 锁定，忽略 request_open(%s)", self.gate, reason)
            return
        if not force and self.state in (BarrierState.OPENING, BarrierState.KEEP_OPEN):
            # 已开/开启中：重置保持计时即可
            self._keep_timer = self.cfg.keep_open_sec
            return
        if self.state == BarrierState.CLOSING:
            # 落杆中被打断：先停后抬（前端反向插值），发 interrupt
            self._emit_cmd("interrupt", reason or "落杆打断")
        cmd_id = uuid4().hex
        self._begin_ack("open", cmd_id)
        self._emit_cmd("open", reason, cmd_id)
        self._enter(BarrierState.OPENING)

    def manual_lift(self, reason: str = "manual_open") -> None:
        """人工抬杆（含 FAULT 手动曲柄/断连后人工放行）：物理可通行，不等于业务授权。

        与 ``request_open`` 不同：FAULT 下也允许强制进入 OPENING→KEEP_OPEN，
        供管理端/3D 黑名单绕行/故障人工放行使用（完整业务校验后由调用方负责）。
        """
        self._last_reason = reason
        self._manual = True
        self._authorization = "manual"
        self.fault_mode = None          # 允许后续 ack 被接受；故障 incident 仍待 clear_fault
        self._clear_ack()
        if self.state == BarrierState.CLOSING:
            self._emit_cmd("interrupt", reason)
        cmd_id = uuid4().hex
        self._begin_ack("open", cmd_id)
        self._emit_cmd("open", reason, cmd_id)
        self._enter(BarrierState.OPENING)

    def manual_hold(self, reason: str = "manual_open") -> None:
        """人工抬杆并**保持抬升**（P1）：keep_open=True，直到管理员落杆/恢复/重置。

        与 ``manual_lift`` 区别：``manual_lift`` 只完成一次抬杆（tick 到
        keep_open_sec 后可能自动落杆）；``manual_hold`` 持久保持 KEEP_OPEN，
        供人工抬杆/故障手动曲柄/黑名单绕行之外的"常开"操作使用。
        FAULT 下同样允许强制抬杆（曲柄），故障 incident 仍待 clear_fault。
        """
        self._last_reason = reason
        self._manual = True
        self._authorization = "manual"
        self.fault_mode = None
        self.keep_open = True
        self._clear_ack()
        if self.state == BarrierState.CLOSING:
            self._emit_cmd("interrupt", reason)
        cmd_id = uuid4().hex
        self._begin_ack("open", cmd_id)
        self._emit_cmd("open", reason, cmd_id)
        self._enter(BarrierState.OPENING)

    def request_close(self, *, manual: bool = False, reason: str = "") -> None:
        """请求落杆（仅在 KEEP_OPEN 且杆区清空、无合法后车时真正执行）。"""
        self._manual = bool(manual)
        self._authorization = "manual" if manual else (reason or "business")
        if self.state != BarrierState.KEEP_OPEN and not (manual and self.state in (
                BarrierState.OPENING, BarrierState.STOPPED, BarrierState.CLOSED)):
            return
        if self.zone_occupied or (not manual and (self.keep_open or self.policy.should_keep_open(self.gate))):
            # 还有车/合法后车：重新计时，保持开启
            self._keep_timer = self.cfg.keep_open_sec
            return
        cmd_id = uuid4().hex
        self._begin_ack("closed", cmd_id)
        self._emit_cmd("close", reason, cmd_id)
        self._enter(BarrierState.CLOSING)

    def request_stop(self, reason: str = "manual_stop") -> None:
        self._manual = True
        self._authorization = "manual"
        cmd_id = uuid4().hex
        self._begin_ack("stopped", cmd_id)
        self._emit_cmd("stop", reason, cmd_id)
        self._enter(BarrierState.STOPPED)

    def set_keep_open(self, enabled: bool, *, manual: bool = True) -> None:
        self.keep_open = enabled
        self._manual = bool(manual)
        self._authorization = "keep_open" if enabled else "manual"
        if enabled:
            self.request_open("keep_open", force=True, manual=manual,
                              authorization="keep_open")
        else:
            self._keep_timer = self.cfg.keep_open_sec
            self.bus.publish(BarrierStateChanged(gate=self.gate, state=self.state.value,
                cmd_id=self._ack_cmd_id, keep_open=False, reason="keep_open_disabled",
                manual=self._manual, passable=self.is_passable(),
                authorization=self._authorization))

    def interrupt_close(self, reason: str = "") -> None:
        """落杆被打断 → 立即复抬（FR-4.3）。"""
        if self.state == BarrierState.CLOSING:
            self.request_open(reason or "interrupt")

    def on_zone_occupied(self, occupied: bool, vehicle_id: Optional[str] = None) -> None:
        """杆区（虚拟地感）占用变化：防砸输入（FR-4.3）。"""
        self.zone_occupied = occupied
        if occupied:
            if self.state == BarrierState.CLOSING:
                self.interrupt_close("杆区闯入")
            elif self.state == BarrierState.KEEP_OPEN:
                self._keep_timer = self.cfg.keep_open_sec   # 有车在杆区，保持
        else:
            # 杆区清空：启动落杆延时（KEEP_OPEN 计时到期后由 tick 落杆）
            self._close_delay = self.cfg.close_delay_sec

    def on_vehicle_passed(self) -> None:
        """车辆完全越过闸杆平面（FR-4.4）：重置保持计时。"""
        if self.state in (BarrierState.KEEP_OPEN, BarrierState.OPENING):
            self._keep_timer = self.cfg.keep_open_sec

    def on_ack(self, ack_state: str, cmd_id: Optional[str] = None,
               *, require_cmd: bool = False) -> bool:
        """前端动画完成回执（barrier_ack: open/closed）。"""
        if self.state == BarrierState.FAULT or self.fault_mode == "ack_timeout":
            return False
        if require_cmd and not cmd_id:
            return False
        if cmd_id and cmd_id != self._ack_cmd_id:
            return False
        if ack_state == "open" and self._ack_pending == "open":
            if self.state == BarrierState.OPENING:
                self._enter(BarrierState.KEEP_OPEN)
                self._keep_timer = self.cfg.keep_open_sec
            self._clear_ack()
            return True
        elif ack_state == "closed" and self._ack_pending == "closed":
            if self.state == BarrierState.CLOSING:
                self._enter(BarrierState.CLOSED)
            self._clear_ack()
            return True
        elif ack_state == "stopped" and self._ack_pending == "stopped":
            self._clear_ack()
            return True
        return False

    # ----------------------------- 定时驱动 -----------------------------
    def tick(self, dt: float) -> None:
        """定时器步长驱动（默认 50ms）。FAULT/断连暂停时不推进业务计时。"""
        if self.state == BarrierState.FAULT:
            return
        # 1) 指令回执超时 → 重试 → 故障
        if self._ack_pending is not None:
            self._ack_timer -= dt
            if self._ack_timer <= 0:
                self._handle_ack_timeout()
            return
        # 2) KEEP_OPEN 计时
        if self.state == BarrierState.KEEP_OPEN:
            if self.keep_open or self.zone_occupied or self.policy.should_keep_open(self.gate):
                self._keep_timer = self.cfg.keep_open_sec
                return
            self._keep_timer -= dt
            if self._keep_timer <= 0:
                # 计时到期：杆区清空后延时落杆
                if self._close_delay > 0:
                    self._close_delay -= dt
                    if self._close_delay > 0:
                        return
                self.request_close()

    def _handle_ack_timeout(self) -> None:
        if self._retries_left > 0:
            self._retries_left -= 1
            log.warning("[%s] 道闸指令 %s 无回执，重试(剩 %d)",
                        self.gate, self._ack_pending, self._retries_left)
            self._emit_cmd({"open": "open", "closed": "close", "stopped": "stop"}[self._ack_pending],
                           "retry", cmd_id=self._ack_cmd_id)
            self._ack_timer = self.cfg.barrier_ack_timeout_sec
            return
        # 重试耗尽 → 故障锁定（FR-9.4）
        self._clear_ack()
        self._enter(BarrierState.FAULT)
        log.error("[%s] 道闸故障锁定（指令无回执）", self.gate)
        self.bus.publish(IncidentRaised(
            kind="barrier", gate=self.gate,
            detail={"reason": "ack_timeout", "last_reason": self._last_reason}))

    # ----------------------------- 断连 / 恢复 -----------------------------
    def force_safe(self) -> None:
        """WS 断连：锁定 KEEP_OPEN（安全侧），暂停计时（§6.2）。"""
        self._manual = False
        self._authorization = "safety"
        self._clear_ack()
        if self.state != BarrierState.FAULT:
            self._enter(BarrierState.KEEP_OPEN)
            self._keep_timer = float("inf")   # 断连期间不落杆

    def reset(self) -> None:
        """启动/恢复：一律回 CLOSED（安全侧，§10.1）。"""
        self._manual = False
        self._authorization = "reset"
        self._clear_ack()
        self.zone_occupied = False
        self._keep_timer = 0.0
        self._close_delay = 0.0
        self.keep_open = False
        self.fault_mode = None
        self._enter(BarrierState.CLOSED)

    def restore(self, state: str, pending: int = 0, *, keep_open: bool = False) -> None:
        """Restore durable terminal states; uncertain motion requires manual recovery."""
        self._clear_ack()
        self.zone_occupied = False
        target = (BarrierState(state) if state in
                  {item.value for item in BarrierState} else BarrierState.FAULT)
        if target in (BarrierState.OPENING, BarrierState.CLOSING):
            target = BarrierState.FAULT
        self.state = target
        self.keep_open = keep_open
        self._keep_timer = self.cfg.keep_open_sec if target == BarrierState.KEEP_OPEN else 0

    def clear_fault(self) -> None:
        """Repair acknowledged by an operator: move to the closed limit via ack."""
        self.fault_mode = None
        if self.state == BarrierState.FAULT:
            self._clear_ack()
            self.keep_open = False
            self.state = BarrierState.STOPPED
            if self.zone_occupied:
                self.request_open("fault_cleared")
            else:
                self.request_close(manual=True, reason="fault_cleared")

    def inject_fault(self, mode: str) -> None:
        self.fault_mode = mode
        self._last_reason = mode
        if mode == "ack_timeout":
            self.request_open("fault_injection", force=True)
        else:
            self._clear_ack()
            self._emit_cmd("fault", mode)
            self._enter(BarrierState.FAULT)
            self.bus.publish(IncidentRaised(kind="barrier", gate=self.gate,
                                           detail={"reason": mode, "injected": True}))

    # ----------------------------- 内部 -----------------------------
    def _enter(self, new_state: BarrierState) -> None:
        if new_state == self.state:
            return
        self.state = new_state
        self._passable = new_state in (BarrierState.OPENING, BarrierState.KEEP_OPEN)
        self.bus.publish(BarrierStateChanged(gate=self.gate, state=new_state.value,
            cmd_id=self._ack_cmd_id, keep_open=self.keep_open, reason=self._last_reason,
            transient=new_state in (BarrierState.OPENING, BarrierState.CLOSING),
            manual=self._manual, passable=self._passable,
            authorization=self._authorization))

    def _emit_cmd(self, cmd: str, reason: str = "",
                  cmd_id: Optional[str] = None) -> str:
        cmd_id = cmd_id or uuid4().hex
        passable = cmd in ("open", "keep_open")
        self.bus.publish(BarrierCommand(gate=self.gate, cmd=cmd, reason=reason,
            cmd_id=cmd_id, target=("open" if reason == "stuck_open" else
                                  "closed" if reason == "stuck_closed" else cmd),
            duration_sec=self.cfg.barrier_close_sec if cmd == "close" else self.cfg.barrier_open_sec,
            manual=self._manual, passable=passable,
            authorization=self._authorization))
        return cmd_id

    def _begin_ack(self, expect: str, cmd_id: str) -> None:
        self._ack_pending = expect
        self._ack_cmd_id = cmd_id
        self._ack_timer = self.cfg.barrier_ack_timeout_sec
        self._retries_left = self.cfg.barrier_retry

    def _clear_ack(self) -> None:
        self._ack_pending = None
        self._ack_cmd_id = None
        self._ack_timer = 0.0
        self._retries_left = 0
