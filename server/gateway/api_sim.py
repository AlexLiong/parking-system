"""仿真端 REST + 车牌图像识别导入（gateway/api_sim.py）。

对应设计文档 §3.7 / §11.1：
- ``POST /sim/events/{arrived,passed,left,zone,barrier_ack}``：仿真端上报事件，
  网关只做"HTTP → EventBus"转发，业务由 access 处理（前端无权自行开闸）；
- ``GET /sim/scenario``：拉取演示剧本（存在 web/sim3d/scenario.json 则用之）；
- ``POST /api/recognize``：**导入车牌图片识别**（上传真实蓝/绿牌照片或给图库路径，
  返回识别结果）——识别输入与 3D 建模彻底解耦（FR-1.3）。
"""
from __future__ import annotations

import logging
import hashlib
import shutil
from pathlib import Path
from typing import Optional
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..core.errors import CameraFault, ParkingError, StateConflict
from ..core.domain import RecognitionResult
from ..core.events import (PlateRecognized, VehicleArrived, VehicleLeft,
                           VehiclePassedGate, ZoneOccupied)
from ..core.timeutil import iso_local, utcnow
from ..lpr.charset import validate_grammar
from ..lpr.dispatch import create_plate_image
from ..lpr.video import (MAX_VIDEO_BYTES, VideoDecodeError, VideoError,
                         VideoSourceError, is_rtsp_source, sample_video)
from .context import Container, get_container

log = logging.getLogger("gateway.sim")
router = APIRouter(tags=["sim"])
MAX_FRAME_BYTES = 5 * 1024 * 1024


class PlateImageIn(BaseModel):
    plate: str = Field(..., min_length=1, max_length=12)
    plate_type: str = Field(..., description="blue|green|green_large|yellow")


class DispatchPlanIn(PlateImageIn):
    duration_sec: Optional[float] = Field(
        None, ge=1.0, le=3600.0, description="仿真秒；仅派车展示，不写业务")
    duration: Optional[float] = Field(
        None, ge=1.0, le=3600.0, description="duration_sec 的兼容别名（仿真秒）")

    def resolved_duration(self) -> float:
        if self.duration_sec is not None and self.duration is not None:
            raise ValueError("duration_sec 与 duration 只能提供一个")
        value = self.duration_sec if self.duration_sec is not None else self.duration
        return float(60.0 if value is None else value)


def _dispatch_payload(c: Container, body: PlateImageIn,
                      duration_sec: Optional[float] = None) -> dict:
    try:
        data = create_plate_image(body.plate, body.plate_type, c.cfg.paths.photos_dir)
    except ValueError as exc:
        raise HTTPException(422, detail={
            "reason": str(exc), "plate": body.plate, "plate_type": body.plate_type,
        }) from exc
    except Exception as exc:  # noqa: BLE001 - 渲染失败不应 500 静默
        log.exception("生成车牌图片失败 plate=%s type=%s", body.plate, body.plate_type)
        raise HTTPException(500, detail={"reason": "车牌图片渲染失败"}) from exc
    image_url = c.access._public_image_url(data["image_path"])
    result = {
        "ok": True,
        "plate": data["plate"],
        "plate_type": data["plate_type"],
        "color": data["color"],
        "vehicle_kind": data["vehicle_kind"],
        "image_url": image_url,
        "image_path": data["image_path"],
    }
    if duration_sec is not None:
        result["duration_sec"] = float(duration_sec)
    return result



def _recognition_log(c: Container, *, gate: str, image_path: Optional[str],
                     result: RecognitionResult,
                     success: Optional[bool] = None) -> Optional[int]:
    """P2 上传/视频识别写 recognition_log；失败只记日志，绝不影响识别返回。"""
    try:
        if c.recog_repo is None:
            return None
        log_gate = gate if gate in ("in", "out") else "upload"
        color = result.color.value if result.color else None
        with c.db.scope():
            row = c.recog_repo.add(
                gate=log_gate,
                image_path=str(image_path) if image_path else None,
                plate=result.plate,
                plate_color=color,
                char_confs=[float(v) for v in (result.char_confs or [])],
                overall_conf=float(result.overall_conf or 0.0),
                elapsed_ms=int(result.elapsed_ms or 0),
                success=bool(result.success if success is None else success),
            )
            log_id = int(row.id)
        return log_id
    except Exception:  # noqa: BLE001 - 日志写入失败不阻断识别/演示
        log.exception("写 recognition_log 失败（不影响识别返回）")
        return None


