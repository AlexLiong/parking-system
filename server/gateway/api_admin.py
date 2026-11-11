"""管理端 REST（gateway/api_admin.py）。

对应设计文档 §11.1。覆盖管理端全部后端能力：登录鉴权、费率版本、进出记录查询、
周期统计与 PDF 导出、异常面板、人工干预（抬杆/录入/放行）、缴费结算、摄像头
故障注入与恢复、名单管理、实时总览。

约定：
- 除登录外，所有写操作都经 ``require_admin`` 校验 JWT；
- 服务层方法自行管理事务（先落库后动作），路由**不再包一层 db.scope**；
- 领域异常由 ``main`` 的全局处理器统一转 HTTP，路由内只处理 HTTP 语义。
"""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, HTTPException
import time
from collections import defaultdict, deque
import threading
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from ..core.errors import NotFound, StateConflict, ValidationError
from ..core.timeutil import iso_local, parse_local_iso, utcnow
from ..lpr.charset import normalize_valid_plate
from .auth import login as do_login
from .auth import require_admin
from .context import Container, get_container

log = logging.getLogger("gateway.admin")
router = APIRouter(tags=["admin"])


def _input_time(value: Optional[str], field: str):
    parsed = parse_local_iso(value)
    if value and parsed is None:
        raise ValidationError(f"{field} 必须为 ISO 日期时间")
    return parsed


def _sim_input(sim_sec: float | None, sim_min: float | None) -> float | None:
    """仿真秒/仿真分钟二选一 → 仿真秒；都为空返回 None（实现 B）。"""
    if sim_sec is not None and sim_min is not None:
        raise ValidationError("entry_sim_sec 与 entry_sim_min 只能提供一个")
    if sim_sec is not None:
        return float(sim_sec)
    if sim_min is not None:
        return round(float(sim_min) * 60.0, 3)
    return None


# ----------------------------- 认证 -----------------------------
class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response,
               c: Container = Depends(get_container)) -> dict:
    key = request.client.host if request.client else "local"
    with c.login_lock:
        now = time.monotonic()
        if len(c.login_attempts)>10000:
            c.login_attempts.clear()
        attempts = c.login_attempts[key]
        while attempts and attempts[0]<now-60:
            attempts.popleft()
        if len(attempts)>=10:
            raise HTTPException(429,"登录尝试过多，请稍后重试")
        attempts.append(now)
    result = do_login(body.username, body.password, c)
    with c.login_lock:
        c.login_attempts.pop(key,None)
    response.set_cookie('parking_session',result['token'],httponly=True,samesite='strict',
                        secure=request.url.scheme=='https',max_age=result['expires_in'])
    return result


@router.get('/api/public-config')
def public_config(c: Container = Depends(get_container)):
    return {'demo_mode':c.cfg.server.demo_mode,'timezone':c.cfg.server.timezone}


@router.post('/api/auth/demo-session')
def demo_session(c: Container = Depends(get_container)):
    if not c.cfg.server.demo_mode:
        raise HTTPException(403,'演示会话已关闭，请登录管理端')
    from .auth import create_token
    return {'token':create_token('simulator','operator',c.cfg)}


@router.post('/api/auth/logout')
def logout(response: Response, admin: dict = Depends(require_admin),
           c: Container = Depends(get_container)):
    from ..repository.models import RevokedToken
    with c.db.scope():
        if c.db.session.get(RevokedToken,admin['jti']) is None:
            c.db.session.add(RevokedToken(jti=admin['jti'],expires_at=admin['exp']))
    response.delete_cookie('parking_session')
    return {'ok':True}


@router.get("/api/auth/me")
def auth_me(admin: dict = Depends(require_admin)) -> dict:
    return {"username": admin.get("sub"), "role": admin.get("role")}


# ----------------------------- 费率版本 -----------------------------
class FeeRuleIn(BaseModel):
    free_minutes: int = Field(15, ge=0)
    first_hour_price: float = Field(5.0, ge=0)
    unit_minutes: int = Field(30, ge=1)
    unit_price: float = Field(2.0, ge=0)
    daily_cap: float = Field(40.0, ge=0)
    ne_discount: float = Field(0.9, gt=0, le=1.0)
    round_up: bool = True
    note: str = ""
    effective_from: Optional[str] = None


@router.get("/api/fee-rules")
def list_fee_rules(c: Container = Depends(get_container)) -> dict:
    return {"items": c.billing.list_rules()}


