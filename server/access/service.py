"""进出场控制服务（access/service.py）—— 系统核心。

对应设计文档 §3.3 / §6 / §8.2 / §10。职责：订阅仿真端事件，驱动进出场业务，
裁决道闸"许可"（业务真理在后端，前端无权自行开闸，§2.2）。严格遵循
**先落库后动作**（RR-4.1）：DB 事务提交成功后才发道闸指令。

进出场主流程：
- 入口：VehicleArrived(in) → 摄像头/识别健康检查 → 识别 → 置信度门槛（低则转
  人工确认，禁止静默放行 §10.3）→ 重复进场拦截 → 车位分配（满位不放行 FR-2.3）
  → 建在场记录（含费率快照 FR-5.3）→ 提交 → 抬杆 + 推送识别/分配结果。
- 出口：VehicleArrived(out) → 识别 → 出场校验 → 计费报价（billing 协作）→
  PaymentCompleted → 落库(标记出场/释放车位) → 提交 → 抬杆。

道闸物理动画由前端执行、回执 barrier_ack；后端 BarrierController 只做逻辑裁决。
"""
from __future__ import annotations

import logging
import json
from datetime import timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from ..core.config import AppConfig, PROJECT_ROOT, get_config
from ..core.errors import (DataAnomaly, DuplicateEntryError, LotFullError, NoParkingRecordError, NotFound,
                           ParkingError, StateConflict, ValidationError)
from ..core.events import (ArrivalRetryRequested, EventBus, EntryDenied,
                           ExitAuthorized, ManualAuthorized, FeeQuoted,
                           IncidentRaised, IncidentResolved, BarrierStateChanged,
                           PaymentCompleted, PendingCreated, PendingResolved,
                           PlateRecognized, SpaceAssigned,
                           SpaceReleased, VehicleArrived, VehicleLeft,
                           VehiclePassedGate, ZoneOccupied)
from ..core.interfaces import IPlateRecognizer
from ..core.timeutil import iso_local, utcnow
from ..repository.log_repo import IncidentRepo, OpLogRepo, RecognitionLogRepo
from ..repository.parking_repo import ParkingRecordRepo
from ..repository.space_repo import SpaceRepo
from ..repository.models import BarrierAction, BarrierStateRow
from ..repository.db import synchronized
from ..repository.pending_repo import PendingManualRepo
from ..repository.user_repo import VehicleListRepo
from ..lpr.charset import normalize_valid_plate, validate_grammar
from .allocator import SpaceAllocator
from .barrier import BarrierController, TailgatingPolicy
from .guard import ConsistencyGuard

log = logging.getLogger("access")

_ZONE_TO_GATE = {"in_barrier": "in", "out_barrier": "out"}