def _allowed_video(raw: str, c: Container) -> Optional[Path]:
    """本地视频路径限制在 data/photos 或项目根内；RTSP 返回 None。"""
    if is_rtsp_source(raw):
        return None
    from ..core.config import PROJECT_ROOT
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(400, f"视频路径无效: {raw}") from exc
    roots = [Path(c.cfg.paths.photos_dir).resolve(),
             Path(c.cfg.paths.data_dir).resolve(),
             Path(PROJECT_ROOT).resolve()]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise HTTPException(403, "只能读取 data/photos、data 或项目内的本地视频")
    return resolved


class VideoIn(BaseModel):
    """视频识别输入：path（本地文件）或 rtsp_url/RTSP source 二选一。"""
    path: Optional[str] = None
    rtsp_url: Optional[str] = None
    source: Optional[str] = None        # 兼容 source=本地路径或 RTSP URL
    gate: Literal['in', 'out'] = 'in'
    frames: int = Field(3, ge=2, le=5)
    interval: float = Field(0.5, gt=0, le=10)
    timeout_sec: float = Field(15.0, ge=1, le=60)



def _save_frame(data: bytes, cfg) -> tuple[Path, str]:
    """Validate and persist a camera screenshot under photos_dir/camera."""
    import cv2
    import numpy as np
    if not data:
        raise HTTPException(400, "空图像")
    if len(data) > MAX_FRAME_BYTES:
        raise HTTPException(413, "图像不能超过5MiB")
    if cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) is None:
        raise HTTPException(400, "需要有效JPEG/PNG图像")
    suffix = ".png" if data.startswith(b"\x89PNG") else ".jpg"
    dest_dir = Path(cfg.paths.photos_dir) / "camera"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"cam_{utcnow():%Y%m%d%H%M%S}_{uuid4().hex[:10]}{suffix}"
    target.write_bytes(data)
    return target, suffix



def allowed_image(raw: str, c: Container) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        from ..core.config import PROJECT_ROOT
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    allowed = [Path(c.cfg.paths.photos_dir).resolve(), Path(c.cfg.paths.plates_dataset).resolve().parent]
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise HTTPException(403, '只能读取项目车牌图库或上传图片目录')
    return resolved


class ArrivedIn(BaseModel):
    vehicle_id: str
    gate: Literal['in','out']
    image_path: Optional[str] = None
    sim_ts: Optional[float] = None
    expected_plate: Optional[str] = None      # 仿真 3D 相机（可选；用于 expected 决策）
    source: Literal['legacy','sim_camera','upload'] = 'legacy'


class PassedIn(BaseModel):
    vehicle_id: str
    gate: Literal['in','out']
    plate: Optional[str] = Field(None, max_length=12)


class ZoneIn(BaseModel):
    owner_id: Optional[str] = Field(None,min_length=1,max_length=64)
    session_id: Optional[str] = None
    zone_id: Literal['in_barrier','out_barrier']
    occupied: bool
    vehicle_id: Optional[str] = None


class AckIn(BaseModel):
    gate: Literal['in','out']
    state: Literal['open','closed','stopped']
    cmd_id: Optional[str] = None
    arm_position: Optional[float] = Field(None, ge=0, le=1)


@router.post("/sim/events/arrived")
def sim_arrived(body: ArrivedIn, c: Container = Depends(get_container)) -> dict:
    if body.image_path:
        allowed_image(body.image_path,c)
    c.bus.publish(VehicleArrived(vehicle_id=body.vehicle_id, gate=body.gate,
                                 image_path=body.image_path, sim_ts=body.sim_ts,
                                 expected_plate=body.expected_plate,
                                 source=body.source))
    return {"ok": True}