@router.get("/api/fee-rules/current")
def current_fee_rule(c: Container = Depends(get_container)) -> dict:
    return c.billing.current_rule().model_dump(mode="json")


@router.put("/api/fee-rules/current")
def update_current_fee_rule(body: FeeRuleIn, admin: dict = Depends(require_admin),
                            c: Container = Depends(get_container)) -> dict:
    """编辑当前生效费率（N2）：内部生成新版本（历史保留），返回当前版本。"""
    current = c.billing.current_rule()
    same = (int(current.free_minutes) == int(body.free_minutes)
            and float(current.first_hour_price) == float(body.first_hour_price)
            and int(current.unit_minutes) == int(body.unit_minutes)
            and float(current.unit_price) == float(body.unit_price)
            and float(current.daily_cap) == float(body.daily_cap)
            and float(current.ne_discount) == float(body.ne_discount)
            and bool(current.round_up) == bool(body.round_up))
    if same:
        # 内容未变化：不制造无意义版本，直接返回当前版本
        view = c.billing.current_rule_view()
        if view is not None:
            return view
    c.billing.add_rule(
        free_minutes=body.free_minutes, first_hour_price=body.first_hour_price,
        unit_minutes=body.unit_minutes, unit_price=body.unit_price,
        daily_cap=body.daily_cap, ne_discount=body.ne_discount,
        round_up=body.round_up, operator=admin.get("sub", "admin"),
        note=body.note or f"编辑当前费率（来自 #{current.id}）",
        effective_from=utcnow())
    return c.billing.current_rule_view() or c.billing.current_rule().model_dump(mode="json")


@router.post("/api/fee-rules")
def add_fee_rule(body: FeeRuleIn, admin: dict = Depends(require_admin),
                 c: Container = Depends(get_container)) -> dict:
    """新增费率版本（旧记录仍按各自进场快照计费，FR-5.3）。"""
    eff = _input_time(body.effective_from, "生效时间")
    rule = c.billing.add_rule(
        free_minutes=body.free_minutes, first_hour_price=body.first_hour_price,
        unit_minutes=body.unit_minutes, unit_price=body.unit_price,
        daily_cap=body.daily_cap, ne_discount=body.ne_discount,
        round_up=body.round_up, operator=admin.get("sub", "admin"),
        note=body.note, effective_from=eff)
    return rule.model_dump(mode="json")


@router.put("/api/fee-rules/{rule_id}")
def replace_fee_rule(rule_id: int, body: FeeRuleIn,
                     admin: dict = Depends(require_admin),
                     c: Container = Depends(get_container)) -> dict:
    """费率快照不可变：PUT 语义 = 以现有版本为基础，新增一个"从现在生效"的版本。

    这样既满足管理端"修改费率"的直觉，又保证历史在场车辆的计费口径不变。
    """
    old = c.fee_repo.get(rule_id)
    if old is None:
        raise NotFound(f"费率版本 #{rule_id} 不存在")
    rule = c.billing.add_rule(
        free_minutes=body.free_minutes, first_hour_price=body.first_hour_price,
        unit_minutes=body.unit_minutes, unit_price=body.unit_price,
        daily_cap=body.daily_cap, ne_discount=body.ne_discount,
        round_up=body.round_up, operator=admin.get("sub", "admin"),
        note=body.note or f"基于 #{rule_id} 修订", effective_from=utcnow())
    return rule.model_dump(mode="json")


# ----------------------------- 记录 / 统计 / 报表 -----------------------------
@router.get("/api/records")
def search_records(plate: Optional[str] = Query(None),
                   start: Optional[str] = Query(None),
                   end: Optional[str] = Query(None),
                   status: Optional[str] = Query(None),
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=200),
                   c: Container = Depends(get_container)) -> dict:
    return c.stats.search_records(
        plate=plate, start=_input_time(start, "起始时间"),
        end=_input_time(end, "结束时间"), status=status,
        page=page, page_size=page_size)


