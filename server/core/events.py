"""进程内事件总线（core/events.py）。

架构总原则：**事件驱动 + 模块化**。各功能模块只依赖事件与抽象接口，
不互相 import 内部实现（低耦合，见设计文档 §2.3）。EventBus 同步分发，
handler 抛出的异常被隔离并记录日志，保证一个订阅者出错不影响其他订阅者。

事件是"已发生的事实"（过去式命名）；对外的实时推送由 gateway 的
Broadcaster 订阅这些事件后转成 WebSocket 消息，业务模块不直接碰网络。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable, Optional

from pydantic import BaseModel, Field

from .timeutil import utcnow as _utcnow

log = logging.getLogger("events")


class Event(BaseModel):
    """所有事件基类。"""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = Field(default_factory=_utcnow)
    type: str = ""

    def model_post_init(self, __context) -> None:  # pragma: no cover - 简单赋值
        if not self.type:
            self.type = self.__class__.__name__


# ----------------------------- 仿真端上报的事件 -----------------------------
class VehicleArrived(Event):
    """车辆到达入口/出口触发区（携带测试车牌图像路径，识别输入与 3D 模型解耦）。"""

    vehicle_id: str
    gate: str                                  # "in" / "out"
    image_path: Optional[str] = None           # 独立车牌图像；摄像头故障时可为空/损坏
    sim_ts: Optional[float] = None
    # 仿真 3D 相机截图识别（/sim/events/arrived-frame）：expected_plate 用于 access
    # 决策层"规范化后相等 + 置信度达标"放行；source 区分来源。
    expected_plate: Optional[str] = None
    source: str = "legacy"                      # legacy | sim_camera | upload


class VehiclePassedGate(Event):
    """车辆碰撞箱完全越过闸杆平面（FR-4.4），落杆前置条件之一。"""

    vehicle_id: str
    gate: str


class VehicleLeft(Event):
    """车辆驶离（出场完成 / 入场入库）。``plate`` 可选（H4 收尾兜底定位记录）。"""

    vehicle_id: str
    gate: str
    plate: Optional[str] = None


class ZoneOccupied(Event):
    """杆区（虚拟地感）占用状态变化（FR-4.3 防砸输入）。"""

    zone_id: str                               # "in_barrier" / "out_barrier"
    occupied: bool
    vehicle_id: Optional[str] = None
    owner_id: Optional[str] = None


# ----------------------------- 业务内部事件 -----------------------------
class PlateRecognized(Event):
    """识别完成（推送 3D 显示屏 / 管理端）。"""

    vehicle_id: Optional[str] = None
    gate: str = "in"
    plate: Optional[str] = None
    color: Optional[str] = None
    overall_conf: float = 0.0
    need_manual: bool = False
    candidates: list[str] = Field(default_factory=list)
    # 识别输入图片（FR-1.3）：本次识别实际使用的图像，与 3D 车身车牌渲染解耦。
    # image_path 保留原始路径供内部/审计；image_url/plate_crop_url 为前端可访问 URL。
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    plate_crop_url: Optional[str] = None
    image_source: str = ""                 # demo-lpr / datasets / upload / unknown
    image_synthetic: Optional[bool] = None  # True=合成样例；None=来源未声明
    source: str = ""                       # legacy | sim_camera | upload
    expected_plate: Optional[str] = None   # 仿真 3D 相机提供、用于 HUD 显示/审计
    plate_match: Optional[bool] = None     # 规范化后识别串是否等于 expected_plate
    reason: str = ""
    message: str = ""


class PendingCreated(Event):
    pending_id: int
    gate: str
    vehicle_id: Optional[str] = None
    reason: str
    source_plate: Optional[str] = None
    candidates: list[str] = Field(default_factory=list)
    expires_at: datetime


class PendingResolved(Event):
    pending_id: int
    gate: str
    vehicle_id: Optional[str] = None
    status: str
    plate: Optional[str] = None
    message: str = ""


class BarrierStateChanged(Event):
    """道闸状态机状态变化（同步给前端动画与管理端）。

    ``passable``：物理上是否可通行（OPENING/KEEP_OPEN）；``manual``：是否人工操作；
    ``authorization``：授权来源（entry/payment/manual/blacklist_bypass/safety/fault/keep_open）。
    注意：物理可通行 ≠ 业务已授权，3D/管理端需结合业务事件判断。
    """

    gate: str
    state: str
    cmd_id: Optional[str] = None
    transient: bool = False
    reason: str = ""
    keep_open: bool = False
    arm_position: Optional[float] = None
    manual: bool = False
    passable: bool = False
    authorization: str = ""


class SpaceAssigned(Event):
    """车位分配结果（下发仿真端导航）。"""

    vehicle_id: Optional[str] = None
    plate: str
    space_no: str
    zone: str


class SpaceReleased(Event):
    """车位释放（出场）。"""

    space_no: str


class FeeQuoted(Event):
    """出口报价生成（费用弹牌）。"""

    record_id: int
    vehicle_id: Optional[str] = None
    plate: str
    minutes: int
    amount: float
    entry_photo: Optional[str] = None
    exit_photo: Optional[str] = None


class EntryDenied(Event):
    """入口拒绝（黑名单等）：3D 端据此倒车离场或"进场后直接出场"，后端不建记录/不抬杆。

    ``mode``：blacklist（黑名单）/ denied（其它拒绝）；``gate`` 为 in/out；
    3D 可通过 /api/sim/blacklist-gate 操作闸杆完成物理绕行（不产生 parking_record）。
    """

    vehicle_id: Optional[str] = None
    gate: str = "in"
    plate: Optional[str] = None
    reason: str = "blacklist"
    mode: str = "blacklist"
    message: str = ""
    expected_plate: Optional[str] = None
    image_path: Optional[str] = None


class ExitAuthorized(Event):
    """人工授权出场（结算/免费/作废后）——3D 端据此驶离，道闸由 PaymentCompleted 流程打开。"""

    vehicle_id: Optional[str] = None
    plate: str
    record_id: Optional[int] = None
    action: str = "charge"
    amount_paid: Optional[float] = None
    space_no: Optional[str] = None
    reason: str = ""
    gate: str = "out"


class ArrivalRetryRequested(Event):
    """自动重试后车时旧帧仍不可用 → 请求 3D 端重新抓拍并上报 arrival（失败再待办）。"""

    vehicle_id: Optional[str] = None
    gate: str = "in"
    reason: str = "retry_failed"
    message: str = ""


class ManualAuthorized(Event):
    """人工授权通过（入口）：管理端/3D 手动放行后广播，便于前端同步车辆状态。"""

    vehicle_id: Optional[str] = None
    plate: str
    gate: str = "in"
    record_id: Optional[int] = None
    space_no: Optional[str] = None
    action: str = "entry"
    reason: str = ""


class PaymentCompleted(Event):
    """缴费完成（触发自动抬杆）。"""

    record_id: int
    plate: str
    amount: float
    method: str
    vehicle_id: Optional[str] = None


class SpaceUpdate(Event):
    """余位变化（推送余位屏 / 管理端）。"""

    free_total: int
    by_zone: dict[str, int] = Field(default_factory=dict)
    recommend: Optional[str] = None


class LayoutChanged(Event):
    """布局变更（前端收到后重拉 /api/layout 热更新）(§3.9)。"""

    version: int
    diff: str = ""


class IncidentRaised(Event):
    """异常事件登记（FR-9）。"""

    kind: str
    gate: Optional[str] = None
    detail: dict = Field(default_factory=dict)
    incident_id: Optional[int] = None


class IncidentResolved(Event):
    kind: str
    incident_id: int
    resolution: str = ""


class BarrierCommand(Event):
    """后端 → 前端道闸指令（open/close/stop/interrupt）(§6)。

    ``manual``/``passable``/``authorization`` 语义同 ``BarrierStateChanged``：人工抬杆
    （manual=True, authorization=manual/blacklist_bypass）只代表物理可通行，业务授权
    仍以业务事件（space_assigned/payment_completed/exit_authorized 等）为准。
    """

    gate: str
    cmd: str
    reason: str = ""
    cmd_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    target: str = ""
    duration_sec: float = 1.5
    manual: bool = False
    passable: bool = False
    authorization: str = ""


# ----------------------------- 事件总线 -----------------------------
Handler = Callable[[Event], None]


class SimulationChanged(Event):
    paused: bool
    speed: float
    sim_time: float
    revision: int
    enabled: bool
    business_time: Optional[str] = None
    speed_min: float = 0.25
    speed_max: float = 10.0


class EventBus:
    """极简进程内 pub/sub。

    - ``subscribe(event_type, handler)``：订阅某事件类型（含其子类，便于网关统一转发）；
    - ``publish(event)``：同步分发给所有匹配 handler，异常隔离并记日志；
    - 支持 ``subscribe_all`` 供网关做"全量转发到 WebSocket"。
    """

    def __init__(self) -> None:
        self._subs: dict[type, list[Handler]] = {}
        self._all_subs: list[Handler] = []
        self.outbound_handler: Optional[Handler] = None
        self._critical: set[Handler] = set()

    def subscribe(self, event_type: type[Event], handler: Handler, *, critical=False) -> None:
        self._subs.setdefault(event_type, []).append(handler)
        if critical:
            self._critical.add(handler)

    def unsubscribe(self, event_type: type[Event], handler: Handler) -> None:
        """移除某个订阅（用于端点内一次性收集事件，避免影响全局订阅）。"""
        handlers = self._subs.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)
        self._critical.discard(handler)

    def subscribe_all(self, handler: Handler) -> None:
        """订阅所有事件（网关用它把事件转成 WS 广播）。"""
        self._all_subs.append(handler)

    def unsubscribe_all(self) -> None:
        self._subs.clear()
        self._all_subs.clear()
        self._critical.clear()

    def publish(self, event: Event) -> None:
        if self.outbound_handler is not None:
            self.outbound_handler(event)
        # 精确类型 + 父类型匹配
        targets: list[Handler] = list(self._all_subs)
        for etype, handlers in self._subs.items():
            if isinstance(event, etype):
                targets.extend(handlers)
        for h in targets:
            try:
                h(event)
            except Exception:  # noqa: BLE001 - 事件分发必须隔离 handler 异常
                if h in self._critical:
                    raise
                log.exception("事件 handler 执行失败: type=%s handler=%s",
                              event.type, getattr(h, "__name__", h))