@router.post("/sim/events/arrived-frame")
async def sim_arrived_frame(
        vehicle_id: str = Form(...),
        gate: Literal['in', 'out'] = Form(...),
        expected_plate: Optional[str] = Form(None),
        sim_ts: Optional[float] = Form(None),
        image: UploadFile = File(...),
        c: Container = Depends(get_container)) -> dict:
    """仿真 3D 场景相机截图识别入口（multipart；与 /api/recognize 业务隔离）。

    保存原图到 photos_dir/camera/ → 发布 VehicleArrived(source=sim_camera,
    expected_plate) → 现有 AccessControlService 完成识别/expected 决策/放行/分配；
    返回截图 URL 与本次识别事件（若已产生），WS 同步推送 plate_recognized。
    """
    data = await image.read(MAX_FRAME_BYTES + 1)
    target, _suffix = _save_frame(data, c.cfg)
    image_url = c.access._public_image_url(str(target))
    captured: list[PlateRecognized] = []

    def _capture(event) -> None:
        try:
            if (isinstance(event, PlateRecognized)
                    and event.vehicle_id == vehicle_id and event.gate == gate):
                captured.append(event)
        except Exception:  # noqa: BLE001 - 收集失败不影响业务
            log.exception("收集 PlateRecognized 失败")

    c.bus.subscribe(PlateRecognized, _capture)
    try:
        c.bus.publish(VehicleArrived(
            vehicle_id=vehicle_id, gate=gate, image_path=str(target),
            sim_ts=sim_ts, expected_plate=expected_plate, source="sim_camera"))
    finally:
        c.bus.unsubscribe(PlateRecognized, _capture)
    event = captured[-1] if captured else None
    recognition = None
    if event is not None:
        recognition = {
            "plate": event.plate, "color": event.color,
            "overall_conf": event.overall_conf, "conf": event.overall_conf,
            "need_manual": event.need_manual, "candidates": event.candidates,
            "message": event.message, "reason": event.reason,
            "image_path": event.image_path, "image_url": event.image_url,
            "plate_crop_url": event.plate_crop_url,
            "source": event.source, "expected_plate": event.expected_plate,
            "plate_match": event.plate_match,
        }
    return {"ok": True, "vehicle_id": vehicle_id, "gate": gate,
            "expected_plate": expected_plate,
            "image_path": str(target), "image_url": image_url,
            "recognition": recognition}


@router.post("/sim/events/passed")
def sim_passed(body: PassedIn, c: Container = Depends(get_container)) -> dict:
    c.bus.publish(VehiclePassedGate(vehicle_id=body.vehicle_id, gate=body.gate))
    return {"ok": True}


@router.post("/sim/events/left")
def sim_left(body: PassedIn, c: Container = Depends(get_container)) -> dict:
    c.bus.publish(VehicleLeft(vehicle_id=body.vehicle_id, gate=body.gate,
                              plate=body.plate))
    return {"ok": True}


@router.post("/sim/events/zone")
def sim_zone(body: ZoneIn, c: Container = Depends(get_container)) -> dict:
    from ..core.errors import StateConflict
    with c.state._lock:
        active=c.state.current_session()
        if body.owner_id and (not active or body.session_id!=active.id):
            raise StateConflict("session_changed","地感消息会话已切换")
        c.bus.publish(ZoneOccupied(zone_id=body.zone_id, occupied=body.occupied,
                                   vehicle_id=body.vehicle_id,owner_id=body.owner_id))
    return {"ok": True}


@router.post("/sim/events/barrier_ack")
def sim_ack(body: AckIn, c: Container = Depends(get_container)) -> dict:
    """前端道闸动画完成回执（REST 通道；亦可走 WS）。"""
    accepted = c.access.on_barrier_ack(body.gate, body.state, body.cmd_id,
        require_cmd=c.cfg.server.ws_v2, arm_position=body.arm_position)
    return {"ok": True, "accepted": accepted}


