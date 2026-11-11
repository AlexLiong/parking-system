"""进出场控制模块（access）—— 系统核心。

组件（低耦合，均通过 core/events 与 core/interfaces 交互）：
- ``BarrierController`` / ``TailgatingPolicy``：道闸状态机 + 防砸/跟车（§6）；
- ``SpaceAllocator``：车位分配策略（新能源优先 / C 区保留，§3.3）；
- ``ConsistencyGuard``：进出场幂等校验 + 启动对账（RR-2.4/FR-9.6）；
- ``AccessControlService``：编排进出场业务，裁决道闸许可（先落库后动作）。
"""
from .allocator import SpaceAllocator
from .barrier import BarrierController, TailgatingPolicy
from .guard import ConsistencyGuard
from .service import AccessControlService

__all__ = [
    "AccessControlService", "BarrierController", "TailgatingPolicy",
    "SpaceAllocator", "ConsistencyGuard",
]
