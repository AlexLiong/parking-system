"""异常处理与恢复模块（recovery）。

对应设计文档 §3.6 / §10（FR-9）：
- ``IncidentService``：异常事件登记—处理闭环—面板数据源，并落库其它模块的异常事件；
- ``CameraWatchdog``：图像源健康检查，连续失败降级为人工录入；
- ``StartupRecovery``：启动一致性恢复（半截交易/道闸复位/车位对账）。
"""
from .incident import IncidentService
from .startup import StartupRecovery
from .watchdog import CameraWatchdog

__all__ = ["IncidentService", "CameraWatchdog", "StartupRecovery"]