@router.get("/sim/scenario")
def get_scenario(c: Container = Depends(get_container)) -> dict:
    """仿真剧本：优先 data/scenario.json，回退内置默认（新格式含 vehicles）。"""
    from .. import scenario as scenario_service
    scenario, _source = scenario_service.load_scenario(c.cfg)
    return scenario


@router.post("/api/sim/plate-image")
def sim_plate_image(body: PlateImageIn,
                    c: Container = Depends(get_container)) -> dict:
    """手动派车/演示用：按 plate_type 渲染车牌图片，不进入业务。"""
    return _dispatch_payload(c, body)


@router.post("/api/sim/dispatch-plan")
def sim_dispatch_plan(body: DispatchPlanIn,
                      c: Container = Depends(get_container)) -> dict:
    """手动派车计划：同车牌渲染 + duration_sec（仿真秒），无停车记录/入场。"""
    try:
        duration = body.resolved_duration()
    except ValueError as exc:
        raise HTTPException(422, detail={"reason": str(exc)}) from exc
    # 实现 B：当前已有同牌未出场记录 → 409 duplicate_entry（返回现有记录），
    # 不生成车牌图、不派车（3D 端据 409 直接提示"重复进场"）。
    from ..lpr.charset import normalize_plate_type_plate
    from ..repository.parking_repo import ParkingRecordRepo
    try:
        normalized_plate, _plate_type = normalize_plate_type_plate(body.plate, body.plate_type)
    except ValueError as exc:
        raise HTTPException(422, detail={"reason": str(exc), "plate": body.plate}) from exc
    existing = c.parking_repo.find_open_by_plate(normalized_plate, include_temporary=False)
    if existing is not None:
        raise StateConflict(
            "duplicate_entry",
            f"车牌 {normalized_plate} 已有未出场记录，禁止重复派车",
            plate=normalized_plate,
            existing_record=ParkingRecordRepo.to_view(existing))
    return _dispatch_payload(c, body, duration_sec=duration)


class BlacklistGateIn(BaseModel):
    """黑名单车辆绕行闸杆（实现 A）：不建 parking_record，仅物理开/关 + 审计。"""

    vehicle_id: str = Field(..., min_length=1, max_length=64)
    plate: str = Field(..., min_length=1, max_length=12)
    gate: Literal['in', 'out']
    action: Literal['open', 'close']


@router.post("/api/sim/blacklist-gate")
def sim_blacklist_gate(body: BlacklistGateIn,
                       c: Container = Depends(get_container)) -> dict:
    """黑名单绕行走正常闸杆：access 操作 barrier（reason=blacklist_bypass）。

    - 不创建/修改 parking_record，不产生业务授权（EntryDenied/场内绕行由 3D 决策）；
    - open/close 走正常 BarrierCommand/BarrierStateChanged + barrier_ack；
    - 审计：incident/recognition_log + blacklist_gate 事件 + op_log。
    """
    result = c.access.blacklist_gate(body.vehicle_id, body.plate.strip().upper(),
                                     body.gate, body.action, operator="sim")
    payload = {"vehicle_id": body.vehicle_id, "plate": result.get("plate"),
               "gate": result.get("gate"), "action": body.action,
               "reason": "blacklist_bypass", "state": result.get("state"),
               "cmd_id": result.get("cmd_id"), "manual": result.get("manual"),
               "passable": result.get("passable"),
               "authorization": result.get("authorization")}
    seq = None
    try:
        with c.db.scope():
            if c.oplog_repo is not None:
                c.oplog_repo.add(operator="sim", action="blacklist_gate",
                                 target=result.get("plate"), detail=payload)
            if c.recog_repo is not None:
                c.recog_repo.add(gate=body.gate, image_path=None,
                                 plate=result.get("plate"), plate_color=None,
                                 char_confs=[], overall_conf=1.0,
                                 elapsed_ms=0, success=True)
            event = c.state.append_event("blacklist_gate", payload, source="sim")
            seq = event.seq
    except Exception:  # noqa: BLE001 - 审计失败不影响闸杆动作（动作已落库）
        log.exception("blacklist-gate 审计写入失败")
    return {**result, "ok": True, "vehicle_id": body.vehicle_id,
            "action": body.action, "seq": seq,
            "reason": "blacklist_bypass"}