class AccessControlService:
    def __init__(self, *, bus: EventBus, db, parking_repo: ParkingRecordRepo,
                 space_repo: SpaceRepo, incident_repo: IncidentRepo,
                 recog_log_repo: RecognitionLogRepo,
                 allocator: SpaceAllocator, guard: ConsistencyGuard,
                 cfg: Optional[AppConfig] = None,
                 recognizer: Optional[IPlateRecognizer] = None,
                 fee_provider=None, billing=None,
                 oplog_repo: Optional[OpLogRepo] = None,
                 pending_repo: Optional[PendingManualRepo] = None,
                 list_repo: Optional[VehicleListRepo] = None):
        self.bus = bus
        self.db = db
        self.parking = parking_repo
        self.spaces = space_repo
        self.incidents = incident_repo
        self.recog_logs = recog_log_repo
        self.oplog = oplog_repo
        self.pending_repo = pending_repo
        self.lists = list_repo
        self.allocator = allocator
        self.guard = guard
        self.cfg = cfg or get_config()
        self.recognizer = recognizer
        self.simulation = None                 # P1-05：由 Container 注入演示时钟
        self.fee_provider = fee_provider      # IFeeRuleProvider（进场费率快照）
        self.billing = billing                # 计费协作对象（出口报价/结算），可后接

        # 每闸一个"已授权未通过"计数，供跟车保持开启判定
        self._zone_sources: dict[str, dict[str, bool]] = {"in":{}, "out":{}}
        self._pending: dict[str, int] = {"in": 0, "out": 0}
        # P1-07：保存每个闸口/车辆最近一次到达事件，摄像头恢复后自动重试识别。
        self._arrivals: dict[tuple[str, str], VehicleArrived] = {}
        policy = TailgatingPolicy(pending_provider=lambda g: self._pending.get(g, 0))
        self.barriers: dict[str, BarrierController] = {
            "in": BarrierController("in", self.cfg.access, bus, policy),
            "out": BarrierController("out", self.cfg.access, bus, policy),
        }
        self._started = False

    # ----------------------------- 业务时间（实现 B） -----------------------------
    def _now(self):
        """业务当前时间：演示模式=仿真业务时间（epoch+sim_time），否则真实墙钟。"""
        if self.simulation is not None:
            return self.simulation.business_now()
        return utcnow()

    def _sim_seconds(self) -> Optional[float]:
        """当前仿真秒（非演示模式返回 None，记录保持"真实时间"语义）。"""
        if self.simulation is None or not getattr(self.cfg.server, "demo_mode", False):
            return None
        return float(self.simulation.sim_time)

    # ----------------------------- 事件订阅 -----------------------------
    def start(self) -> None:
        """订阅事件（gateway 启动时调用一次）。"""
        if self._started:
            return
        self.bus.subscribe(VehicleArrived, self.on_vehicle_arrived)
        self.bus.subscribe(VehiclePassedGate, self.on_vehicle_passed)
        self.bus.subscribe(ZoneOccupied, self.on_zone_occupied)
        self.bus.subscribe(VehicleLeft, self.on_vehicle_left)
        self.bus.subscribe(PaymentCompleted, self.on_payment_completed, critical=True)
        self._started = True

    @synchronized
    def tick(self, dt: float) -> None:
        """定时器驱动道闸状态机（gateway 后台任务按 tick_interval_ms 调用）。"""
        for b in self.barriers.values():
            b.tick(dt)

    # ----------------------------- 入口事件 -----------------------------
    @synchronized
    def on_vehicle_arrived(self, e: VehicleArrived) -> None:
        if e.vehicle_id:
            self._arrivals[(e.gate, e.vehicle_id)] = e
        try:
            if e.gate == "in":
                self._process_entry(e)
            elif e.gate == "out":
                self._process_exit_arrival(e)
            else:
                log.warning("未知闸口方向: %s", e.gate)
        except ParkingError as exc:
            # 业务异常：记录并按类型转异常流，不抬杆
            log.info("进场/出场业务拒绝: %s", exc.to_dict())
            if isinstance(exc, DataAnomaly) and exc.kind == "blacklist":
                # 用户规则：黑名单不建普通 pending 阻塞、不建进出场记录；只发布 EntryDenied
                # （3D 端决定"后方无车直接倒车离场 / 后方有车则进场后直接出场"）+ 审计事件。
                plate = exc.detail.get("plate")
                self.bus.publish(IncidentRaised(
                    kind="data", gate=e.gate,
                    detail={"plate": plate, "reason": "blacklist",
                            "vehicle_id": e.vehicle_id}))
                self.bus.publish(EntryDenied(
                    vehicle_id=e.vehicle_id, gate=e.gate, plate=plate,
                    reason="blacklist", message=exc.message,
                    expected_plate=getattr(e, "expected_plate", None),
                    image_path=e.image_path))
                self._publish_recognized(e.gate, e.vehicle_id, plate=plate,
                                         need_manual=False, reason="blacklist",
                                         message=exc.message,
                                         image_path=e.image_path,
                                         expected_plate=getattr(e, "expected_plate", None))
                return
            self._create_pending(e, reason=exc.code, message=exc.message)
            self._publish_recognized(e.gate, e.vehicle_id, need_manual=True,
                                     reason=exc.code, message=exc.message,
                                     image_path=e.image_path)
        except Exception:  # noqa: BLE001 - 兜底：绝不让异常导致误抬杆
            log.exception("处理 VehicleArrived 失败 gate=%s", e.gate)

    def _vehicle_open(self, vehicle_id: Optional[str]) -> bool:
        """该 vehicle_id 是否已有在场记录（重复 arrival 幂等判定）。"""
        if not vehicle_id:
            return False
        try:
            from ..repository.models import ParkingRecord
            return self.db.session.query(ParkingRecord).filter(
                ParkingRecord.vehicle_id == vehicle_id,
                ParkingRecord.status == "in").first() is not None
        except Exception:  # noqa: BLE001 - 查询失败走正常流程
            log.exception("查询在场车辆失败 vehicle=%s", vehicle_id)
            return False

    def _process_entry(self, e: VehicleArrived) -> None:
        # 0) 上传识别只做演示：即使有人把 upload 来源塞进业务事件，也不得入场。
        if getattr(e, "source", "legacy") == "upload":
            log.warning("忽略 upload 来源的进场业务事件 vehicle=%s", e.vehicle_id)
            return
        # 0.1) 幂等：同一 vehicle_id 已授权在场（如 3D 在自动恢复后又补发 arrival），
        #      不重复建记录/待办，避免"自动入场后残留 pending"。
        if self._vehicle_open(e.vehicle_id):
            log.info("车辆 %s 已入场，忽略重复到达上报", e.vehicle_id)
            return
        # 1) 摄像头故障降级（FR-9.2）
        if self._camera_down("in"):
            log.warning("入口摄像头故障，转人工录入 vehicle=%s", e.vehicle_id)
            self._create_pending(e, reason="camera_fault", message="摄像头故障，请人工录入车牌")
            self._publish_recognized("in", e.vehicle_id, need_manual=True,
                                     reason="camera_fault", message="摄像头故障，请人工录入车牌",
                                     image_path=e.image_path,
                                     source=getattr(e, "source", "legacy"),
                                     expected_plate=getattr(e, "expected_plate", None),
                                     plate_match=None)
            return
        # 2) 识别（输入是独立车牌图像，与 3D 模型解耦 FR-1.3）
        plate, color, conf, need_manual, candidates, crop, reason, log_id = self._recognize("in", e)
        plate_match = self._expected_plate_match(e, plate, color, conf)
        if getattr(e, "source", "legacy") == "sim_camera":
            if plate_match:
                need_manual = False
                reason = "expected_match"
                if log_id:
                    with self.db.scope():
                        self.recog_logs.mark_expected_match(log_id)
            else:
                need_manual = True
                reason = self._sim_manual_reason(e, plate)
        if need_manual:
            # 低置信度/失败 → 人工确认队列，禁止静默放行（§10.3）
            self._create_pending(e, reason=reason, message="识别待人工确认",
                                 plate=plate, color=color, candidates=candidates,
                                 recognition_log_id=log_id)
            self._publish_recognized("in", e.vehicle_id, plate=plate, color=color,
                                     conf=conf, need_manual=True,
                                     candidates=candidates,
                                     reason=reason,
                                     image_path=e.image_path, plate_crop_path=crop,
                                     source=getattr(e, "source", "legacy"),
                                     expected_plate=getattr(e, "expected_plate", None),
                                     plate_match=plate_match,
                                     message=self._manual_message(reason))
            return
        # 3) 授权进场（校验 + 分配 + 落库 + 抬杆）
        self._authorize_entry(e.vehicle_id, plate, color,
                              entry_photo=e.image_path, entry_crop=crop, conf=conf,
                              source=getattr(e, "source", "legacy"),
                              expected_plate=getattr(e, "expected_plate", None),
                              plate_match=plate_match,
                              match_reason=reason)
        # 授权成功后清理同 vehicle_id 的旧 pending（黑名单/识别失败遗留），避免前端残留
        if e.vehicle_id:
            self._resolve_pending_for_vehicle(
                e.vehicle_id, gate="in", operator="system", plate=plate,
                resolution="entry_authorized")

    @synchronized
    def _authorize_entry(self, vehicle_id: Optional[str], plate: str, color: str,
                         *, entry_photo: Optional[str] = None,
                         entry_crop: Optional[str] = None,
                         manual: bool = False, operator: Optional[str] = None,
                         confirmed: bool = False, conf: float = 1.0,
                         source: str = "legacy", expected_plate: Optional[str] = None,
                         plate_match: Optional[bool] = None,
                         match_reason: Optional[str] = None) -> dict:
        """入口授权：重复拦截 → 满位拦截 → 建记录（先落库）→ 抬杆（后动作）。"""
        plate, color = self._validated_plate(plate, color)
        entry_time = self._now()
        entry_sim_sec = self._sim_seconds()
        with self.db.scope():
            list_type = self.lists.active_type(plate, entry_time) if self.lists else None
            if list_type == "black":
                raise DataAnomaly(f"车牌 {plate} 在黑名单中，禁止进场",
                                  kind="blacklist", plate=plate)
            self.guard.check_entry(plate)                 # 重复进场
            fee_rule_id = self._snapshot_fee_rule_id()
            space = self.allocator.allocate(plate, color, vehicle_id=vehicle_id)
            if space is None:
                raise LotFullError(f"停车场已满，拒绝 {plate} 进场")
            rec = self.parking.create(
                plate=plate, plate_color=color, entry_time=entry_time,
                space_no=space.space_no, vehicle_id=vehicle_id,
                entry_photo=entry_photo, entry_plate_crop=entry_crop,
                manual_entry=manual, fee_rule_id=fee_rule_id,
                benefit_type=list_type if list_type in ("white", "monthly") else None,
                status="in", entry_sim_sec=entry_sim_sec)
            record_id, space_no, zone = rec.id, space.space_no, space.zone
            self.bus.publish(PlateRecognized(
                vehicle_id=vehicle_id, gate="in", plate=plate, color=color,
                overall_conf=1.0 if manual or confirmed else conf, need_manual=False,
                reason=match_reason or ("confirmed" if confirmed else "manual" if manual else "ok"),
                image_path=entry_photo,
                image_url=self._public_image_url(entry_photo),
                plate_crop_url=self._public_image_url(entry_crop),
                image_source=self._image_meta(self._public_image_url(entry_photo))[0],
                image_synthetic=self._image_meta(self._public_image_url(entry_photo))[1],
                source=source, expected_plate=expected_plate, plate_match=plate_match))
            self.bus.publish(SpaceAssigned(
                vehicle_id=vehicle_id, plate=plate, space_no=space_no, zone=zone))
            if manual and self.oplog:
                self.oplog.add(operator=operator, action="manual_register",
                               target=plate, detail={"record_id": record_id})
            self.db.on_commit(lambda: self._complete_entry_authorization_safe(
                record_id, vehicle_id, plate))
        if manual:
            # 提交后广播"人工授权通过"，3D/管理端可据此同步手动放行的车辆状态
            self.bus.publish(ManualAuthorized(
                vehicle_id=vehicle_id, plate=plate, gate="in", record_id=record_id,
                space_no=space_no, action="entry",
                reason="manual_confirm" if confirmed else (match_reason or "manual_authorize")))
        log.info("进场授权成功 %s → %s(record=%s, manual=%s)",
                 plate, space_no, record_id, manual)
        return {"record_id": record_id, "plate": plate, "space_no": space_no,
                "zone": zone}

    def _resolve_pending_for_vehicle(self, vehicle_id: Optional[str], *,
                                     gate: str = "in", operator: str = "system",
                                     status: str = "confirmed",
                                     plate: Optional[str] = None,
                                     resolution: str = "entry_authorized") -> list[int]:
        """入口授权成功后清理同 vehicle_id 的旧待办（confirmed/rejected，保留审计）。

        覆盖：黑名单后车恢复、人工 authorize-entry、自动重试成功等路径，避免前端
        pending_manual 残留。返回被处理的待办 id 列表。
        """
        if not vehicle_id or self.pending_repo is None:
            return []
        session_id = self.pending_repo.current_session_id()
        resolved: list[int] = []
        for item in self.pending_repo.list(session_id=session_id, status="pending"):
            if item.get("gate") != gate or item.get("vehicle_id") != vehicle_id:
                continue
            row = self.pending_repo.get(item["id"])
            if row is None or row.status != "pending":
                continue
            confirmed_plate = plate or row.source_plate
            with self.db.scope():
                self.pending_repo.resolve(
                    row, status=status, operator=operator,
                    plate=confirmed_plate, resolution=resolution)
                self.bus.publish(PendingResolved(
                    pending_id=row.id, gate=row.gate, vehicle_id=row.vehicle_id,
                    status=status, plate=confirmed_plate,
                    message="入口已授权，自动清理旧待办"))
                if self.oplog:
                    self.oplog.add(operator=operator, action="pending_resolve",
                                   target=str(row.id),
                                   detail={"vehicle_id": vehicle_id,
                                           "plate": confirmed_plate,
                                           "resolution": resolution})
            resolved.append(row.id)
        return resolved

    def _authorize_entry_from_expected(self, event: VehicleArrived) -> Optional[dict]:
        """demo + sim_camera + expected_plate 的兜底授权（旧帧不可读时恢复后车队列）。

        仅由 ``retry_waiting_vehicles(trust_expected=True)``（黑名单处置后）调用；
        expected_plate 必须语法合法，且 ``_authorize_entry`` 内仍执行黑名单/重复/满位校验。
        """
        if not getattr(self.cfg.server, "demo_mode", False):
            return None
        if getattr(event, "source", "legacy") != "sim_camera":
            return None
        expected = getattr(event, "expected_plate", None)
        if not expected:
            return None
        try:
            plate, color = normalize_valid_plate(expected)
        except ValueError:
            log.info("expected_plate 非法，跳过兜底授权: %r", expected)
            return None
        try:
            return self._authorize_entry(
                event.vehicle_id, plate, color,
                entry_photo=event.image_path, source="sim_camera",
                expected_plate=expected, plate_match=True,
                match_reason="expected_retry")
        except ParkingError as exc:
            log.info("expected_plate 兜底授权仍失败 vehicle=%s plate=%s: %s",
                     event.vehicle_id, expected, exc.to_dict())
            return None
        except Exception:  # noqa: BLE001 - 兜底失败不得中断重试队列
            log.exception("expected_plate 兜底授权异常 vehicle=%s", event.vehicle_id)
            return None

    # H3：授权后开闸动作失败不能静默（记录/车位已提交，不能回滚）
    AUTH_OPEN_ATTEMPTS = 3

    def _complete_entry_authorization_safe(self, record_id: int,
                                          vehicle_id: Optional[str],
                                          plate: str) -> None:
        """after-commit 安全回调：permit 只累加一次；开闸失败有限重试，仍失败发 incident。

        记录与车位已在事务中提交、**不回滚**；incident/oplog 提供可恢复审计，
        管理端可人工抬杆/清理。异常不再被 Database.scope 静默吞掉。
        """
        attempts = 0
        last_exc: Optional[BaseException] = None
        self._pending["in"] += 1
        try:
            self._persist_pending("in")
        except Exception:  # noqa: BLE001 - permit 持久化失败不应阻止开闸尝试
            log.exception("入口授权后 permit 持久化失败 record=%s", record_id)
        for attempt in range(1, self.AUTH_OPEN_ATTEMPTS + 1):
            attempts = attempt
            try:
                self.barriers["in"].request_open("entry", authorization="entry")
                if attempt > 1:
                    log.warning("入口授权后开门重试成功 record=%s attempt=%s",
                                record_id, attempt)
                return
            except Exception as exc:  # noqa: BLE001 - 继续有限重试
                last_exc = exc
                log.exception("入口授权后开门失败 record=%s plate=%s attempt=%s",
                              record_id, plate, attempt)
        detail = {
            "reason": "authorization_callback_failed",
            "record_id": record_id, "vehicle_id": vehicle_id,
            "plate": plate, "attempts": attempts,
            "error": repr(last_exc),
        }
        try:
            self.bus.publish(IncidentRaised(kind='barrier', gate='in',
                                            detail=detail))
        except Exception:  # noqa: BLE001 - incident 发布失败仍要记日志
            log.exception("入口授权失败 incident 发布失败 record=%s", record_id)
        if self.oplog:
            try:
                with self.db.scope():
                    self.oplog.add(operator='system',
                                   action='authorization_callback_failed',
                                   target=plate, detail=detail)
            except Exception:  # noqa: BLE001
                log.exception("入口授权失败 oplog 写入失败 record=%s", record_id)

    @synchronized
    def _complete_entry_authorization(self) -> None:
        self._pending["in"] += 1
        self._persist_pending("in")
        self.barriers["in"].request_open("entry", authorization="entry")

    # ----------------------------- 出口事件 -----------------------------
    def _process_exit_arrival(self, e: VehicleArrived) -> None:
        if getattr(e, "source", "legacy") == "upload":
            log.warning("忽略 upload 来源的出场业务事件 vehicle=%s", e.vehicle_id)
            return
        # M2：重复/迟到出口事件（同一车/牌刚完成出场）→ 幂等忽略，不生成待办/异常
        settled = self._recent_exit_record(
            getattr(e, "vehicle_id", None), getattr(e, "expected_plate", None))
        if settled is not None:
            self._drop_arrival(getattr(e, "vehicle_id", None))
            log.info("重复/迟到出口事件幂等忽略 vehicle=%s plate=%s record=%s",
                     e.vehicle_id, settled.plate, settled.id)
            return {"status": "settled", "idempotent": True,
                    "record_id": settled.id, "plate": settled.plate}
        if self._camera_down("out"):
            self._create_pending(e, reason="camera_fault", message="摄像头故障，请人工录入车牌")
            self._publish_recognized("out", e.vehicle_id, need_manual=True,
                                     reason="camera_fault", message="摄像头故障，请人工录入车牌",
                                     image_path=e.image_path,
                                     source=getattr(e, "source", "legacy"),
                                     expected_plate=getattr(e, "expected_plate", None),
                                     plate_match=None)
            return
        plate, color, conf, need_manual, candidates, crop, reason, log_id = self._recognize("out", e)
        plate_match = self._expected_plate_match(e, plate, color, conf)
        if getattr(e, "source", "legacy") == "sim_camera":
            if plate_match:
                need_manual = False
                reason = "expected_match"
                if log_id:
                    with self.db.scope():
                        self.recog_logs.mark_expected_match(log_id)
            else:
                need_manual = True
                reason = self._sim_manual_reason(e, plate)
        if need_manual:
            self._create_pending(e, reason=reason, message="识别待人工确认",
                                 plate=plate, color=color, candidates=candidates,
                                 recognition_log_id=log_id)
            self._publish_recognized("out", e.vehicle_id, plate=plate, color=color,
                                     conf=conf, need_manual=True,
                                     candidates=candidates,
                                     reason=reason,
                                     image_path=e.image_path, plate_crop_path=crop,
                                     source=getattr(e, "source", "legacy"),
                                     expected_plate=getattr(e, "expected_plate", None),
                                     plate_match=plate_match,
                                     message=self._manual_message(reason))
            return
        if self._is_blacklist(plate):
            # 实现 A：黑名单车辆出场不产生费用/不建普通记录（即使有遗留记录也只审计清理）
            self._handle_blacklist_exit(e.vehicle_id, plate, e.image_path, crop)
            return
        self._authorize_exit(e.vehicle_id, plate, color, conf,
                             image_path=e.image_path, crop=crop,
                             source=getattr(e, "source", "legacy"),
                             expected_plate=getattr(e, "expected_plate", None),
                             plate_match=plate_match)

    def _is_blacklist(self, plate: str) -> bool:
        try:
            return bool(self.lists and self.lists.active_type(plate) == "black")
        except Exception:  # noqa: BLE001 - 名单查询失败按非黑名单处理
            log.exception("查询黑名单失败 plate=%s", plate)
            return False

    # M2：同一车/牌刚完成出场的迟到/重复出口事件，在时间窗内视为幂等
    EXIT_IDEMPOTENT_WINDOW_SEC = 120.0

    def _recent_exit_record(self, vehicle_id: Optional[str],
                            plate: Optional[str]):
        """M2：返回时间窗内刚结算的最近记录；同牌/同车存在新 open 记录时不算重复。"""
        try:
            if plate and self.parking.find_open_by_plate(
                    plate, include_temporary=False) is not None:
                return None
            if vehicle_id and self.parking.find_open_by_vehicle(vehicle_id) is not None:
                return None
            session_id = (self.pending_repo.current_session_id()
                          if self.pending_repo is not None else None)
            rec = (self.parking.latest_closed(vehicle_id=vehicle_id,
                                              session_id=session_id)
                   if vehicle_id else None)
            if rec is None and plate:
                rec = self.parking.latest_closed(plate=plate,
                                                 session_id=session_id)
            if rec is None or rec.exit_time is None:
                return None
            age = (self._now() - rec.exit_time).total_seconds()
            if age <= self.EXIT_IDEMPOTENT_WINDOW_SEC:
                return rec
        except Exception:  # noqa: BLE001 - 幂等判定失败按正常流程处理
            log.exception("重复出口幂等判定失败 vehicle=%s plate=%s",
                          vehicle_id, plate)
        return None

    def _drop_arrival(self, vehicle_id: Optional[str]) -> None:
        for gate in ("in", "out"):
            self._arrivals.pop((gate, vehicle_id or ""), None)

    def _write_exit_evidence(self, rec, image_path: Optional[str],
                             crop: Optional[str]) -> None:
        """出场照片/裁剪写回记录（实现 A，先落库后动作）。"""
        if not image_path and not crop:
            return
        with self.db.scope():
            if image_path:
                rec.exit_photo = image_path
            if crop:
                rec.exit_plate_crop = crop
            self.db.session.flush()

    @synchronized
    def _auto_settle_exit(self, rec, *, image_path: Optional[str] = None,
                          crop: Optional[str] = None,
                          operator: str = "system") -> dict:
        """正常有记录车辆出场自动结算（实现 A AutoPaid）。

        - 按现有费率计算（免费/白名单/月租/新能源/封顶均沿用 billing）；
        - 立即生成 PaymentRecord(method=auto，金额含 0) → PaymentCompleted →
          status=out/释放车位/出口开闸；重复调用/已支付幂等，不重复扣费；
        - 出场照片/裁剪写回记录。
        """
        if self.billing is None:
            return {"record_id": rec.id, "plate": rec.plate, "status": "manual_billing"}
        self._write_exit_evidence(rec, image_path, crop)
        quote = self.billing.quote_exit(rec, exit_photo=image_path, exit_crop=crop)
        self.bus.publish(FeeQuoted(
            record_id=rec.id, vehicle_id=rec.vehicle_id, plate=rec.plate,
            minutes=quote.minutes, amount=quote.amount_due,
            entry_photo=rec.entry_photo, exit_photo=image_path))
        payment = self.billing.pay(rec.id, method="auto", operator=operator)
        fresh = self.parking.get(rec.id)
        if fresh is not None and fresh.status == "in":
            # 兜底：若事件订阅未生效（纯单元测试构造 service），直接完成出场事务
            self.on_payment_completed(PaymentCompleted(
                record_id=rec.id, plate=fresh.plate, amount=payment.amount_paid,
                method=payment.method, vehicle_id=fresh.vehicle_id))
            fresh = self.parking.get(rec.id)
        settlement = {
            "record_id": rec.id,
            "plate": (fresh.plate if fresh is not None else rec.plate),
            "payment_id": payment.id,
            "amount_due": payment.amount_due,
            "amount_paid": payment.amount_paid,
            "minutes": payment.minutes,
            "method": payment.method,
            "status": (fresh.status if fresh is not None else "out"),
            "space_no": (fresh.space_no if fresh is not None else rec.space_no),
            "auto_settled": True,
        }
        log.info("出口自动结算 record=#%s plate=%s 金额=%.2f 方式=%s status=%s",
                 rec.id, settlement["plate"], payment.amount_paid,
                 payment.method, settlement["status"])
        return settlement

    @synchronized
    def _handle_blacklist_exit(self, vehicle_id: Optional[str], plate: str,
                               image_path: Optional[str],
                               crop: Optional[str]) -> dict:
        """黑名单车辆出场：不收费、不建普通记录；遗留记录仅状态审计+释放车位。"""
        rec = self.parking.find_open_by_plate(plate, include_temporary=True)
        cleared = None
        if rec is not None:
            exit_sim_sec = self._sim_seconds()
            duration_sim_sec = None
            if exit_sim_sec is not None and rec.entry_sim_sec is not None:
                duration_sim_sec = round(float(exit_sim_sec) - float(rec.entry_sim_sec), 3)
            with self.db.scope():
                if image_path:
                    rec.exit_photo = image_path
                if crop:
                    rec.exit_plate_crop = crop
                self.parking.mark_exit(rec, exit_time=self._now(), status="abnormal",
                                       exit_photo=image_path, exit_plate_crop=crop,
                                       exit_sim_sec=exit_sim_sec,
                                       duration_sim_sec=duration_sim_sec)
                rec.aborted_reason = "blacklist_exit"
                self.allocator.release(rec.space_no)
                self.bus.publish(SpaceReleased(space_no=rec.space_no or ""))
            cleared = rec.id
        self.bus.publish(IncidentRaised(kind="data", gate="out", detail={
            "reason": "blacklist_exit", "plate": plate,
            "record_id": cleared, "vehicle_id": vehicle_id}))
        self._publish_recognized("out", vehicle_id, plate=plate, need_manual=False,
                                 reason="blacklist_exit",
                                 message="黑名单车辆出场：不收费、按黑名单审计",
                                 image_path=image_path, plate_crop_path=crop)
        log.info("黑名单车辆出场不收费 plate=%s cleared_record=%s", plate, cleared)
        return {"record_id": cleared, "plate": plate, "status": "blacklist_exit",
                "cleared_record_id": cleared, "amount_paid": 0.0}

    @synchronized
    def _authorize_exit(self, vehicle_id: Optional[str], plate: str, color: str,
                        conf: float, *, image_path: Optional[str] = None,
                        crop: Optional[str] = None, confirmed: bool = False,
                        exit_policy=None, source: str = "legacy",
                        expected_plate: Optional[str] = None,
                        plate_match: Optional[bool] = None) -> dict:
        plate, color = self._validated_plate(plate, color)
        rec = self.parking.find_open_by_plate(plate, include_temporary=True)
        policy = exit_policy or self.cfg.access.exit_no_record_policy
        if rec is None and vehicle_id:
            from ..repository.models import ParkingRecord
            previous = self.db.session.query(ParkingRecord).filter_by(
                vehicle_id=vehicle_id, plate=plate, temporary=True,
                session_id=self.pending_repo.current_session_id()).order_by(ParkingRecord.id.desc()).first()
            if previous is not None and previous.status != "in":
                return {"record_id":previous.id,"plate":plate,"status":"settled"}
        if rec is None:
            settled = self._recent_exit_record(vehicle_id, plate)
            if settled is not None:
                self._drop_arrival(vehicle_id)
                log.info("重复/迟到出口授权幂等 vehicle=%s plate=%s record=%s",
                         vehicle_id, plate, settled.id)
                return {"record_id": settled.id, "plate": settled.plate,
                        "status": "settled", "idempotent": True}
            # 实现 A：无入场记录**禁止静默 auto**，统一转人工异常流程：
            # 待办中由人工选择 免费/作废/计费/纠正；无记录不自动扣费、不开闸。
            self.bus.publish(IncidentRaised(kind="data",gate="out",detail={
                "reason":"exit_without_record","plate":plate,"policy":policy}))
            self._create_pending(VehicleArrived(vehicle_id=vehicle_id or "manual-exit",gate="out",image_path=image_path),
                reason="exit_without_record",message="无入场记录，请选择计费、免费或作废",plate=plate,color=color)
            self._publish_recognized("out",vehicle_id,plate=plate,color=color,conf=conf,
                need_manual=True,reason="exit_without_record",message="无入场记录，请人工选择处理方式",
                image_path=image_path, plate_crop_path=crop,
                source=source, expected_plate=expected_plate, plate_match=plate_match)
            return {"plate":plate,"status":"pending"}
        if rec.status != "in":
            return {"record_id":rec.id,"plate":plate,"status":"settled"}
        # 实现 A：黑名单车辆即使有遗留记录也不收费/不建普通记录
        if self._is_blacklist(plate):
            return self._handle_blacklist_exit(vehicle_id, plate, image_path, crop)
        self._publish_recognized("out", vehicle_id, plate=plate, color=color,
                                 conf=conf, need_manual=False,
                                 reason="confirmed" if confirmed else "ok",
                                 image_path=image_path, plate_crop_path=crop,
                                 source=source, expected_plate=expected_plate,
                                 plate_match=plate_match)
        # 实现 A：所有正常有记录车辆出场立即自动结算（含 0 元），
        # 不再等待 /api/pay 或人工收费；/api/pay 仍幂等兼容（返回已存在支付）。
        if self.billing is not None:
            try:
                return self._auto_settle_exit(
                    rec, image_path=image_path, crop=crop, operator="system")
            except Exception:  # noqa: BLE001 - 自动结算失败转人工待办，避免静默放行
                if confirmed:
                    raise
                log.exception("出口自动结算失败 plate=%s", plate)
                self.bus.publish(IncidentRaised(
                    kind="data", gate="out",
                    detail={"plate": plate, "reason": "auto_settle_failed"}))
                self._create_pending(
                    VehicleArrived(vehicle_id=vehicle_id or "manual-exit",
                                   gate="out", image_path=image_path),
                    reason="auto_settle_failed",
                    message="自动结算失败，请人工处理", plate=plate, color=color)
                self._publish_recognized(
                    "out", vehicle_id, plate=plate, color=color, conf=conf,
                    need_manual=True, reason="auto_settle_failed",
                    message="自动结算失败，请人工处理", image_path=image_path,
                    plate_crop_path=crop, source=source,
                    expected_plate=expected_plate, plate_match=plate_match)
                return {"plate": plate, "status": "pending"}
        log.info("未接入 billing，出口 %s 待人工/后接模块结算", plate)
        return {"record_id": rec.id, "plate": plate, "status": "manual_billing"}

    @synchronized
    def on_payment_completed(self, e: PaymentCompleted) -> None:
        """缴费完成 → 落库(标记出场/释放车位) → 抬杆（先落库后动作）。"""
        rec = self.parking.get(e.record_id)
        if rec is None:
            log.error("PaymentCompleted 指向不存在记录 #%s", e.record_id)
            return
        if rec.status != "in":
            log.info("记录 #%s 已出场，幂等忽略", e.record_id)
            return
        space_no = rec.space_no
        exit_sim_sec = self._sim_seconds()
        duration_sim_sec = None
        if exit_sim_sec is not None and rec.entry_sim_sec is not None:
            duration_sim_sec = round(float(exit_sim_sec) - float(rec.entry_sim_sec), 3)
        with self.db.scope():
            self.parking.mark_exit(rec, exit_time=self._now(), status="out",
                                   exit_sim_sec=exit_sim_sec,
                                   duration_sim_sec=duration_sim_sec)
            self.allocator.release(space_no)
            self.bus.publish(SpaceReleased(space_no=space_no or ""))
            self.db.on_commit(lambda: self._complete_exit_authorization("payment"))
        log.info("缴费完成，出场抬杆 plate=%s record=%s", rec.plate, e.record_id)

    def _complete_exit_authorization(self, reason):
        self._pending["out"] += 1
        self._persist_pending("out")
        self.barriers["out"].request_open(reason, authorization="payment")

    @synchronized
    def on_vehicle_passed(self, e: VehiclePassedGate) -> None:
        """车辆完全越过闸杆平面（FR-4.4）：重置保持计时 + 递减 permit。"""
        b = self.barriers.get(e.gate)
        if b is not None:
            b.on_vehicle_passed()
        if self._pending.get(e.gate, 0) > 0:
            self._pending[e.gate] -= 1
            self._persist_pending(e.gate)

    @synchronized
    def on_zone_occupied(self, e: ZoneOccupied) -> None:
        gate = _ZONE_TO_GATE.get(e.zone_id, e.zone_id)
        b = self.barriers.get(gate)
        if b is not None:
            sources=self._zone_sources.setdefault(gate,{})
            sources[e.owner_id or "legacy"] = e.occupied
            b.on_zone_occupied(any(sources.values()), e.vehicle_id)

    @synchronized
    def on_vehicle_left(self, e: VehicleLeft) -> None:
        """车辆驶离兜底收尾（H4）：按 vehicle_id/plate 找 open 记录并幂等收敛。

        - 无 open 记录 / 已 out：幂等直接返回，不重复收费/释放；
        - 有 open 且已有支付 → out + ``vehicle_left_after_payment``；
        - 有 open 且未支付 → abnormal + ``vehicle_left_without_exit`` + incident；
        - 释放车位并发 SpaceReleased（仅一次）。
        """
        for gate in ("in", "out"):
            self._arrivals.pop((gate, e.vehicle_id), None)
        vehicle_id = getattr(e, "vehicle_id", None)
        plate = getattr(e, "plate", None)
        rec = self.parking.find_open_by_vehicle(vehicle_id) if vehicle_id else None
        if rec is None and plate:
            rec = self.parking.find_open_by_plate(plate, include_temporary=False)
        if rec is None:
            log.debug("车辆驶离无 open 记录 gate=%s vehicle=%s plate=%s（幂等）",
                      e.gate, vehicle_id, plate)
            return
        if rec.status != "in":
            log.info("车辆驶离幂等：记录 #%s 已结束(%s)", rec.id, rec.status)
            return
        payment = None
        if self.billing is not None:
            try:
                payment = self.billing.payment.get_by_parking(rec.id)
            except Exception:  # noqa: BLE001 - 查询支付失败按未支付处理（异常路径更安全）
                log.exception("车辆驶离查询支付失败 record=%s", rec.id)
        paid = payment is not None
        space_no = rec.space_no
        with self.db.scope():
            if paid:
                self.parking.mark_exit(rec, exit_time=self._now(), status='out',
                                       exit_sim_sec=self._sim_seconds())
                rec.aborted_reason = None
            else:
                self.parking.mark_exit(rec, exit_time=self._now(), status='abnormal',
                                       exit_sim_sec=self._sim_seconds())
                rec.aborted_reason = 'vehicle_left_without_exit'
            if space_no:
                self.allocator.release(space_no)
                self.bus.publish(SpaceReleased(space_no=space_no))
            if self._pending.get(e.gate, 0) > 0:
                self._pending[e.gate] -= 1
                self._persist_pending(e.gate)
        action = 'vehicle_left_after_payment' if paid else 'vehicle_left_without_exit'
        if self.oplog:
            try:
                with self.db.scope():
                    self.oplog.add(operator='system', action=action,
                                   target=rec.plate,
                                   detail={'record_id': rec.id,
                                           'vehicle_id': vehicle_id,
                                           'plate': rec.plate,
                                           'gate': e.gate,
                                           'space_no': space_no})
            except Exception:  # noqa: BLE001
                log.exception("车辆驶离 oplog 写入失败 record=%s", rec.id)
        if not paid:
            self.bus.publish(IncidentRaised(
                kind='data', gate=e.gate,
                detail={'reason': 'vehicle_left_without_exit',
                        'record_id': rec.id, 'vehicle_id': vehicle_id,
                        'plate': rec.plate, 'space_no': space_no}))
        log.info("车辆驶离兜底收尾 record=#%s plate=%s paid=%s status=%s",
                 rec.id, rec.plate, paid, rec.status)

    @synchronized
    def retry_waiting_vehicles(self, gate: str, operator: str = "system", *,
                               include_all: bool = False,
                               resolution: str = "camera_recovered_retry",
                               trust_expected: bool = False) -> dict:
        """自动重试该闸口待办车辆（摄像头恢复 P1-07 / 黑名单处置后后车恢复）。

        - ``include_all=False``（默认）：仅重试 ``camera_fault``（兼容摄像头恢复旧行为）；
        - ``include_all=True``：重试该 gate 的全部 pending（黑名单处置后自动恢复后车）；
        - 旧待办先置 rejected（保留审计），再复用最近一次 arrival；没有 arrival 时用
          待办保存的图像作为"新帧"重新识别；
        - 成功走正常授权；仍失败会重新生成待办，不会静默放行；
        - 单辆车失败只影响自己（不阻塞同队列其它车辆），返回失败计数；
        - ``trust_expected=True``（仅黑名单处置后的后车恢复）：旧帧仍低置信时，若该 arriving
          事件来自 3D 仿真相机且带 ``expected_plate``，在 demo 模式下用 expected_plate 完成
          授权（仍走完整重复/黑名单/满位校验），避免旧帧不可读导致永久卡住。
        """
        if gate not in ("in", "out"):
            raise ValidationError("gate 应为 in/out")
        empty = {"ok": True, "gate": gate, "retried": 0, "authorized": 0,
                 "failed": 0, "pending_ids": [], "retried_vehicle_ids": []}
        if self.pending_repo is None:
            return {**empty, "reason": "pending_repo_missing"}
        if self._camera_down(gate):
            return {**empty, "ok": False, "reason": "camera_down"}
        session_id = self.pending_repo.current_session_id()
        targets = [p for p in self.pending_repo.list(session_id=session_id, status="pending")
                   if p.get("gate") == gate
                   and (include_all or p.get("reason") == "camera_fault")]
        if not targets:
            return empty
        retried: list[int] = []
        retried_vehicles: list[str] = []
        authorized = 0
        failed = 0
        expected_authorized = 0
        for item in targets:
            row = self.pending_repo.get(item["id"])
            if row is None or row.status != "pending":
                continue
            event = self._arrivals.get((gate, row.vehicle_id or ""))
            with self.db.scope():
                self.pending_repo.resolve(
                    row, status="rejected", operator=operator,
                    resolution=resolution)
                self.bus.publish(PendingResolved(
                    pending_id=row.id, gate=row.gate, vehicle_id=row.vehicle_id,
                    status="rejected", message="自动重试识别（原待办保留审计）"))
            if event is None:
                event = VehicleArrived(
                    vehicle_id=row.vehicle_id or f"retry-{row.id}",
                    gate=gate, image_path=row.image_path, source="auto_retry")
            # 复用最近 arrival（含 expected_plate/source），走正常识别/人工分支。
            try:
                if gate == "in":
                    self._process_entry(event)
                else:
                    self._process_exit_arrival(event)
            except ParkingError as exc:
                log.info("自动重试仍失败 gate=%s vehicle=%s: %s",
                         gate, row.vehicle_id, exc.to_dict())
                if isinstance(exc, DataAnomaly) and exc.kind == "blacklist":
                    self.bus.publish(EntryDenied(
                        vehicle_id=event.vehicle_id, gate=gate,
                        plate=exc.detail.get("plate"), reason="blacklist",
                        message=exc.message, image_path=event.image_path))
                    self._publish_recognized(gate, event.vehicle_id,
                                             plate=exc.detail.get("plate"),
                                             need_manual=False, reason="blacklist",
                                             message=exc.message,
                                             image_path=event.image_path)
                else:
                    self._create_pending(event, reason=exc.code, message=exc.message)
                    self._publish_recognized(gate, event.vehicle_id, need_manual=True,
                                             reason=exc.code, message=exc.message,
                                             image_path=event.image_path)
            except Exception:  # noqa: BLE001 - 单辆失败不得阻塞后续队列
                log.exception("自动重试异常 gate=%s vehicle=%s", gate, row.vehicle_id)
            # 结果判定：同 vehicle 不再有 pending → 已授权；仍 pending → 失败再入队
            still = [p for p in self.pending_repo.list(session_id=session_id,
                                                       status="pending")
                     if p.get("gate") == gate
                     and (row.vehicle_id or "") == (p.get("vehicle_id") or "")]
            if still and trust_expected and gate == "in":
                # 旧帧不可读（如后车排在前车后面拍的帧）但 3D 已给出期望车牌：
                # demo 模式下用 expected_plate 补授权（完整校验仍生效），保证队列自动恢复。
                fallback = self._authorize_entry_from_expected(event)
                if fallback is not None:
                    self._resolve_pending_for_vehicle(
                        event.vehicle_id, gate="in", operator="system",
                        plate=fallback.get("plate"),
                        resolution="expected_retry_authorized")
                    still = []
                    expected_authorized += 1
                    log.info("后车旧帧低置信，已按 expected_plate 自动授权 vehicle=%s plate=%s",
                             event.vehicle_id, fallback.get("plate"))
            if still:
                failed += 1
                # 旧帧不可用且无法 expected 兜底：请求 3D 重新抓拍/上报 arrival，
                # 保证前端有明确恢复路径（新帧到达后走正常识别/授权）。
                self.bus.publish(ArrivalRetryRequested(
                    vehicle_id=event.vehicle_id, gate=gate,
                    reason="retry_failed",
                    message="旧帧仍无法识别，请重新抓拍并上报 arrival"))
            else:
                authorized += 1
            retried.append(row.id)
            if row.vehicle_id:
                retried_vehicles.append(row.vehicle_id)
        log.info("闸口自动重试 gate=%s 待办=%s 成功=%s 失败=%s 操作员=%s",
                 gate, retried, authorized, failed, operator)
        return {"ok": True, "gate": gate, "retried": len(retried),
                "authorized": authorized, "failed": failed,
                "expected_authorized": expected_authorized,
                "pending_ids": retried, "retried_vehicle_ids": retried_vehicles}

    @synchronized
    def on_barrier_ack(self, gate: str, ack_state: str,
                       cmd_id: Optional[str] = None, *, require_cmd: bool = False,
                       arm_position: Optional[float] = None) -> bool:
        """前端动画完成回执（gateway 收到 WS barrier_ack 后调用）。"""
        b = self.barriers.get(gate)
        if b is not None:
            if ack_state == "stopped" and (arm_position is None or not 0 <= arm_position <= 1):
                return False
            accepted_id = b._ack_cmd_id
            with self.db.scope():
                if b.on_ack(ack_state, cmd_id, require_cmd=require_cmd) and accepted_id:
                    action = self.db.session.query(BarrierAction).filter_by(
                        cmd_id=accepted_id).first()
                    if action is not None:
                        action.status = "acked"
                        action.acked_at = utcnow()
                    row = self.db.session.get(BarrierStateRow, gate)
                    if row is None:
                        row = BarrierStateRow(gate=gate, state=b.state.value,
                                              arm_position=0, pending=0, updated_at=utcnow())
                        self.db.session.add(row)
                    row.state = b.state.value
                    row.keep_open = b.keep_open
                    row.fault = b.fault_mode
                    row.updated_at = utcnow()
                    if arm_position is not None:
                        row.arm_position = arm_position
                    self.bus.publish(BarrierStateChanged(gate=gate, state=b.state.value,
                        cmd_id=accepted_id, keep_open=b.keep_open, arm_position=arm_position,
                        manual=b._manual, passable=b.is_passable(),
                        authorization=b._authorization))
                    return True
        return False

    # ----------------------------- 人工干预（异常流） -----------------------------
    def manual_open(self, gate: str, operator: str, reason: str) -> dict:
        """人工抬杆并保持（含故障手动曲柄）：只操作物理闸杆，**不产生业务授权/记录**。

        P1：抬杆后 keep_open=True 并持久化，保持 KEEP_OPEN 直到管理员落杆
        （关闭且 keep_open=false）或恢复/重置；故障人工抬杆同样保持。
        """
        return self.operate_barrier(gate, "open", operator=operator,
                                    reason=reason or "manual_open", hold=True)

    @synchronized
    def operate_barrier(self, gate: str, action: str, *, operator: str,
                        reason: str = "manual", enabled: bool = False,
                        mode: str = "ack_timeout", hold: bool = False) -> dict:
        b = self.barriers.get(gate)
        if b is None:
            raise ValidationError(f"未知闸口 {gate}")
        if action not in ("open", "close", "stop", "keep_open", "clear_fault", "inject"):
            raise ValidationError("未知道闸操作")
        if mode not in ("ack_timeout", "stuck_open", "stuck_closed"):
            raise ValidationError("未知故障类型")
        # 实现 A：FAULT 下允许人工抬杆（手动曲柄）与清故障/重新注入故障；
        # 人工抬杆只让闸杆物理可通行，业务授权仍需正常流程完成。
        if b.state.value == "FAULT" and action not in ("open", "clear_fault", "inject"):
            raise StateConflict("barrier_fault", "请先排除故障并清故障")
        if action == "close" and b.zone_occupied:
            raise StateConflict("zone_occupied", "杆区有车辆，禁止落杆")
        before = (b.state, b.keep_open)
        hold_requested = bool(hold) and action == "open"
        with self.db.scope():
            if action == "open":
                if hold:
                    # P1：人工抬杆保持（keep_open=True，直到人工落杆/恢复/重置）
                    b.manual_hold(reason)
                else:
                    # 普通一次性人工抬杆（黑名单绕行等，tick 后可自动落杆）
                    b.manual_lift(reason)
            elif action == "close":
                b.keep_open = False
                b.request_close(manual=True, reason=reason)
            elif action == "stop":
                b.request_stop(reason)
            elif action == "keep_open":
                b.set_keep_open(enabled, manual=True)
            elif action == "inject":
                b.inject_fault(mode)
            else:
                b.clear_fault()
                for incident in self.incidents.open_by_kind("barrier", gate):
                    self.incidents.resolve(incident.id, operator=operator,
                                           resolution=reason or "设备已修复")
                    self.bus.publish(IncidentResolved(kind="barrier", incident_id=incident.id,
                                                       resolution=reason or "设备已修复"))
            if self.oplog:
                self.oplog.add(operator=operator, action="barrier_"+action,
                               target=gate, detail={"reason": reason, "enabled": enabled, "mode": mode})
            row = self.db.session.get(BarrierStateRow, gate)
            if row is None:
                row = BarrierStateRow(gate=gate, state=b.state.value, arm_position=0,
                                      pending=self._pending.get(gate, 0),
                                      updated_at=utcnow())
                self.db.session.add(row)
            row.state = b.state.value
            row.keep_open = b.keep_open
            row.fault = b.fault_mode
            row.updated_at = utcnow()
            if b._ack_cmd_id:
                item = self.db.session.query(BarrierAction).filter_by(cmd_id=b._ack_cmd_id).first()
                if item is not None:
                    item.operator = operator
            from sqlalchemy import func
            from ..repository.models import EventLog
            seq = self.db.session.query(func.max(EventLog.seq)).scalar() or 0
        return {"ok": True, "gate": gate, "action": action,
                "state": b.state.value,
                "changed": before != (b.state, b.keep_open), "cmd_id": b._ack_cmd_id,
                "keep_open": b.keep_open,
                # 实现 A：物理可通行/人工操作/授权来源（manual ≠ 业务授权）
                "manual": b._manual, "passable": b.is_passable(),
                "authorization": b._authorization, "reason": reason,
                "hold": hold_requested, "seq": seq}

    @synchronized
    def blacklist_gate(self, vehicle_id: str, plate: str, gate: str, action: str,
                       operator: str = "sim") -> dict:
        """黑名单车辆绕行闸杆（实现 A）：只操作闸杆，**不建 parking_record**。

        供 3D 在"无后车倒车离场 / 有后车场内绕行"时调用；open/close 走正常道闸指令与
        回执，reason=blacklist_bypass，仅 incident/recognition/blacklist-resolution 审计。
        """
        plate, _color = self._validated_plate(plate)
        if gate not in ("in", "out"):
            raise ValidationError("gate 应为 in/out")
        if action not in ("open", "close"):
            raise ValidationError("action 应为 open/close")
        is_black = bool(self.lists and self.lists.active_type(plate) == "black")
        if not is_black:
            raise StateConflict("not_blacklisted",
                                f"车牌 {plate} 不在黑名单，禁止使用黑名单绕行通道")
        # 实现 A：黑名单绕行绝不调用自动计费；若有遗留 open 记录，仅状态审计并释放，
        # 不生成 PaymentRecord（0 费用）。
        cleared_record_id = None
        if gate == "out" and action == "open":
            rec = self.parking.find_open_by_plate(plate, include_temporary=True)
            if rec is not None:
                with self.db.scope():
                    self.parking.mark_exit(rec, exit_time=self._now(),
                                           status="abnormal")
                    rec.aborted_reason = "blacklist_exit"
                    self.allocator.release(rec.space_no)
                    self.bus.publish(SpaceReleased(space_no=rec.space_no or ""))
                cleared_record_id = rec.id
                if self.oplog:
                    self.oplog.add(operator=operator, action="blacklist_exit_clear",
                                   target=plate, detail={"record_id": rec.id,
                                                         "reason": "blacklist_bypass"})
        result = self.operate_barrier(gate, action, operator=operator,
                                      reason="blacklist_bypass", hold=False)
        result.update({"vehicle_id": vehicle_id, "plate": plate,
                       "reason": "blacklist_bypass",
                       "cleared_record_id": cleared_record_id})
        log.info("黑名单绕行闸杆 plate=%s gate=%s action=%s state=%s passable=%s",
                 plate, gate, action, result.get("state"), result.get("passable"))
        return result

    @synchronized
    def correct_exit(self, *, plate: str, record_id: Optional[int] = None,
                     entry_sim_sec: Optional[float] = None,
                     action: str = "auto", reason: str = "",
                     operator: str = "admin", request_id: Optional[str] = None,
                     exit_photo: Optional[str] = None) -> dict:
        """异常出口纠正（冻结契约）：纠正入场车牌/时长 → 正常 AutoPaid 结算出发。

        - 有 ``record_id``：校验记录 open；``plate`` 即正确车牌（与其他 open 冲突→409
          duplicate_entry）；更新 plate/可选 entry_sim_sec 后按正常逻辑自动结算；
        - 无 ``record_id``：先按 ``plate`` 找 open 记录（找到→同上）；找不到时
          ``action=free|void`` + reason 走临时账单放行，``action=auto`` → 409 no_parking_record；
        - ``request_id`` 幂等（相同参数返回上次结果）。
        """
        from ..repository.models import ResetRequest

        plate, _ = self._validated_plate(plate)
        if action not in ("auto", "free", "void"):
            raise ValidationError("action 应为 auto/free/void")
        if action in ("free", "void") and not reason.strip():
            raise ValidationError("免费/作废必须填写原因")
        body = json.dumps({"plate": plate, "record_id": record_id,
                           "entry_sim_sec": entry_sim_sec, "action": action,
                           "reason": reason}, sort_keys=True, default=str)
        key = "correct-exit:" + request_id if request_id else None
        with self.db.scope():
            previous = self.db.session.get(ResetRequest, key) if key else None
            if previous is not None:
                if previous.request_body != body:
                    raise StateConflict("idempotency_conflict",
                                        "重复请求参数不同")
                return json.loads(previous.response)
            rec = (self.parking.get(record_id) if record_id is not None
                   else self.parking.find_open_by_plate(plate, include_temporary=False))
            if rec is not None and rec.status != "in":
                raise StateConflict("record_not_open",
                                    f"记录 #{rec.id} 已结束，不能纠正出场")
            if rec is None:
                if action == "auto":
                    open_records = [{"id": row.id, "plate": row.plate,
                                     "space_no": row.space_no,
                                     "entry_sim_sec": row.entry_sim_sec}
                                    for row in self.parking.all_open()[:20]]
                    error = NoParkingRecordError(plate)
                    error.detail["open_records"] = open_records
                    raise error
                # 无记录 + free/void：临时账单放行（不建普通在场记录、0/异常收费）
                request_key = f"correct-tmp-{request_id or plate}-{action}"
                result = self.manual_exit(plate, operator, reason, action=action,
                                          request_id=request_key)
                settlement = {
                    "action": action,
                    "record_id": result.get("record_id"),
                    "payment_id": result.get("payment_id"),
                    "amount_paid": result.get("amount_paid") or 0.0,
                    "space_no": result.get("space_no"),
                    "temporary": bool(result.get("temporary", True)),
                    "reason": reason,
                    "status": result.get("status", "out"),
                    "plate": plate,
                }
                self.bus.publish(ExitAuthorized(
                    vehicle_id=None, plate=plate,
                    record_id=settlement["record_id"], action=action,
                    amount_paid=settlement["amount_paid"],
                    space_no=settlement["space_no"], reason=reason))
                response = {"ok": True,
                            "record_id": settlement["record_id"],
                            "plate": plate,
                            "status": settlement["status"],
                            "settlement": settlement}
                if key:
                    self.db.session.add(ResetRequest(
                        request_id=key, request_body=body,
                        response=json.dumps(response, ensure_ascii=False)))
                return response
            old_plate = rec.plate
            if rec.plate != plate:
                if self._is_blacklist(plate):
                    raise ValidationError("纠正后的车牌在黑名单，需走黑名单出场流程")
                other = self.parking.find_open_by_plate(plate, include_temporary=False)
                if other is not None and other.id != rec.id:
                    raise DuplicateEntryError(plate)
                with self.db.scope():
                    rec.plate = plate
                    self.db.session.flush()
            if entry_sim_sec is not None:
                sim_now = self._sim_seconds()
                if sim_now is None:
                    raise StateConflict("invalid_entry_time",
                                        "入口仿真时间仅在演示模式可用")
                try:
                    value = float(entry_sim_sec)
                except (TypeError, ValueError) as exc:
                    raise StateConflict("invalid_entry_time",
                                        "entry_sim_sec 必须为数字") from exc
                if value < 0 or value > sim_now + 1e-6:
                    raise StateConflict(
                        "invalid_entry_time",
                        "入口仿真时间必须在 [0, 当前仿真秒] 之间")
                with self.db.scope():
                    rec.entry_sim_sec = value
                    rec.entry_time = self._sim_to_datetime(value)
                    rec.duration_sim_sec = None
                    self.db.session.flush()
            if self.oplog:
                with self.db.scope():
                    self.oplog.add(operator=operator, action="correct_exit",
                                   target=plate,
                                   detail={"record_id": rec.id,
                                           "old_plate": old_plate,
                                           "entry_sim_sec": entry_sim_sec,
                                           "reason": reason})
            settled = self._auto_settle_exit(
                rec, image_path=exit_photo or rec.exit_photo, operator=operator)
            settlement = {
                "action": "auto",
                "record_id": settled.get("record_id"),
                "payment_id": settled.get("payment_id"),
                "amount_paid": settled.get("amount_paid") or 0.0,
                "amount_due": settled.get("amount_due") or 0.0,
                "status": settled.get("status", "out"),
                "space_no": settled.get("space_no"),
                "temporary": bool(rec.temporary),
                "reason": reason,
                "old_plate": old_plate,
                "corrected": old_plate != plate,
            }
            response = {"ok": True,
                        "record_id": settlement["record_id"],
                        "plate": plate,
                        "status": settlement["status"],
                        "settlement": settlement}
            if self.oplog and old_plate != plate:
                pass
            if key:
                self.db.session.add(ResetRequest(
                    request_id=key, request_body=body,
                    response=json.dumps(response, ensure_ascii=False)))
            log.info("异常出口纠正结算 record=#%s %s→%s 金额=%s status=%s",
                     settlement["record_id"], old_plate, plate,
                     settlement["amount_paid"], settlement["status"])
            return response

    def list_open_records(self) -> list[dict]:
        """列出当前开放记录（供管理端异常纠正/手工出场选择）。"""
        items: list[dict] = []
        for rec in self.parking.all_open():
            item = {
                "record_id": rec.id, "plate": rec.plate,
                "plate_color": rec.plate_color, "vehicle_id": rec.vehicle_id,
                "space_no": rec.space_no,
                "entry_time": iso_local(rec.entry_time),
                "entry_sim_sec": rec.entry_sim_sec,
                "temporary": rec.temporary,
                "benefit_type": rec.benefit_type,
                "manual_entry": bool(rec.manual_entry),
            }
            if self.billing is not None:
                try:
                    quote = self.billing.quote(rec.plate)
                    item.update({"minutes": quote.minutes,
                                 "amount_due": quote.amount_due,
                                 "duration_sim_sec": quote.duration_sim_sec})
                except Exception:  # noqa: BLE001 - 单条报价失败不影响列表
                    log.exception("开放记录报价失败 record=%s", rec.id)
            items.append(item)
        return items

    @synchronized
    def authorize_entry(self, vehicle_id: Optional[str], plate: str, *,
                        color: str = "blue", operator: str = "admin") -> dict:
        """人工授权入场（实现 B）：建记录/分配车位/发 SpaceAssigned/开入口。

        与 ``manual_open`` 的区别：后者只物理抬杆、不产生业务记录；本方法走完整入场
        业务（含重复/黑名单/满位校验），供管理端在"无识别结果但需放行"时使用。
        """
        result = self._authorize_entry(vehicle_id, plate, color, manual=True,
                                       operator=operator, match_reason="manual_authorize")
        # 授权成功后清理同 vehicle_id 的旧待办（confirmed 保留审计），测试/前端断言 pending=0
        result["resolved_pending"] = self._resolve_pending_for_vehicle(
            vehicle_id, gate="in", operator=operator,
            plate=result.get("plate", plate), resolution="manual_authorized")
        return result

    @synchronized
    def authorize_exit(self, vehicle_id: Optional[str], plate: str, *,
                       action: str = "charge", reason: str = "",
                       operator: str = "admin",
                       request_id: Optional[str] = None) -> dict:
        """人工授权出场（实现 B）：结算/免费/作废 → PaymentCompleted → 开出口。

        - ``charge``：有在场记录才可计费；无记录抛 ``NoParkingRecordError``（明确错误，
          不会静默卡死），此时可改用 ``free``/``void`` 走临时账单放行；
        - ``free``/``void``：无记录也可放行（临时账单，必须填写原因）；
        - 无车位记录（临时账单/temporary）也能正常结算，不依赖 space_no。
        """
        plate, _color = self._validated_plate(plate)
        if action not in ("charge", "free", "void"):
            raise ValidationError("action 应为 charge/free/void")
        if action in ("free", "void") and not reason.strip():
            raise ValidationError("免费/作废必须填写原因")
        rec = self.parking.find_open_by_plate(plate, include_temporary=True)
        if rec is None:
            if action == "charge":
                raise NoParkingRecordError(plate)
            request_id = request_id or f"authorize-exit-{plate}-{action}"
            result = self.manual_exit(plate, operator, reason, action=action,
                                      request_id=request_id)
            self.bus.publish(ExitAuthorized(
                vehicle_id=vehicle_id, plate=plate, record_id=result.get("record_id"),
                action=action, amount_paid=result.get("amount_paid"),
                space_no=result.get("space_no"), reason=reason))
            return result
        if vehicle_id and not rec.vehicle_id:
            with self.db.scope():
                rec.vehicle_id = vehicle_id
        if rec.status != "in":
            payment = self.billing.payment.get_by_parking(rec.id) if self.billing else None
            result = {"record_id": rec.id, "plate": plate, "status": rec.status,
                      "payment_id": getattr(payment, "id", None),
                      "amount_paid": getattr(payment, "amount_paid", None),
                      "space_no": rec.space_no, "idempotent": True}
            self.bus.publish(ExitAuthorized(
                vehicle_id=vehicle_id, plate=plate, record_id=rec.id,
                action=action, amount_paid=result.get("amount_paid"),
                space_no=rec.space_no, reason=reason))
            return result
        if action == "charge":
            payment = self.billing.pay(rec.id, method="cash", operator=operator)
            self.bus.publish(ExitAuthorized(
                vehicle_id=vehicle_id, plate=plate, record_id=rec.id,
                action=action, amount_paid=payment.amount_paid,
                space_no=rec.space_no, reason=reason))
            return {"record_id": rec.id, "plate": plate, "status": rec.status,
                    "payment_id": payment.id, "amount_paid": payment.amount_paid,
                    "space_no": rec.space_no}
        result = self.manual_exit(plate, operator, reason, action=action,
                                  record_id=rec.id,
                                  request_id=request_id or f"authorize-exit-{rec.id}-{action}")
        self.bus.publish(ExitAuthorized(
            vehicle_id=vehicle_id, plate=plate, record_id=rec.id,
            action=action, amount_paid=result.get("amount_paid"),
            space_no=rec.space_no, reason=reason))
        return result

    def manual_register(self, plate: str, gate: str, operator: str,
                        color: str = "blue") -> dict:
        """人工录入车牌并授权进场（摄像头/识别故障降级，FR-9.2）。"""
        if gate != "in":
            raise DataAnomaly("人工录入仅支持入口方向")
        result = self._authorize_entry(None, plate, color, manual=True,
                                       operator=operator)
        return result

    def _sim_to_datetime(self, sim_sec: float):
        """仿真秒 → 业务时间（epoch + sim_sec；非演示模式退回当前真实时间）。"""
        from datetime import timedelta as _td
        epoch = getattr(self.simulation, "epoch", None)
        if epoch is None or not getattr(self.cfg.server, "demo_mode", False):
            return utcnow()
        return epoch + _td(seconds=float(sim_sec))

    def _temporary_exit_record(self, plate, color, *, vehicle_id=None, hours=None,
                               entry_time=None, entry_sim_sec=None):
        now=self._now()
        if entry_time is not None and entry_time > now:
            raise ValidationError("入场时间不能晚于当前时间")
        hours=self.cfg.access.temp_max_hours if hours is None else hours
        if not 0 <= hours <= 24*365:
            raise ValidationError("temp_max_hours超出允许范围")
        sim_now = self._sim_seconds()
        if entry_sim_sec is None and sim_now is not None and entry_time is None:
            entry_sim_sec = max(0.0, sim_now - float(hours) * 3600.0)
        self.bus.publish(IncidentRaised(kind="data",gate="out",detail={
            "reason":"exit_without_record","plate":plate,"temporary":True}))
        rec=self.parking.create(plate=plate,plate_color=color,space_no=None,
            entry_time=entry_time or now-timedelta(hours=hours),vehicle_id=vehicle_id,
            fee_rule_id=self._snapshot_fee_rule_id(),temporary=True,
            entry_sim_sec=entry_sim_sec)
        if self.oplog:
            self.oplog.add(operator="system",action="temporary_exit",target=plate,
                detail={"record_id":rec.id,"inferred_hours":hours,"entry_time":str(rec.entry_time)})
        return rec

    @synchronized
    def manual_exit(self, plate: str, operator: str, reason: str = "", *,
                    action="charge", record_id=None, entry_time=None,
                    entry_sim_sec=None, amount_override=None, request_id=None) -> dict:
        """人工结算：收费/免费/异常作废均保留账单、原应收与操作审计。"""
        plate,color=self._validated_plate(plate)
        if action not in ("charge","max_duration","free","void","manual_time"):
            raise ValidationError("未知出场处理方式")
        if action in ("free","void") and not reason.strip():
            raise ValidationError("减免/作废必须填写原因")
        if action == "manual_time":
            if entry_time is None and entry_sim_sec is None:
                raise ValidationError("请提供实际入场时间（交付 ISO 时间或仿真秒）")
            if entry_sim_sec is not None:
                try:
                    entry_sim_sec = float(entry_sim_sec)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("仿真入场时间必须为数字秒") from exc
                sim_now = self._sim_seconds()
                if sim_now is None:
                    raise ValidationError("仿真秒输入仅在演示模式（demo_mode）可用；请改传 ISO 时间")
                if entry_sim_sec < 0 or entry_sim_sec > sim_now + 1e-6:
                    raise ValidationError("仿真入场时间必须在 [0, 当前仿真秒] 之间")
                entry_time = self._sim_to_datetime(entry_sim_sec)
            elif entry_time > utcnow():
                # 旧 ISO 输入按真实墙钟校验（保持既有语义）；仿真秒输入按业务时间校验
                raise ValidationError("入场时间不能晚于当前时间")
        from ..repository.models import ResetRequest
        body=json.dumps({"plate":plate,"action":action,"record_id":record_id,"reason":reason,
            "entry_time":str(entry_time),"entry_sim_sec":entry_sim_sec,
            "amount":amount_override},sort_keys=True)
        key="manual-exit:"+request_id if request_id else None
        with self.db.scope():
            previous=self.db.session.get(ResetRequest,key) if key else None
            if previous:
                if previous.request_body!=body:raise StateConflict("idempotency_conflict","重复请求参数不同")
                return json.loads(previous.response)
            rec=self.parking.get(record_id) if record_id is not None else self.parking.find_open_by_plate(plate)
            if record_id is not None and (rec is None or rec.plate!=plate):
                raise ValidationError("记录与车牌不匹配")
            if rec is not None and action == "manual_time":
                if rec.status != "in":
                    raise StateConflict("record_closed", "记录已结束，不能修改入场时间")
                rec.entry_time = entry_time
                # 仿真秒输入 → 同步 entry_sim_sec（计费/时长按仿真时间）；ISO 旧输入保持真实语义
                rec.entry_sim_sec = float(entry_sim_sec) if entry_sim_sec is not None else None
                rec.duration_sim_sec = None
                self.db.session.flush()
            if rec is None:
                if not request_id:
                    raise ValidationError("无记录出场请从人工待办处理或提供request_id")
                if action=="manual_time" and entry_time is None:
                    raise ValidationError("请提供实际入场时间")
                rec=self._temporary_exit_record(plate,color,entry_time=entry_time,
                    entry_sim_sec=entry_sim_sec,
                    hours=0 if action in ("free","void") else None)
            if rec.status!="in":
                payment=self.billing.payment.get_by_parking(rec.id)
                if payment is None:raise StateConflict("record_closed","记录已结束且无可重试付款")
                return {"record_id":rec.id,"plate":plate,"payment_id":payment.id,
                        "amount_paid":payment.amount_paid,"status":rec.status,"space_no":rec.space_no}
            payment=self.billing.pay(rec.id,method="void" if action=="void" else "free" if action=="free" else "cash",
                operator=operator,amount_override=0 if action in ("free","void") else amount_override)
            if action=="void":
                rec.status="abnormal";rec.aborted_reason=reason
            if self.oplog:
                self.oplog.add(operator=operator,action="manual_exit",target=plate,
                    detail={"reason":reason,"action":action,"record_id":rec.id,"payment_id":payment.id,
                            "amount_due":payment.amount_due,"amount_paid":payment.amount_paid})
            response={"record_id":rec.id,"plate":plate,"payment_id":payment.id,"amount_paid":payment.amount_paid,
                    "space_no":rec.space_no,"status":rec.status,"temporary":rec.temporary}
            if key:
                self.db.session.add(ResetRequest(request_id=key,request_body=body,response=json.dumps(response)))
            return response

    # ----------------------------- 断连 / 恢复钩子 -----------------------------
    @synchronized
    def on_disconnect(self) -> None:
        """Only hold an open gate when a vehicle remains in its barrier zone."""
        for b in self.barriers.values():
            if b.zone_occupied:
                b.force_safe()

    @synchronized
    def on_reconnect_reset(self) -> None:
        """恢复：道闸一律回 CLOSED（安全侧，§10.1）。"""
        for b in self.barriers.values():
            b.reset()
        self._zone_sources = {"in":{}, "out":{}}
        self._pending = {"in": 0, "out": 0}
        self._arrivals.clear()

    @synchronized
    def restore_barriers(self) -> None:
        """Recover durable terminal states without replaying old commands."""
        with self.db.scope():
            for gate, barrier in self.barriers.items():
                row = self.db.session.get(BarrierStateRow, gate)
                if row is None:
                    barrier.reset()
                    continue
                interrupted = row.state in ("OPENING", "CLOSING")
                if interrupted:
                    row.state = "FAULT"
                    row.fault = "restart_during_motion"
                    row.pending = 0
                    row.updated_at = utcnow()
                actions = self.db.session.query(BarrierAction).filter_by(
                    gate=gate, status="pending").all()
                for action in actions:
                    action.status = "interrupted"
                barrier.restore(row.state, row.pending, keep_open=row.keep_open)
                self._pending[gate] = row.pending

    def _persist_pending(self, gate: str) -> None:
        with self.db.scope():
            row = self.db.session.get(BarrierStateRow, gate)
            if row is None:
                row = BarrierStateRow(gate=gate, state=self.barriers[gate].state.value,
                                      arm_position=0, pending=0, updated_at=utcnow())
                self.db.session.add(row)
            row.pending = self._pending[gate]
            row.updated_at = utcnow()

    # ----------------------------- 内部工具 -----------------------------
    def _camera_down(self, gate: str) -> bool:
        try:
            return bool(self.incidents.open_by_kind("camera", gate))
        except Exception:  # noqa: BLE001
            log.exception("查询摄像头故障状态失败，保守视为可用")
            return False

    def _expected_plate_match(self, e: VehicleArrived, plate: Optional[str],
                              color: str, conf: float) -> Optional[bool]:
        """仿真 3D 相机：规范化后识别串 == expected_plate 且 conf 达 sim 阈值。

        返回 None 表示非仿真来源（不参与 expected 决策）；False 表示不匹配/不达标。
        """
        if getattr(e, "source", "legacy") != "sim_camera":
            return None
        expected = getattr(e, "expected_plate", None)
        if not expected or not plate:
            return False
        try:
            expected_norm, _ = normalize_valid_plate(expected)
            got_norm, _ = normalize_valid_plate(plate, color)
        except ValueError:
            return False
        threshold = float(getattr(self.cfg.lpr, "sim_conf_threshold", 0.85))
        return got_norm == expected_norm and float(conf) >= threshold

    @staticmethod
    def _sim_manual_reason(e: VehicleArrived, plate: Optional[str]) -> str:
        expected = getattr(e, "expected_plate", None)
        if not expected:
            return "expected_missing"
        if not plate:
            return "low_conf"
        try:
            return "expected_mismatch" if normalize_valid_plate(expected)[0] != plate else "low_conf"
        except ValueError:
            return "expected_mismatch"

    @staticmethod
    def _manual_message(reason: str) -> str:
        if reason == "expected_mismatch":
            return "仿真相机识别与期望车牌不一致，请人工确认"
        if reason == "expected_missing":
            return "仿真相机未提供期望车牌，请人工确认"
        if reason in ("camera_missing", "image_unreadable", "camera_blank"):
            return "图像源不可用，请人工确认"
        if reason == "recognize_error":
            return "识别服务异常，请人工确认"
        if reason == "invalid_plate":
            return "车牌格式无效，请人工确认"
        return "识别置信度不足，转人工确认"

    def _recognize(self, gate: str, e: VehicleArrived):
        """识别车牌，返回结果及图像源/识别失败原因。"""
        if self.recognizer is None or not e.image_path:
            # 无识别器/无图像 → 需人工（禁止静默放行）
            return None, "blue", 0.0, True, [], None, (
                "camera_missing" if not e.image_path else "model_unavailable"), None
        crop_dir = Path(self.cfg.paths.photos_dir) / "crops"
        try:
            crop_dir.mkdir(parents=True, exist_ok=True)
            res = self.recognizer.recognize(e.image_path, gate=gate, crop_dir=crop_dir)
        except TypeError:
            # 兼容只实现 recognize(image_path) 的旧/测试识别器
            res = self.recognizer.recognize(e.image_path)
        except Exception:  # noqa: BLE001 - 推理异常必须降级，不能静默丢失到达事件
            log.exception("车牌识别异常 gate=%s vehicle=%s", gate, e.vehicle_id)
            return None, "blue", 0.0, True, [], None, "recognize_error", None
        log_id = None
        try:
            with self.db.scope():
                row = self.recog_logs.add(
                    gate=gate, image_path=e.image_path, plate=res.plate,
                    plate_color=res.color.value if res.color else None,
                    char_confs=res.char_confs, overall_conf=res.overall_conf,
                    elapsed_ms=res.elapsed_ms, success=res.success)
                log_id = row.id
        except Exception:  # noqa: BLE001 - 日志失败不阻断业务
            log.exception("写识别日志失败")
        color = res.color.value if res.color else "blue"
        grammar_ok = bool(res.plate and validate_grammar(res.plate, color)[0])
        need_manual = bool(res.need_manual or not res.success
                           or not res.plate
                           or not grammar_ok
                           or res.overall_conf < self.cfg.lpr.conf_threshold)
        reason = ("image_unreadable" if res.source_status == "unreadable"
                  else "camera_blank" if res.source_status == "blank"
                  else "image_unreadable" if res.message.startswith("无法读取图像")
                  else "recognize_error" if res.message in ("识别模型推理失败", "实拍识别模型不可用")
                  else "invalid_plate" if res.plate and not grammar_ok
                  else "low_conf" if need_manual else "ok")
        return res.plate, color, res.overall_conf, need_manual, res.candidates, \
            res.plate_crop_path, reason, log_id

    def _create_pending(self, e: VehicleArrived, *, reason: str, message: str,
                        plate: str | None = None, color: str | None = None,
                        candidates: list[str] | None = None,
                        recognition_log_id: int | None = None) -> dict:
        if self.pending_repo is None:
            raise RuntimeError("人工确认仓储未配置")
        with self.db.scope():
            row, _ = self.pending_repo.create_or_refresh(
                gate=e.gate, vehicle_id=e.vehicle_id,
                image_path=e.image_path, candidates=candidates or [],
                source_plate=plate, plate_color=color,
                recognition_log_id=recognition_log_id, reason=reason,
                message=message,
                timeout_seconds=self.cfg.lpr.manual_confirm_timeout,
                sim_now=self._sim_seconds(),
                speed=float(getattr(self.simulation, "speed", 1.0) or 1.0))
            view = self.pending_repo.view(row)
            self.bus.publish(PendingCreated(
                pending_id=row.id, gate=row.gate, vehicle_id=row.vehicle_id,
                reason=row.reason, source_plate=row.source_plate,
                candidates=candidates or [], expires_at=row.expires_at))
        return view

    def list_pending(self, status: str = "pending") -> list[dict]:
        if status not in ("pending", "confirmed", "rejected", "expired", "all"):
            raise ValidationError("无效的待办状态")
        return self.pending_repo.list(session_id=self.pending_repo.current_session_id(),
                                      status=status)

    def _settle_exit_pending(self, pending_id: int, plate: str, *,
                             operator: str, exit_action: Optional[str] = None,
                             reason: str = "", entry_time=None,
                             entry_sim_sec=None) -> dict:
        """出口待办统一正常结算：charge/free/void/manual_time/max_duration。

        已有在场记录 → 实际 ``billing.pay``（标记 out、释放车位、PaymentCompleted 开出口）；
        无记录 → 临时处置（同样实际入账后放行）。随后发 ``ExitAuthorized`` 供 3D 驶离。
        """
        action = exit_action or "charge"
        if action not in ("charge", "max_duration", "free", "void", "manual_time"):
            raise ValidationError(
                "exit_action 应为 charge/free/void/manual_time/max_duration")
        settle_reason = reason
        if action in ("free", "void") and not settle_reason.strip():
            # 人工确认待办时未填原因：补默认审计原因，避免正常放行被卡
            settle_reason = "人工确认出场：" + ("免费放行" if action == "free" else "异常作废")
        rec = self.parking.find_open_by_plate(plate, include_temporary=True)
        result = self.manual_exit(
            plate, operator, settle_reason, action=action,
            record_id=rec.id if rec is not None else None,
            entry_time=entry_time, entry_sim_sec=entry_sim_sec,
            request_id=f"pending-{pending_id}")
        settlement = {
            "action": action,
            "record_id": result.get("record_id"),
            "payment_id": result.get("payment_id"),
            "amount_paid": result.get("amount_paid"),
            "status": result.get("status"),
            "space_no": result.get("space_no"),
            "temporary": result.get("temporary"),
            "reason": settle_reason,
        }
        self.bus.publish(ExitAuthorized(
            vehicle_id=None, plate=plate, record_id=settlement["record_id"],
            action=action, amount_paid=settlement["amount_paid"],
            space_no=settlement["space_no"], reason=settle_reason))
        log.info("出口待办人工确认已结算 pending=%s plate=%s action=%s amount=%s status=%s",
                 pending_id, plate, action, settlement["amount_paid"],
                 settlement["status"])
        return settlement

    @synchronized
    def confirm_pending(self, item_id: int, plate: str, operator: str, *,
                        exit_action=None, reason="", entry_time=None,
                        entry_sim_sec=None) -> dict:
        plate, color = self._validated_plate(plate)
        with self.db.scope():
            row = self.pending_repo.get(item_id)
            if row is None:
                raise NotFound(f"人工待办 #{item_id} 不存在")
            if row.session_id != self.pending_repo.current_session_id():
                raise StateConflict("session_changed", "待办所属会话已结束")
            if row.status == "confirmed":
                if row.confirmed_plate != plate:
                    raise StateConflict("pending_resolved", "待办已用其他车牌确认")
                return self.pending_repo.view(row)
            if row.status != "pending":
                raise StateConflict("pending_resolved", "待办已处理")
            sim_now = self._sim_seconds()
            expired = (sim_now is not None and row.sim_deadline_sec is not None
                       and sim_now >= float(row.sim_deadline_sec))
            if expired or row.expires_at <= utcnow():
                raise StateConflict("pending_expired", "待办已超时")
            settlement = None
            if row.gate == "in":
                self._authorize_entry(row.vehicle_id, plate, color,
                                      entry_photo=row.image_path, manual=True,
                                      operator=operator, confirmed=True)
            elif row.gate == "out":
                # 实现 A：出口待办统一走正常结算（实际 billing.pay → PaymentCompleted →
                # 标记 out/释放车位/开出口），禁止只 quote 不支付；无记录走临时处置。
                settlement = self._settle_exit_pending(
                    row.id, plate, operator=operator, exit_action=exit_action,
                    reason=reason, entry_time=entry_time,
                    entry_sim_sec=entry_sim_sec)
            else:
                raise DataAnomaly("待办闸口无效")
            if row.recognition_log_id:
                self.recog_logs.mark_corrected(row.recognition_log_id, plate)
            self.pending_repo.resolve(row, status="confirmed", operator=operator,
                                      plate=plate)
            if self.oplog:
                self.oplog.add(operator=operator, action="pending_confirm",
                               target=str(row.id), detail={"plate": plate,
                               "source_plate": row.source_plate,
                               "recognition_log_id": row.recognition_log_id})
            self.bus.publish(PendingResolved(
                pending_id=row.id, gate=row.gate, vehicle_id=row.vehicle_id,
                status="confirmed", plate=plate))
            view = self.pending_repo.view(row)
            if settlement is not None:
                view["settlement"] = settlement
            return view

    @synchronized
    def reject_pending(self, item_id: int, operator: str, reason: str = "") -> dict:
        with self.db.scope():
            row = self.pending_repo.get(item_id)
            if row is None:
                raise NotFound(f"人工待办 #{item_id} 不存在")
            if row.session_id != self.pending_repo.current_session_id():
                raise StateConflict("session_changed", "待办所属会话已结束")
            if row.status == "rejected":
                return self.pending_repo.view(row)
            if row.status != "pending":
                raise StateConflict("pending_resolved", "待办已处理")
            self.pending_repo.resolve(row, status="rejected", operator=operator,
                                      resolution=reason)
            if self.oplog:
                self.oplog.add(operator=operator, action="pending_reject",
                               target=str(row.id), detail={"reason": reason})
            self.bus.publish(PendingResolved(
                pending_id=row.id, gate=row.gate, vehicle_id=row.vehicle_id,
                status="rejected", message=reason))
            # 实现 A：拒绝后不永久占用停止线 → 请求 3D 清场/重新抓拍重试（不阻塞后车队列）
            self._arrivals.pop((row.gate, row.vehicle_id or ""), None)
            self.bus.publish(ArrivalRetryRequested(
                vehicle_id=row.vehicle_id, gate=row.gate,
                reason="pending_rejected",
                message="待办已拒绝，请重新抓拍或驶离后重试（不阻塞后续车辆）"))
            return self.pending_repo.view(row)

    @synchronized
    def expire_pending(self) -> int:
        # P1-05：演示暂停期间冻结人工待办倒计时，不自动过期。
        if getattr(self.simulation, "paused", False):
            return 0
        with self.db.scope():
            rows = self.pending_repo.expire_due(sim_now=self._sim_seconds())
            for row in rows:
                self.bus.publish(PendingResolved(
                    pending_id=row.id, gate=row.gate, vehicle_id=row.vehicle_id,
                    status="expired", message="人工确认超时"))
                # 实现 A：超时释放停止线，允许同车/后车重试
                self._arrivals.pop((row.gate, row.vehicle_id or ""), None)
                self.bus.publish(ArrivalRetryRequested(
                    vehicle_id=row.vehicle_id, gate=row.gate,
                    reason="pending_expired",
                    message="人工确认超时，请重新抓拍或驶离后重试（不阻塞后续车辆）"))
            return len(rows)

    @synchronized
    def shift_pending_deadlines(self, seconds: float) -> int:
        """演示恢复时顺延 pending 到期时间（由 SimulationClock 调用）。"""
        if self.pending_repo is None:
            return 0
        with self.db.scope():
            return self.pending_repo.shift_pending_deadlines(
                seconds, self.pending_repo.current_session_id())

    @staticmethod
    def _validated_plate(plate: str, color: str | None = None) -> tuple[str, str]:
        try:
            return normalize_valid_plate(plate, color)
        except ValueError as exc:
            raise ValidationError(f"车牌格式无效：{exc}", plate=plate) from exc

    def _snapshot_fee_rule_id(self) -> Optional[int]:
        if self.fee_provider is None:
            return None
        try:
            rule = self.fee_provider.current_rule()
            return getattr(rule, "id", None)
        except Exception:  # noqa: BLE001
            log.exception("获取进场费率快照失败")
            return None

    # ----------------------------- 识别输入图片（想法一） -----------------------------
    def _public_image_url(self, raw: Optional[str]) -> Optional[str]:
        """把识别输入/车牌裁剪图的本地路径映射为现有静态挂载 URL。

        - ``datasets/**``（plates_dataset.parent）→ ``/datasets/**``
        - ``data/photos/**``（photos_dir）→ ``/media/**``
        - 已是 http(s) 或 /datasets /media 前缀则原样返回
        不能映射到公开目录时返回 None，事件仍保留 image_path 供审计。
        """
        if not raw:
            return None
        value = str(raw).strip()
        if not value:
            return None
        if value.lower().startswith(("http://", "https://")):
            return value
        if value.startswith(("/datasets/", "/media/")):
            return value
        try:
            path = Path(value)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            path = path.resolve()
        except Exception:  # noqa: BLE001 - 非法路径不应阻断识别事件
            return None
        mounts = [
            (Path(self.cfg.paths.plates_dataset).resolve().parent, "/datasets/"),
            (Path(self.cfg.paths.photos_dir).resolve(), "/media/"),
        ]
        for root, prefix in mounts:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            return prefix + quote(rel.as_posix(), safe="/")
        return None

    @staticmethod
    def _image_meta(image_url: Optional[str]) -> tuple[str, Optional[bool]]:
        """返回（来源标签, 是否合成）；None 表示来源未声明。"""
        if not image_url:
            return "", None
        if "/datasets/demo-lpr/" in image_url:
            return "demo-lpr 合成演示样例", True
        if image_url.startswith("/datasets/"):
            return "datasets 演示图库", None
        if "/media/camera/" in image_url:
            return "仿真端 3D 相机截图", True
        if "/media/uploads/" in image_url:
            return "接口/管理端上传图片", None
        if "/media/crops/" in image_url:
            return "服务端裁剪图", None
        if image_url.startswith("/media/"):
            return "服务端照片", None
        return "外部图片", None

    def _publish_recognized(self, gate: str, vehicle_id: Optional[str], *,
                            plate: Optional[str] = None, color: Optional[str] = None,
                            conf: float = 0.0, need_manual: bool = False,
                            candidates: Optional[list] = None,
                            message: str = "", reason: str = "",
                            image_path: Optional[str] = None,
                            plate_crop_path: Optional[str] = None,
                            source: str = "legacy",
                            expected_plate: Optional[str] = None,
                            plate_match: Optional[bool] = None) -> None:
        image_url = self._public_image_url(image_path)
        crop_url = self._public_image_url(plate_crop_path)
        image_source, image_synthetic = self._image_meta(image_url)
        self.bus.publish(PlateRecognized(
            vehicle_id=vehicle_id, gate=gate, plate=plate, color=color,
            overall_conf=conf, need_manual=need_manual,
            candidates=candidates or [], reason=reason, message=message,
            image_path=image_path, image_url=image_url,
            plate_crop_url=crop_url, image_source=image_source,
            image_synthetic=image_synthetic, source=source,
            expected_plate=expected_plate, plate_match=plate_match))
        if message:
            log.info("识别推送[%s] %s", gate, message)
