"""模块抽象接口（core/interfaces.py）。

面向接口编程：模块之间只通过这些 Protocol 与事件交互，禁止跨模块引用具体类
（低耦合，RR-4.2）。任一实现都可被 Mock 替换以便单独测试。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from .domain import FeeRule, RecognitionResult


@runtime_checkable
class IPlateRecognizer(Protocol):
    """车牌识别服务接口。识别输入是独立图像路径，与 3D 模型无关（FR-1.3）。"""

    def recognize(self, image_path: str) -> RecognitionResult: ...


@runtime_checkable
class IBarrierActuator(Protocol):
    """道闸指令通道（后端 → 前端仿真）。实际实现把指令经 WebSocket 下发。"""

    async def open(self, gate: str) -> None: ...
    async def close(self, gate: str) -> None: ...
    async def stop(self, gate: str) -> None: ...
    async def interrupt(self, gate: str) -> None: ...


@runtime_checkable
class IFeeRuleProvider(Protocol):
    """费率提供者：按时间点取生效的费率快照（FR-5.3）。"""

    def current_rule(self, at: Optional[datetime] = None) -> FeeRule: ...


@runtime_checkable
class IBroadcaster(Protocol):
    """实时广播接口：把业务消息推送给所有已连接的前端（3D 端 / 管理端）。"""

    async def broadcast(self, message: dict) -> None: ...
