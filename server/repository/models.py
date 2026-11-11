"""SQLAlchemy ORM 模型（repository/models.py）——对应设计文档 §9 的建表 DDL。

约定：
- 所有 DateTime 存 naive UTC（见 core/timeutil.py）；
- 布尔用 SQLite 兼容的 Boolean；
- 索引按查询模式建立（车牌+状态、进场时间等）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Float, Index, Integer, String,
                        Text, UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ParkingRecord(Base):
    """进出/在场记录（核心实体）。"""

    __tablename__ = "parking_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plate: Mapped[str] = mapped_column(String(16), nullable=False)
    plate_color: Mapped[str] = mapped_column(String(8), nullable=False)   # blue/green
    vehicle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    space_no: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in")  # in/out/abnormal
    entry_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    exit_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    entry_plate_crop: Mapped[str | None] = mapped_column(String(256), nullable=True)
    exit_plate_crop: Mapped[str | None] = mapped_column(String(256), nullable=True)
    manual_entry: Mapped[bool] = mapped_column(Boolean, default=False)
    fee_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 进场费率快照
    benefit_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    temporary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    aborted_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 演示业务时间（实现 B）：entry_time/exit_time 在演示模式按仿真时钟给出业务时间，
    # created_at 永远是真实墙钟；以下为原始仿真秒，供 API/统计/审计直接读取。
    entry_sim_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_sim_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_sim_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("idx_plate_status", "plate", "status"),
        Index("idx_entry_time", "entry_time"),
        # P1-13：出口统计/按天出场投影按 exit_time 过滤；无索引时 100k 行基准为全表扫描。
        Index("idx_exit_time", "exit_time"),
        # P1-13：覆盖索引——统计基准的区间扫描只读索引即可完成（免回表）：
        #   idx_entry_cover：进出场峰值扫描线/按天进场计数（entry_time 范围 + exit_time/temporary）
        #   idx_exit_cover ：出场次数/平均时长/按天出场计数/峰值扫描线
        Index("idx_entry_cover", "entry_time", "exit_time", "temporary"),
        Index("idx_exit_cover", "exit_time", "entry_time", "temporary"),
    )


class PaymentRecord(Base):
    """收费记录（一单一付，幂等）。"""

    __tablename__ = "payment_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parking_record_id: Mapped[int] = mapped_column(Integer, nullable=False)
    plate: Mapped[str] = mapped_column(String(16), nullable=False)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billable_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_due: Mapped[float] = mapped_column(Float, nullable=False)
    amount_paid: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)  # mock_scan/cash/free/monthly
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)     # FeeBreakdown JSON
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint("parking_record_id", name="uq_payment_parking"),
        # P1-13：营收/按天营收按 paid_at 区间过滤；无索引时 100k 行基准为全表扫描。
        # 覆盖索引（paid_at, amount_paid）让 SUM 区间聚合免回表。
        Index("idx_paid_at", "paid_at"),
        Index("idx_paid_at_cover", "paid_at", "amount_paid"),
    )


class FeeRule(Base):
    """收费标准（含历史版本，按 effective_from 取快照）。"""

    __tablename__ = "fee_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # N2：版本创建时间（旧库可空，读取时回退 effective_from）
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    free_minutes: Mapped[int] = mapped_column(Integer, default=15)
    first_hour_price: Mapped[float] = mapped_column(Float, default=5.0)
    unit_minutes: Mapped[int] = mapped_column(Integer, default=30)
    unit_price: Mapped[float] = mapped_column(Float, default=2.0)
    daily_cap: Mapped[float] = mapped_column(Float, default=40.0)
    ne_discount: Mapped[float] = mapped_column(Float, default=0.9)
    round_up: Mapped[bool] = mapped_column(Boolean, default=True)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (Index("idx_fee_effective", "effective_from"),)


class ParkingSpace(Base):
    """车位（布局唯一事实源，§3.9）。"""

    __tablename__ = "parking_space"

    space_no: Mapped[str] = mapped_column(String(16), primary_key=True)  # "A-01"
    zone: Mapped[str] = mapped_column(String(4), nullable=False)         # A/B/C/D
    kind: Mapped[str] = mapped_column(String(16), default="normal")      # normal/ne_prefer
    x: Mapped[float] = mapped_column(Float, default=0.0)
    z: Mapped[float] = mapped_column(Float, default=0.0)
    rotation: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="free")      # free/occupied
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)       # 软删除
    current_plate: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (Index("idx_space_zone_status", "zone", "status", "disabled"),)


class LayoutHistory(Base):
    """布局变更留痕。"""

    __tablename__ = "layout_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decl: Mapped[str | None] = mapped_column(Text, nullable=True)   # 声明内容 JSON
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)   # 变更摘要 JSON
    version: Mapped[int] = mapped_column(Integer, default=1)


class RecognitionLog(Base):
    """识别日志（FR-1.5，全链路留痕 RR-4.3）。"""

    __tablename__ = "recognition_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    gate: Mapped[str | None] = mapped_column(String(8), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    plate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    plate_color: Mapped[str | None] = mapped_column(String(8), nullable=True)
    char_confs: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 数组
    overall_conf: Mapped[float | None] = mapped_column(Float, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    corrected_plate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("idx_recog_ts", "ts"),)


class PendingManual(Base):
    """Durable recognition review tied to a demo session and vehicle."""

    __tablename__ = "pending_manual"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gate: Mapped[str] = mapped_column(String(8), nullable=False)
    vehicle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    candidates_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_plate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    plate_color: Mapped[str | None] = mapped_column(String(8), nullable=True)
    recognition_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 演示模式：待办超时以仿真秒计（sim_deadline_sec）；expires_at 仍保留真实墙钟兜底。
    sim_deadline_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed_plate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(256), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_pending_status_expiry", "status", "expires_at"),
        Index("uq_pending_vehicle_open", "session_id", "gate", "vehicle_id",
              unique=True, sqlite_where=text("status = 'pending' AND vehicle_id IS NOT NULL")),
    )


class IncidentLog(Base):
    """异常事件日志（FR-9）。"""

    __tablename__ = "incident_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # power/camera/barrier/data/network
    gate: Mapped[str | None] = mapped_column(String(8), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    status: Mapped[str] = mapped_column(String(16), default="open")  # open/resolved
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("idx_incident_status", "status"),)


class AdminUser(Base):
    """管理员账号。"""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="admin")


class RevokedToken(Base):
    __tablename__ = "revoked_token"
    jti: Mapped[str] = mapped_column(String(32), primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)


class VehicleList(Base):
    """白/黑/月租名单（加分项 FR-10.2/10.3）。"""

    __tablename__ = "vehicle_list"

    plate: Mapped[str] = mapped_column(String(16), primary_key=True)
    list_type: Mapped[str] = mapped_column(String(16), nullable=False)  # white/black/monthly
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)


class OpLog(Base):
    """管理员操作日志（费率/布局/手动干预等留痕，RR-4.3）。"""

    __tablename__ = "op_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DemoSession(Base):
    __tablename__ = "demo_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)


class EventLog(Base):
    __tablename__ = "event_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_event_session_seq", "session_id", "seq"),)


class ResetRequest(Base):
    __tablename__ = "demo_reset_request"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_body: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)


class BarrierStateRow(Base):
    __tablename__ = "barrier_state"

    gate: Mapped[str] = mapped_column(String(8), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    arm_position: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    keep_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_cmd_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fault: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class BarrierAction(Base):
    __tablename__ = "barrier_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cmd_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    gate: Mapped[str] = mapped_column(String(8), nullable=False)
    cmd: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class VehicleRuntime(Base):
    __tablename__ = "vehicle_runtime"

    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


ALL_MODELS = [
    ParkingRecord, PaymentRecord, FeeRule, ParkingSpace, LayoutHistory,
    RecognitionLog, PendingManual, IncidentLog, AdminUser, VehicleList, OpLog,
    DemoSession, EventLog, ResetRequest, BarrierStateRow, BarrierAction,
    VehicleRuntime,
]
