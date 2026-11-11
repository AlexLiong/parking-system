"""领域异常定义（core/errors.py）。

约定：所有服务层方法在业务失败时抛出本模块中的领域异常，由接入层
（gateway）统一转换为 HTTP / WebSocket 错误消息。异常携带机器可读的
``code`` 与建议的 HTTP 状态码，避免把 Python 栈直接暴露给前端。
"""
from __future__ import annotations


class ParkingError(Exception):
    """所有领域异常的基类。"""

    code: str = "error"
    http_status: int = 400

    def __init__(self, message: str = "", **detail):
        self.message = message or self.__class__.__doc__ or self.code
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "detail": self.detail}


# ----------------------------- 认证 / 权限 -----------------------------
class AuthError(ParkingError):
    """登录失败或令牌无效。"""

    code = "auth_error"
    http_status = 401


class PermissionDenied(ParkingError):
    """已登录但无权限执行该操作。"""

    code = "permission_denied"
    http_status = 403


# ----------------------------- 通用 -----------------------------
class NotFound(ParkingError):
    """请求的资源不存在。"""

    code = "not_found"
    http_status = 404


class ValidationError(ParkingError):
    """入参校验失败。"""

    code = "validation_error"
    http_status = 422


# ----------------------------- 车牌识别 -----------------------------
class RecognitionError(ParkingError):
    """识别流程本身出错（图像无法读取、定位失败等）。"""

    code = "recognition_error"
    http_status = 422


class LowConfidenceError(ParkingError):
    """识别置信度低于阈值，需转人工确认（FR-1.6 / FR-9.3）。

    注意：这是"业务流程分支"而非系统故障，接入层通常返回 200 + 需人工标记，
    这里保留异常类型以便服务层内部传递候选结果。
    """

    code = "low_confidence"
    http_status = 200

    def __init__(self, message: str = "", candidates=None, result=None):
        super().__init__(message, candidates=candidates or [])
        self.result = result


# ----------------------------- 进出场 / 数据一致性 -----------------------------
class DataAnomaly(ParkingError):
    """数据异常：重复进场、出场无在场记录、时间倒挂等（FR-9.6）。

    一律转人工处理队列，禁止静默放行/扣费。
    """

    code = "data_anomaly"
    http_status = 409

    def __init__(self, message: str = "", kind: str = "data", **detail):
        super().__init__(message, **detail)
        self.kind = kind


class StateConflict(DataAnomaly):
    """会话同步冲突，错误码与可恢复的冲突原因一致。"""

    def __init__(self, kind: str, message: str, **detail):
        super().__init__(message, kind=kind, **detail)
        self.code = kind


class DuplicateEntryError(DataAnomaly):
    """同一车牌已有未出场记录，禁止重复进场（FR-2.2）。"""

    code = "duplicate_entry"

    def __init__(self, plate: str):
        super().__init__(f"车牌 {plate} 已有未出场记录", kind="duplicate_entry", plate=plate)
        self.plate = plate


class NoParkingRecordError(DataAnomaly):
    """出口识别到的车牌无对应在场记录（FR-3.6）。"""

    code = "no_parking_record"

    def __init__(self, plate: str):
        super().__init__(f"车牌 {plate} 无在场记录", kind="no_record", plate=plate)
        self.plate = plate


class LotFullError(ParkingError):
    """停车场满位，入口不予抬杆（FR-2.3）。"""

    code = "lot_full"
    http_status = 409


# ----------------------------- 设备 / 异常工作流 -----------------------------
class CameraFault(ParkingError):
    """摄像头（图像源）故障，需降级为人工录入（FR-9.2）。"""

    code = "camera_fault"
    http_status = 503


class BarrierFault(ParkingError):
    """道闸故障：指令超时未到位（FR-9.4）。"""

    code = "barrier_fault"
    http_status = 503


class LayoutConflict(ParkingError):
    """布局变更安全校验失败（如停用被占车位 / 缩减后小于在场数）(FR-2.6)。"""

    code = "layout_conflict"
    http_status = 409
