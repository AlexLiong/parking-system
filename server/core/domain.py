"""跨模块共享的领域数据模型（core/domain.py）。

这些模型是模块之间、前后端之间的"数据契约"，用 pydantic 定义以便直接
序列化为 JSON（REST / WebSocket）。SQLAlchemy 的持久化模型在
``repository/models.py`` 中单独定义，二者由 repository / service 层互转，
避免 ORM 对象泄漏到接口边界。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_serializer

from .timeutil import iso_local


# ----------------------------- 枚举 -----------------------------
class PlateColor(str, Enum):
    BLUE = "blue"    # 蓝牌：7 位
    GREEN = "green"  # 绿牌（新能源）：8 位


class BarrierState(str, Enum):
    """道闸状态机状态（§6.1）。"""

    CLOSED = "CLOSED"
    OPENING = "OPENING"
    KEEP_OPEN = "KEEP_OPEN"
    CLOSING = "CLOSING"
    STOPPED = "STOPPED"
    FAULT = "FAULT"          # 故障锁定（FR-9.4）


class RecordStatus(str, Enum):
    IN = "in"
    OUT = "out"
    ABNORMAL = "abnormal"


class SpaceKind(str, Enum):
    NORMAL = "normal"
    NE_PREFER = "ne_prefer"  # 新能源优先区


class SpaceStatus(str, Enum):
    FREE = "free"
    OCCUPIED = "occupied"


class IncidentKind(str, Enum):
    POWER = "power"
    CAMERA = "camera"
    BARRIER = "barrier"
    DATA = "data"
    NETWORK = "network"


# ----------------------------- 车牌识别 -----------------------------
class PlateCandidate(BaseModel):
    """车牌定位候选区。"""

    x: int
    y: int
    w: int
    h: int
    angle: float = 0.0
    score: float = 0.0
    color: Optional[PlateColor] = None


class RecognitionResult(BaseModel):
    """识别模块对外唯一结果结构（core/interfaces.py 的 IPlateRecognizer 返回它）。"""

    plate: Optional[str] = None                 # 识别出的车牌号，失败为 None
    color: Optional[PlateColor] = None
    char_confs: list[float] = Field(default_factory=list)   # 逐字符置信度
    overall_conf: float = 0.0                   # 整牌综合置信度
    candidates: list[str] = Field(default_factory=list)      # top 候选（含易混变体）
    plate_crop_path: Optional[str] = None       # 车牌区域截图路径
    elapsed_ms: int = 0
    success: bool = False
    suspected: bool = False                     # 是否经仲裁后仍疑似（需人工确认）
    need_manual: bool = False                   # 是否需转人工
    message: str = ""
    source_status: str = "ok"                   # ok | unreadable | blank
    image_metrics: dict[str, float | bool] = Field(default_factory=dict)

    def to_log_dict(self) -> dict:
        return {
            "plate": self.plate,
            "color": self.color.value if self.color else None,
            "overall_conf": round(self.overall_conf, 4),
            "char_confs": [round(c, 4) for c in self.char_confs],
            "elapsed_ms": self.elapsed_ms,
            "success": self.success,
        }


class PlateContext(BaseModel):
    """识别的业务上下文，用于易混字符仲裁（RR-1.2）。"""

    gate: str = "in"                            # in / out
    in_lot_plates: list[str] = Field(default_factory=list)   # 当前在场车牌（出场匹配用）
    whitelist: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """规则校验 + 仲裁后的结果。"""

    plate: Optional[str] = None
    color: Optional[PlateColor] = None
    char_confs: list[float] = Field(default_factory=list)
    overall_conf: float = 0.0
    candidates: list[str] = Field(default_factory=list)
    valid: bool = False
    suspected: bool = False
    reason: str = ""


# ----------------------------- 计费 -----------------------------
class FeeRule(BaseModel):
    """收费标准（可含历史版本快照，FR-5.3）。"""

    id: Optional[int] = None
    effective_from: datetime
    free_minutes: int = 15
    first_hour_price: float = 5.0
    unit_minutes: int = 30
    unit_price: float = 2.0
    daily_cap: float = 40.0
    ne_discount: float = 0.9        # 绿牌折扣，1.0 表示不打折
    round_up: bool = True
    operator: Optional[str] = None
    note: Optional[str] = None

    @field_serializer("effective_from", when_used="json")
    def serialize_effective_from(self, value: datetime) -> str:
        return iso_local(value)


class FeeBreakdown(BaseModel):
    """计费明细。"""

    minutes: int = 0
    free_minutes_applied: int = 0
    billable_minutes: int = 0
    billable_units: int = 0
    unit_price: float = 0.0
    first_hour_price: float = 0.0
    daily_cap_applied: bool = False
    new_energy_discount: float = 1.0
    amount_due: float = 0.0
    detail: str = ""


class Quote(BaseModel):
    """出口报价（FR-3.2）。"""

    record_id: int
    plate: str
    plate_color: str
    entry_time: datetime
    exit_time: datetime
    minutes: int
    breakdown: FeeBreakdown
    amount_due: float
    entry_photo: Optional[str] = None
    exit_photo: Optional[str] = None
    space_no: Optional[str] = None
    paid: bool = False
    # 实现 B：演示模式按仿真时间计费，报价同时暴露仿真时长/字段（真实记录为 None）。
    duration_sec: Optional[float] = None
    entry_sim_sec: Optional[float] = None
    exit_sim_sec: Optional[float] = None
    duration_sim_sec: Optional[float] = None

    @field_serializer("entry_time", "exit_time", when_used="json")
    def serialize_time(self, value: datetime) -> str:
        return iso_local(value)


# ----------------------------- 车位 / 布局 -----------------------------
class ZoneDecl(BaseModel):
    """车位布局声明：管理员只填 区+数量+类型（其余有默认值）(§3.9)。"""

    zone: str                                  # A / B / C / D
    count: int
    kind: SpaceKind = SpaceKind.NORMAL
    anchor: Optional[tuple[float, float]] = None   # (x, z) 起始锚点，None 用默认
    columns: int = 10                          # 每排车位数
    rows: int = 2                              # 排数（count = columns * rows）
    spacing: Optional[float] = None            # 列间距，None 用车位宽
    heading: Optional[float] = None            # 车头朝向角（度，与 3D 场景一致），None 用默认


class SpaceSpec(BaseModel):
    """单个车位的完整规格（生成器产物 / 前端渲染依据）。"""

    space_no: str                              # "A-01"
    zone: str
    kind: SpaceKind = SpaceKind.NORMAL
    x: float
    z: float
    rotation: float = 0.0
    status: SpaceStatus = SpaceStatus.FREE
    disabled: bool = False
    current_plate: Optional[str] = None


class LayoutDiff(BaseModel):
    """布局变更摘要（广播给前端热更新）(§3.9)。"""

    version: int = 0
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    summary: str = ""


class LotLayout(BaseModel):
    """GET /api/layout 的完整返回：前端渲染的唯一依据。"""

    version: int = 0
    spaces: list[SpaceSpec] = Field(default_factory=list)
    zones: dict[str, int] = Field(default_factory=dict)     # 每区车位总数
    free: dict[str, int] = Field(default_factory=dict)      # 每区余位
    free_total: int = 0
    scene: dict = Field(default_factory=dict)               # 场景几何常量
    gates: dict = Field(default_factory=dict)               # 闸口坐标/停止线


# ----------------------------- 统计 -----------------------------
class DayStat(BaseModel):
    date: str
    count_in: int = 0
    count_out: int = 0
    revenue: float = 0.0


class PeriodStats(BaseModel):
    period: str                                # day/week/month/quarter/year
    anchor: str
    start: datetime
    end: datetime
    count_in: int = 0
    count_out: int = 0
    revenue: float = 0.0
    avg_minutes: float = 0.0
    peak_occupancy: int = 0
    by_day: list[DayStat] = Field(default_factory=list)
    generated_at: datetime

    @field_serializer("start", "end", "generated_at", when_used="json")
    def serialize_time(self, value: datetime) -> str:
        return iso_local(value)


# ----------------------------- 异常事件 -----------------------------
class Incident(BaseModel):
    id: Optional[int] = None
    kind: IncidentKind
    gate: Optional[str] = None
    detail: dict = Field(default_factory=dict)
    status: str = "open"
    ts: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None


class RecoveryReport(BaseModel):
    fixed_half_transactions: int = 0
    barrier_reset: list[str] = Field(default_factory=list)
    space_reconciled: int = 0
    messages: list[str] = Field(default_factory=list)
    recovered_at: datetime