@router.get("/api/records/{record_id}")
def get_record(record_id: int, c: Container = Depends(get_container)) -> dict:
    from ..repository.parking_repo import ParkingRecordRepo
    rec = c.parking_repo.get(record_id)
    if rec is None:
        raise NotFound(f"记录 #{record_id} 不存在")
    view = ParkingRecordRepo.to_view(rec)
    pay = c.payment_repo.get_by_parking(record_id)
    view["payment"] = None if pay is None else {
        "id": pay.id, "amount_paid": pay.amount_paid, "amount_due": pay.amount_due,
        "minutes": pay.minutes, "method": pay.method, "operator": pay.operator,
        "paid_at": iso_local(pay.paid_at),
        "breakdown": c.payment_repo.breakdown_of(pay),
    }
    return view


@router.get("/api/quote")
def quote(plate: str = Query(...), c: Container = Depends(get_container)) -> dict:
    """按车牌实时报价（出口显示屏 / 管理端结算前预览）。"""
    return c.billing.quote(plate).model_dump(mode="json")


@router.get("/api/stats")
def get_stats(period: str = Query("day"), anchor: Optional[str] = Query(None),
              c: Container = Depends(get_container)) -> dict:
    if period not in ("day", "week", "month", "quarter", "year"):
        raise ValidationError("period 应为 day/week/month/quarter/year")
    return c.stats.stats(period, anchor).model_dump(mode="json")


REPORT_PERIODS = ("day", "week", "month", "quarter", "year")


def _report_preview(c: Container, period: str, anchor: Optional[str],
                    limit: int = 500) -> dict:
    if period not in REPORT_PERIODS:
        raise ValidationError("period 应为 day/week/month/quarter/year")
    try:
        return c.reports.preview(period, anchor, vehicle_limit=limit)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@router.get("/api/reports/preview")
def report_preview(period: str = Query("day"), anchor: Optional[str] = Query(None),
                   limit: int = Query(500, ge=1, le=2000),
                   c: Container = Depends(get_container)) -> dict:
    """报表预览（N2）：只统计完整记录；day/week 有车辆明细，month 周汇总，quarter/year 月汇总。"""
    return _report_preview(c, period, anchor, limit)