class BlacklistResolutionIn(BaseModel):
    """3D 端上报黑名单实际处置路径（用户新规则：无后车倒车离场 / 有后车进场后直接出场）。"""

    vehicle_id: Optional[str] = Field(None, max_length=64)
    plate: str = Field(..., min_length=1, max_length=12)
    resolution: Literal['reversed', 'entered_then_left'] = 'reversed'
    note: str = ""


@router.post("/api/sim/blacklist-resolution")
def sim_blacklist_resolution(body: BlacklistResolutionIn,
                             c: Container = Depends(get_container)) -> dict:
    """记录黑名单车辆实际处置（只写事件/审计，不建进出场记录）。"""
    plate = body.plate.strip().upper()
    payload = {"vehicle_id": body.vehicle_id, "plate": plate,
               "reason": "blacklist", "resolution": body.resolution,
               "note": body.note}
    with c.db.scope():
        if c.oplog_repo is not None:
            c.oplog_repo.add(operator="sim", action="blacklist_resolution",
                             target=plate, detail=payload)
        event = c.state.append_event("blacklist_resolution", payload, source="sim")
    # 黑名单前车处置完成后：自动重试同入口等待/待办后车（旧 pending 先 rejected 审计，
    # 再复用最近 arrival 重新识别；成功直接授权入场并清理旧 pending，失败重新入队）。
    try:
        retry = c.access.retry_waiting_vehicles(
            "in", operator="system", include_all=True,
            resolution="blacklist_resolved_retry",
            trust_expected=True)
    except Exception:  # noqa: BLE001 - 处置记录已落库，重试异常不得影响接口返回
        log.exception("黑名单处置后自动重试后车失败")
        retry = {"ok": False, "retried": 0, "authorized": 0, "failed": 0,
                 "pending_ids": [], "retried_vehicle_ids": [],
                 "reason": "retry_failed"}
    return {"ok": True, "plate": plate, "resolution": body.resolution,
            "seq": event.seq,
            "retried": retry.get("retried", 0),
            "authorized": retry.get("authorized", 0),
            "failed": retry.get("failed", 0),
            "pending_ids": retry.get("pending_ids", []),
            "retried_vehicle_ids": retry.get("retried_vehicle_ids", []),
            "expected_authorized": retry.get("expected_authorized", 0),
            "retry": retry}


