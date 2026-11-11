"""摄像头（图像源）看门狗（recovery/watchdog.py）。

对应设计文档 §10.2（FR-9.2）。仿真模式下，摄像头故障由剧本注入（arrived 事件
不带图像或带损坏图）；真实模式下可由图像质量指标驱动。看门狗职责：

- 累计每闸**连续图像源失败**次数（无图/损坏图），达到
  ``cfg.recovery.camera_fail_streak`` → 通过 IncidentService 登记 camera 异常，
  该闸降级为人工录入（access 据 open 的 camera incident 判定降级）；
- 提供 ``inject_fault`` / ``recover`` 供剧本与管理员手动切换；
- ``status(gate)`` 返回健康状态（True=正常）。

真相源是"是否存在未闭环的 camera 异常"，避免看门狗与 incident 状态不一致。
"""
from __future__ import annotations

import logging

from .incident import IncidentService

log = logging.getLogger("recovery.watchdog")


class CameraWatchdog:
    def __init__(self, incident_service: IncidentService, *, fail_streak: int = 3):
        self.incidents = incident_service
        self.fail_streak = max(1, int(fail_streak))
        self._streak: dict[str, int] = {}

    # ----------------------------- 健康监测 -----------------------------
    def feed(self, gate: str, *, success: bool, image_present: bool = True,
             source_healthy: bool = True) -> None:
        """只对图像源不可用计数；低置信识别不是摄像头故障。"""
        if image_present and source_healthy:
            self._streak[gate] = 0
            return
        self._streak[gate] = self._streak.get(gate, 0) + 1
        log.info("摄像头[%s] 连续失败 %d/%d", gate, self._streak[gate], self.fail_streak)
        if self._streak[gate] >= self.fail_streak and not self.down(gate):
            self.incidents.raise_incident(
                "camera", gate,
                {"reason": "image_source_fail_streak",
                 "streak": self._streak[gate]})

    def status(self, gate: str) -> bool:
        """健康返回 True。"""
        return not self.down(gate)

    def details(self, gate: str) -> dict:
        open_incidents = self.incidents.list_open("camera", gate)
        return {"gate": gate, "down": bool(open_incidents),
                "source_fail_streak": self._streak.get(gate, 0),
                "fail_threshold": self.fail_streak,
                "incident_ids": [item["id"] for item in open_incidents]}

    def down(self, gate: str) -> bool:
        return self.incidents.has_open("camera", gate)

    # ----------------------------- 手动 / 剧本注入 -----------------------------
    def inject_fault(self, gate: str, reason: str = "scenario") -> None:
        """剧本注入摄像头故障。"""
        if not self.down(gate):
            self.incidents.raise_incident("camera", gate, {"reason": reason})
        self._streak[gate] = self.fail_streak

    def recover(self, gate: str, operator: str = "system",
                resolution: str = "摄像头恢复") -> None:
        """故障恢复：闭环所有该闸 camera 异常，清零失败计数。"""
        for item in self.incidents.list_open("camera", gate):
            self.incidents.resolve(item["id"], operator, resolution)
        self._streak[gate] = 0
        log.info("摄像头[%s] 已恢复", gate)