@router.get("/api/reports/export.csv")
def export_report_csv(period: str = Query("day"), anchor: Optional[str] = Query(None),
                      lang: Literal["zh", "en"] = Query("zh"),
                      c: Container = Depends(get_container)):
    """导出 CSV（lang=zh|en 表头/支付方式/文件名 _cn/_en；公开下载，无需 Bearer）。"""
    preview = _report_preview(c, period, anchor, limit=2000)
    text = c.reports.csv_text(preview, lang=lang)
    fname = c.reports.filename(period, preview, lang, ext="csv")
    return Response(content=text.encode("utf-8-sig"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{fname}"'})


def _cleanup_report_file(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - 清理失败不影响已发送响应
        logging.getLogger("gateway.admin").exception("清理临时报表失败: %s", path)


@router.get("/api/reports/export")
def export_report(period: str = Query("day"), anchor: Optional[str] = Query(None),
                  lang: Literal["zh", "en"] = Query("zh"),
                  c: Container = Depends(get_container)):
    """生成并下载 PDF 报表（lang=zh|en；M7：唯一临时文件 + 原子 rename + 响应后清理）。"""
    preview = _report_preview(c, period, anchor, limit=2000)
    fname = c.reports.filename(period, preview, lang, ext="pdf")
    reports_dir = Path(c.cfg.paths.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".report_", suffix=".pdf.tmp",
                                    dir=str(reports_dir))
    os.close(fd)
    suffix = "en" if lang == "en" else "cn"
    final_path = (reports_dir /
                  f"report_{period}_{preview['anchor']}_{suffix}_{uuid.uuid4().hex}.pdf")
    try:
        c.pdf.generate_from_preview(preview, tmp_name, lang=lang)
        os.replace(tmp_name, final_path)      # 同一目录原子 rename
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return FileResponse(str(final_path), media_type="application/pdf",
                        filename=fname,
                        background=BackgroundTask(_cleanup_report_file,
                                                  final_path))


# ----------------------------- 异常面板 -----------------------------
@router.get("/api/incidents")
def list_incidents(status: Optional[str] = Query(None),
                   limit: int = Query(100, ge=1, le=500),
                   c: Container = Depends(get_container)) -> dict:
    return {"items": c.incidents.list(status=status, limit=limit)}


class ResolveIn(BaseModel):
    resolution: str = ""


@router.post("/api/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: int, body: ResolveIn,
                     admin: dict = Depends(require_admin),
                     c: Container = Depends(get_container)) -> dict:
    inc = c.incidents.resolve(incident_id, operator=admin.get("sub", "admin"),
                              resolution=body.resolution or "管理员已处理")
    if inc is None:
        raise NotFound(f"异常事件 #{incident_id} 不存在")
    return c.incidents.get(incident_id) or {"id": incident_id, "status": "resolved"}


# ----------------------------- 人工干预 -----------------------------
@router.get("/api/manual/pending")
def manual_pending(status: str = Query("pending"),
                   admin: dict = Depends(require_admin),
                   c: Container = Depends(get_container)) -> dict:
    return {"items": c.access.list_pending(status)}


class ConfirmPendingIn(BaseModel):
    id: int = Field(..., ge=1)
    plate: str
    exit_action: Literal['max_duration','free','void','manual_time'] | None = None
    reason: str = ""
    entry_time: str | None = None
    entry_sim_sec: float | None = Field(None, ge=0, allow_inf_nan=False,
                                        description="仿真秒入场时间（演示模式）")
    entry_sim_min: float | None = Field(None, ge=0, allow_inf_nan=False,
                                        description="仿真分钟入场时间（演示模式）")


@router.post("/api/manual/confirm")
def manual_confirm(body: ConfirmPendingIn, admin: dict = Depends(require_admin),
                   c: Container = Depends(get_container)) -> dict:
    return c.access.confirm_pending(body.id, body.plate,
                                    operator=admin.get("sub", "admin"), exit_action=body.exit_action,
                                    reason=body.reason, entry_time=_input_time(body.entry_time,"入场时间"),
                                    entry_sim_sec=_sim_input(body.entry_sim_sec, body.entry_sim_min))


class RejectPendingIn(BaseModel):
    id: int = Field(..., ge=1)
    reason: str = ""


@router.post("/api/manual/reject")
def manual_reject(body: RejectPendingIn, admin: dict = Depends(require_admin),
                  c: Container = Depends(get_container)) -> dict:
    return c.access.reject_pending(body.id, operator=admin.get("sub", "admin"),
                                   reason=body.reason)


class OpenBarrierIn(BaseModel):
    gate: str = Field(..., description="in | out")
    reason: str = "manual"


@router.post("/api/manual/open-barrier")
def manual_open(body: OpenBarrierIn, admin: dict = Depends(require_admin),
                c: Container = Depends(get_container)) -> dict:
    """仅物理抬杆（不建记录/不派车）；需要业务放行请用 /api/manual/authorize-*。"""
    return c.access.manual_open(body.gate, operator=admin.get("sub", "admin"),
                         reason=body.reason)


@router.post("/api/manual/close-barrier")
def manual_close(body: OpenBarrierIn, admin: dict = Depends(require_admin),
                 c: Container = Depends(get_container)) -> dict:
    return c.access.operate_barrier(body.gate, "close", operator=admin["sub"], reason=body.reason)


@router.post("/api/manual/stop-barrier")
def manual_stop(body: OpenBarrierIn, admin: dict = Depends(require_admin),
                c: Container = Depends(get_container)) -> dict:
    return c.access.operate_barrier(body.gate, "stop", operator=admin["sub"], reason=body.reason)


class KeepOpenIn(BaseModel):
    enabled: bool


@router.post("/api/barrier/{gate}/keep-open")
def keep_open(gate: str, body: KeepOpenIn, admin: dict = Depends(require_admin),
              c: Container = Depends(get_container)) -> dict:
    return c.access.operate_barrier(gate, "keep_open", enabled=body.enabled, operator=admin["sub"])


@router.post("/api/barrier/{gate}/clear-fault")
def clear_fault(gate: str, admin: dict = Depends(require_admin),
                c: Container = Depends(get_container)) -> dict:
    return c.access.operate_barrier(gate, "clear_fault", operator=admin["sub"])


class BarrierFaultIn(BaseModel):
    action: str = "inject"
    mode: str = "ack_timeout"


@router.post("/api/barrier/{gate}/fault")
def barrier_fault(gate: str, body: BarrierFaultIn, admin: dict = Depends(require_admin),
                  c: Container = Depends(get_container)) -> dict:
    if body.action not in ("inject", "clear"):
        raise ValidationError("action 应为 inject 或 clear")
    return c.access.operate_barrier(gate, "inject" if body.action == "inject" else "clear_fault",
                                    mode=body.mode, operator=admin["sub"])


class RegisterIn(BaseModel):
    plate: str
    color: str = "blue"


@router.post("/api/manual/register")
def manual_register(body: RegisterIn, admin: dict = Depends(require_admin),
                    c: Container = Depends(get_container)) -> dict:
    """人工录入车牌并授权进场（摄像头/识别故障降级，FR-9.2）。"""
    plate = body.plate.strip().upper()
    if not plate:
        raise ValidationError("车牌不能为空")
    return c.access.manual_register(plate, gate="in",
                                    operator=admin.get("sub", "admin"),
                                    color=body.color)


class AuthorizeEntryIn(BaseModel):
    """人工授权入场：建记录/分配车位/发 SpaceAssigned/开入口（不依赖识别）。"""

    vehicle_id: str = Field(..., min_length=1, max_length=64)
    plate: str = Field(..., min_length=1, max_length=12)
    color: str = "blue"


@router.post("/api/manual/authorize-entry")
def manual_authorize_entry(body: AuthorizeEntryIn,
                           admin: dict = Depends(require_admin),
                           c: Container = Depends(get_container)) -> dict:
    plate = body.plate.strip().upper()
    if not plate:
        raise ValidationError("车牌不能为空")
    return c.access.authorize_entry(body.vehicle_id, plate, color=body.color,
                                    operator=admin.get("sub", "admin"))


class AuthorizeExitIn(BaseModel):
    """人工授权出场：charge=按在场记录计费；free/void=免费/作废（无记录也可，必须原因）。"""

    vehicle_id: str = Field(..., min_length=1, max_length=64)
    plate: str = Field(..., min_length=1, max_length=12)
    action: Literal['charge', 'free', 'void'] = 'charge'
    reason: str = ""
    request_id: str | None = Field(None, min_length=1, max_length=64)


@router.post("/api/manual/authorize-exit")
def manual_authorize_exit(body: AuthorizeExitIn,
                          admin: dict = Depends(require_admin),
                          c: Container = Depends(get_container)) -> dict:
    plate = body.plate.strip().upper()
    if not plate:
        raise ValidationError("车牌不能为空")
    return c.access.authorize_exit(body.vehicle_id, plate, action=body.action,
                                   reason=body.reason,
                                   operator=admin.get("sub", "admin"),
                                   request_id=body.request_id)


class CorrectExitIn(BaseModel):
    """异常出口纠正（冻结契约）：纠正入场车牌/时长后自动结算，无记录可 free/void。"""

    record_id: int | None = Field(None, ge=1)
    plate: str = Field(..., min_length=1, max_length=12)
    entry_sim_sec: float | None = Field(None, ge=0, allow_inf_nan=False,
                                        description="纠正后的仿真秒入场时间（仅 demo）")
    entry_sim_min: float | None = Field(None, ge=0, allow_inf_nan=False,
                                        description="纠正后的仿真分钟入场时间（仅 demo）")
    action: Literal['auto', 'free', 'void'] = 'auto'
    reason: str = ""
    request_id: str | None = Field(None, min_length=1, max_length=64)


@router.post("/api/manual/correct-exit")
def manual_correct_exit(body: CorrectExitIn,
                        admin: dict = Depends(require_admin),
                        c: Container = Depends(get_container)) -> dict:
    """纠正入场车牌/停车时长并自动结算出场；无记录时 action=free/void 临时放行。"""
    plate = body.plate.strip().upper()
    if not plate:
        raise ValidationError("车牌不能为空")
    entry_sim = _sim_input(body.entry_sim_sec, body.entry_sim_min)
    return c.access.correct_exit(
        plate=plate, record_id=body.record_id, entry_sim_sec=entry_sim,
        action=body.action, reason=body.reason,
        operator=admin.get("sub", "admin"), request_id=body.request_id)


class ManualExitIn(BaseModel):
    plate: str
    reason: str = ""
    action: Literal['charge','max_duration','free','void','manual_time'] = 'charge'
    record_id: int | None = Field(None, ge=1)
    entry_time: str | None = None
    entry_sim_sec: float | None = Field(None, ge=0, allow_inf_nan=False,
                                        description="仿真秒入场时间（演示模式；兼容旧 ISO entry_time）")
    entry_sim_min: float | None = Field(None, ge=0, allow_inf_nan=False,
                                        description="仿真分钟入场时间（演示模式）")
    amount_override: float | None = Field(None, ge=0, allow_inf_nan=False)
    request_id: str | None = Field(None, min_length=1, max_length=48)


@router.post("/api/manual/exit")
def manual_exit(body: ManualExitIn, admin: dict = Depends(require_admin),
                c: Container = Depends(get_container)) -> dict:
    """人工结算；正常记录已在出口识别时自动结算，此处幂等返回/处理异常记录。"""
    plate = body.plate.strip().upper()
    if not plate:
        raise ValidationError("车牌不能为空")
    return c.access.manual_exit(plate, operator=admin.get("sub", "admin"),
                                reason=body.reason, action=body.action, record_id=body.record_id,
                                entry_time=_input_time(body.entry_time,"入场时间"),
                                entry_sim_sec=_sim_input(body.entry_sim_sec, body.entry_sim_min),
                                amount_override=body.amount_override, request_id=body.request_id)


class PayIn(BaseModel):
    record_id: int
    method: str = "mock_scan"
    amount_override: Optional[float] = Field(None, ge=0, allow_inf_nan=False)


@router.post("/api/pay")
def pay(body: PayIn, admin: dict = Depends(require_admin),
        c: Container = Depends(get_container)) -> dict:
    """管理端结算缴费（幂等）。提交后 access 据 PaymentCompleted 出场/抬杆。"""
    rec = c.billing.pay(body.record_id, method=body.method,
                        operator=admin.get("sub", "admin"),
                        amount_override=body.amount_override)
    return {"ok": True, "payment_id": rec.id, "record_id": rec.parking_record_id,
            "amount_paid": rec.amount_paid, "method": rec.method}


# ----------------------------- 摄像头故障注入 / 恢复 -----------------------------
class CameraIn(BaseModel):
    reason: str = "manual"
    resolution: str = "摄像头已恢复"


@router.get("/api/camera/{gate}/status")
def camera_status(gate: str, admin: dict = Depends(require_admin),
                  c: Container = Depends(get_container)) -> dict:
    if gate not in ("in", "out"):
        raise ValidationError("gate 应为 in/out")
    return c.watchdog.details(gate)


@router.post("/api/camera/{gate}/inject")
def camera_inject(gate: str, body: CameraIn, admin: dict = Depends(require_admin),
                  c: Container = Depends(get_container)) -> dict:
    if gate not in ("in", "out"):
        raise ValidationError("gate 应为 in/out")
    c.watchdog.inject_fault(gate, reason=body.reason)
    return {"ok": True, "gate": gate, "down": c.watchdog.down(gate)}


@router.post("/api/camera/{gate}/reset")
@router.post("/api/camera/{gate}/recover")
def camera_recover(gate: str, body: CameraIn, admin: dict = Depends(require_admin),
                   c: Container = Depends(get_container)) -> dict:
    if gate not in ("in", "out"):
        raise ValidationError("gate 应为 in/out")
    c.watchdog.recover(gate, operator=admin.get("sub", "admin"),
                       resolution=body.resolution)
    retry = c.access.retry_waiting_vehicles(
        gate, operator=admin.get("sub", "admin"))
    return {"ok": True, "gate": gate, "down": c.watchdog.down(gate),
            "retry": retry}


# ----------------------------- 名单管理 -----------------------------
class ListIn(BaseModel):
    plate: str
    list_type: str = Field("white", description="white | black | monthly")
    valid_until: Optional[str] = None
    note: Optional[str] = None


@router.get("/api/lists")
def list_vehicles(list_type: Optional[str] = Query(None),
                  c: Container = Depends(get_container)) -> dict:
    return {"items": c.list_repo.list(list_type=list_type)}


@router.post("/api/lists")
def upsert_vehicle(body: ListIn, admin: dict = Depends(require_admin),
                   c: Container = Depends(get_container)) -> dict:
    try:
        plate, _ = normalize_valid_plate(body.plate)
    except ValueError as exc:
        raise ValidationError(f"车牌格式无效：{exc}") from exc
    if body.list_type not in ("white", "black", "monthly"):
        raise ValidationError("名单类型应为 white/black/monthly")
    valid_until = _input_time(body.valid_until, "有效期")
    with c.db.scope():
        c.list_repo.upsert(plate=plate, list_type=body.list_type,
                           valid_until=valid_until, note=body.note)
        c.oplog_repo.add(operator=admin.get("sub", "admin"), action="list_upsert",
                         target=plate, detail={"list_type": body.list_type,
                                               "valid_until": body.valid_until})
    return {"ok": True, "plate": plate, "list_type": body.list_type}


@router.delete("/api/lists/{plate}")
def remove_vehicle(plate: str, admin: dict = Depends(require_admin),
                   c: Container = Depends(get_container)) -> dict:
    try:
        normalized, _ = normalize_valid_plate(plate)
    except ValueError as exc:
        raise ValidationError(f"车牌格式无效：{exc}") from exc
    with c.db.scope():
        removed = c.list_repo.remove(normalized)
    if not removed:
        raise NotFound(f"名单中无车牌 {plate}")
    return {"ok": True, "plate": plate}


# ----------------------------- 识别 / 操作日志 -----------------------------
@router.get("/api/recognition-logs")
def recognition_logs(limit: int = Query(50, ge=1, le=500),
                     c: Container = Depends(get_container)) -> dict:
    return {"items": c.recog_repo.recent(limit=limit)}


class CorrectRecognitionIn(BaseModel):
    plate: str


@router.post("/api/recognition-logs/{log_id}/correct")
def correct_recognition(log_id: int, body: CorrectRecognitionIn,
                        admin: dict = Depends(require_admin),
                        c: Container = Depends(get_container)) -> dict:
    try:
        plate, _ = normalize_valid_plate(body.plate)
    except ValueError as exc:
        raise ValidationError(f"车牌格式无效：{exc}") from exc
    with c.db.scope():
        original = c.recog_repo.get(log_id)
        if original is None:
            raise NotFound(f"识别日志 #{log_id} 不存在")
        linked = c.pending_repo.confirmed_for_log(log_id)
        if linked is not None and linked.confirmed_plate != plate:
            raise StateConflict("linked_record", "该识别日志已关联确认车次，不能单独修改车牌")
        updated, changed = c.recog_repo.mark_corrected(log_id, plate)
        if changed:
            c.oplog_repo.add(
                operator=admin.get("sub", "admin"), action="recognition_correct",
                target=str(log_id), detail={
                    "original_plate": original["plate"],
                    "previous_corrected_plate": original["corrected_plate"],
                    "corrected_plate": plate,
                })
    return updated


@router.get("/api/oplogs")
def oplogs(limit: int = Query(100, ge=1, le=500),
           c: Container = Depends(get_container)) -> dict:
    return {"items": c.oplog_repo.recent(limit=limit)}


# ----------------------------- 实时总览（仪表盘） -----------------------------
@router.get("/api/overview")
def overview(c: Container = Depends(get_container)) -> dict:
    """管理端首屏：余位、在场数、闸口/摄像头状态、未闭环异常数。"""
    layout = c.layout.current_layout()
    return {
        "layout_version": layout.version,
        "free_total": layout.free_total,
        "free_by_zone": layout.free,
        "zones_total": layout.zones,
        "open_records": c.parking_repo.count_open(),
        "occupied_spaces": c.space_repo.occupied_count(),
        "barriers": {g: b.state.value if hasattr(b.state, "value") else str(b.state)
                     for g, b in c.access.barriers.items()},
        "camera": {g: c.watchdog.status(g) for g in ("in", "out")},
        "open_incidents": len(c.incidents.list(status="open", limit=500)),
        "recommend": {
            "blue": c.allocator.recommend("blue"),
            "green": c.allocator.recommend("green"),
        },
        "server_time": iso_local(utcnow()),
        "timezone": c.cfg.server.timezone,
    }


@router.get('/api/find-car')
def locate_current_car(plate: str = Query(...,min_length=1,max_length=32),
                       c: Container = Depends(get_container)) -> dict:
    """Authenticated exact current-visit lookup; never selects a similar/historical plate."""
    from ..access.find_car import find_car
    return find_car(c,plate)


@router.get('/api/recognize/gallery')
def recognition_gallery(c: Container = Depends(get_container)):
    """Curated public sample metadata; never exposes arbitrary dataset files."""
    import json
    from pathlib import Path
    path = Path(c.cfg.paths.plates_dataset).parent / 'demo-lpr' / 'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    return {'source': data['source'], 'purpose': data['purpose'],
            'samples': [{key: row[key] for key in ('file','plate','color','style','condition')}
                        for row in data['samples']]}