# ----------------------------- 车牌图像识别导入 -----------------------------
@router.post("/api/recognize")
async def recognize_image(file: Optional[UploadFile] = File(None),
                          image_path: Optional[str] = None,
                          gate: str = "in",
                          c: Container = Depends(get_container)) -> dict:
    """导入车牌图片进行识别：支持上传文件（file）或指定服务器图库路径（image_path）。

    返回识别结果与是否需人工确认；识别不可用时返回 503（降级人工）。
    """
    if c.lpr is None:
        raise CameraFault("识别服务不可用，请人工录入")

    target: Optional[Path] = None
    tmp_created = False
    if file is not None:
        data = await file.read(MAX_FRAME_BYTES+1)
        if len(data)>MAX_FRAME_BYTES:
            raise HTTPException(413,'图片不能超过5MiB')
        import cv2
        import numpy as np
        if file.content_type not in ('image/jpeg','image/png') or cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR) is None:
            raise HTTPException(400,'需要有效JPEG/PNG图片')
        dest_dir = Path(c.cfg.paths.photos_dir) / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".png" if data.startswith(b"\x89PNG") else ".jpg"
        target = dest_dir / f"up_{utcnow():%Y%m%d%H%M%S}_{uuid4().hex[:8]}{suffix}"
        with open(target, "wb") as out:
            out.write(data)
        tmp_created = True
    elif image_path:
        target = allowed_image(image_path,c)
        if not target.exists():
            raise HTTPException(404, f"图像不存在: {image_path}")
    else:
        raise HTTPException(400, "需提供 file 上传或 image_path 参数")

    try:
        crop_dir = Path(c.cfg.paths.photos_dir) / "crops"
        crop_dir.mkdir(parents=True, exist_ok=True)
        res = c.lpr.recognize(str(target), gate=gate, crop_dir=crop_dir)
        threshold = float(getattr(c.cfg.lpr, "conf_threshold", 0.85))
        color = res.color.value if res.color else None
        grammar_ok = bool(res.plate and color and validate_grammar(res.plate, color)[0])
        # 上传演示阈值固定 0.85（不沿用 HyperLPR3 的 0.95 严格成功位），且只看
        # 图像源/语法/置信度；上传绝不触发入场或 expected_plate 匹配。
        need_manual = bool(res.source_status != "ok"
                           or not res.plate or not grammar_ok
                           or res.overall_conf < threshold)
        image_url = c.access._public_image_url(str(target))
        plate_crop_url = c.access._public_image_url(res.plate_crop_path)
        image_source, image_synthetic = c.access._image_meta(image_url)
        out = res.to_log_dict()
        out.update({
            # 统一结果字段（上传只做演示：绝不调用入场/放行）
            "source": "upload",
            "need_manual": need_manual,
            "success": not need_manual,
            "suspected": res.suspected,
            "candidates": res.candidates,
            "message": res.message,
            "source_status": res.source_status,
            "image_metrics": res.image_metrics,
            "color": color,
            "plate_color": color,
            "conf": res.overall_conf,
            "image_path": str(target),
            "image_url": image_url,
            "plate_crop_path": res.plate_crop_path,
            "plate_crop_url": plate_crop_url,
            "evidence_url": image_url,
            "image_source": image_source,
            "image_synthetic": image_synthetic,
        })
        out["recognition_log_id"] = _recognition_log(
            c, gate=gate, image_path=str(target), result=res,
            success=not need_manual)
        return out
    except ParkingError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("识别失败")
        raise HTTPException(500, "识别处理失败，请查看服务日志") from exc


@router.post("/api/recognize/frames")
async def recognize_frames(files: list[UploadFile] = File(...),
                           gate: str = Form("in"),
                           c: Container = Depends(get_container)) -> dict:
    """Recognize one vehicle from a bounded batch of captured frames; never operate a gate."""
    if c.lpr is None:
        raise CameraFault("识别服务不可用，请人工录入")
    if not c.cfg.lpr.multiframe_enabled:
        raise HTTPException(503, "多帧识别已关闭")
    if gate not in ("in", "out"):
        raise HTTPException(400, "闸口必须为 in 或 out")
    if not 2 <= len(files) <= 5:
        raise HTTPException(400, "需要 2 至 5 张连续帧")

    frames = []
    for file in files:
        data = await file.read(MAX_FRAME_BYTES + 1)
        if len(data) > MAX_FRAME_BYTES:
            raise HTTPException(413, "单帧不能超过 5 MiB")
        suffix = ".jpg" if data.startswith(b"\xff\xd8\xff") else (
            ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else None)
        if suffix is None:
            raise HTTPException(400, "只接受 JPEG 或 PNG 图像")
        frames.append((data, suffix))
    if len({hashlib.sha256(data).digest() for data, _ in frames}) != len(frames):
        raise HTTPException(400, "连续帧不能重复上传同一图像")

    dest_dir = Path(c.cfg.paths.photos_dir) / "uploads" / "frames"
    dest_dir.mkdir(parents=True, exist_ok=True)
    batch_id = uuid4().hex
    paths = []
    for index, (data, suffix) in enumerate(frames):
        target = dest_dir / f"{batch_id}_{index}{suffix}"
        target.write_bytes(data)
        paths.append(str(target))
    crop_dir = Path(c.cfg.paths.photos_dir) / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    frame_results: list[RecognitionResult] = []
    fused = c.lpr.recognize_frames(paths, gate=gate, crop_dir=crop_dir,
                                   frame_results=frame_results)
    log_id = _recognition_log(
        c, gate=gate, image_path=paths[0] if paths else None, result=fused,
        success=not fused.need_manual)
    return {"batch_id": batch_id, "gate": gate,
            "recognition_log_id": log_id,
            "frames": [{**r.to_log_dict(), "need_manual": r.need_manual,
                        "message": r.message, "source_status": r.source_status,
                        "image_path": path}
                       for r, path in zip(frame_results, paths)],
            "fused": {**fused.to_log_dict(), "need_manual": fused.need_manual,
                      "candidates": fused.candidates, "message": fused.message}}


@router.post("/api/recognize/video")
def recognize_video(body: VideoIn, c: Container = Depends(get_container)) -> dict:
    """P3-06 最小可用视频源：本地视频/RTSP 抽 2–5 帧 → 多帧融合。

    只做识别演示，不触发入场/放行/expected_plate 匹配；识别成功/失败都会尝试写
    一条融合 recognition_log（写失败不影响返回）。
    """
    if c.lpr is None:
        raise CameraFault("识别服务不可用，请人工录入")
    if not c.cfg.lpr.multiframe_enabled:
        raise HTTPException(503, "多帧识别已关闭")
    supplied = [value for value in (body.path, body.rtsp_url, body.source) if value]
    if len(supplied) != 1:
        raise HTTPException(400, "path、rtsp_url、source 必须且只能提供一个")
    raw = supplied[0].strip()
    if not raw:
        raise HTTPException(400, "视频源为空")
    if body.rtsp_url and not is_rtsp_source(body.rtsp_url):
        raise HTTPException(400, "rtsp_url 必须为 rtsp:// 或 rtsps:// URL")
    target = _allowed_video(raw, c)
    source_value = str(target) if target is not None else raw

    batch_id = uuid4().hex
    camera_dir = Path(c.cfg.paths.photos_dir) / "camera"
    camera_dir.mkdir(parents=True, exist_ok=True)
    import tempfile
    out_dir = Path(tempfile.mkdtemp(prefix=f"video_{batch_id[:12]}_",
                                    dir=str(camera_dir)))
    try:
        sample = sample_video(source_value, out_dir=out_dir, frames=body.frames,
                              interval=body.interval, timeout_sec=body.timeout_sec,
                              max_file_bytes=MAX_VIDEO_BYTES)
    except (VideoSourceError, VideoDecodeError, VideoError, ValueError) as exc:
        raise HTTPException(400, f"视频源不可用：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("视频抽帧失败 source=%s", raw)
        raise HTTPException(500, "视频抽帧失败，请查看服务日志") from exc

    crop_dir = Path(c.cfg.paths.photos_dir) / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    frame_results: list[RecognitionResult] = []
    try:
        fused = c.lpr.recognize_frames(sample.frames, gate=body.gate,
                                       crop_dir=crop_dir,
                                       frame_results=frame_results)
    except Exception as exc:  # noqa: BLE001
        log.exception("视频多帧识别失败 source=%s", raw)
        raise HTTPException(500, "视频帧识别失败，请查看服务日志") from exc
    log_id = _recognition_log(
        c, gate=body.gate, image_path=sample.frames[0], result=fused,
        success=not fused.need_manual)
    frames = []
    for index, path in enumerate(sample.frames):
        result = frame_results[index] if index < len(frame_results) else None
        item = {"index": index, "image_path": path,
                "image_url": c.access._public_image_url(path)}
        if result is not None:
            item.update({**result.to_log_dict(),
                         "need_manual": result.need_manual,
                         "message": result.message,
                         "source_status": result.source_status})
        frames.append(item)
    return {
        "ok": True,
        "source": raw,
        "source_type": sample.kind,
        "gate": body.gate,
        "batch_id": batch_id,
        "frames": frames,
        "fused": {**fused.to_log_dict(), "need_manual": fused.need_manual,
                  "candidates": fused.candidates, "message": fused.message},
        "video": sample.to_dict(),
        "recognition_log_id": log_id,
    }
