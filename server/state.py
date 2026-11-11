"""Durable demo session state, snapshots, and reset transactions."""
from __future__ import annotations

import json
import math
import threading
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from .core.errors import StateConflict, ValidationError
from .core.timeutil import iso_local, utcnow
from .repository.models import (BarrierAction, BarrierStateRow, DemoSession, EventLog,
                                PendingManual, IncidentLog, OpLog, ParkingRecord,
                                ParkingSpace, PaymentRecord, ResetRequest, VehicleRuntime)


class StateService:
    def __init__(self, container):
        self.c = container
        self._lock = threading.RLock()

    @property
    def db(self):
        return self.c.db

    def current_session(self):
        return self.db.session.execute(select(DemoSession).where(
            DemoSession.status == "active").limit(1)).scalar_one_or_none()

    def ensure_session(self, operator: str = "system", seed: int | None = None):
        with self._lock, self.db.scope():
            session = self.current_session()
            if session is None:
                session = DemoSession(id=uuid4().hex, started_at=utcnow(),
                                      status="active", seed=seed, operator=operator)
                self.db.session.add(session)
                self.db.session.flush()
            return session

    def append_event(self, name: str, data: dict, *, source: str = "system",
                     event_id: str | None = None, session_id: str | None = None):
        session = self.current_session()
        row = EventLog(event_id=event_id or uuid4().hex,
                       session_id=session_id if session_id is not None else (
                           session.id if session else None), name=name,
                       payload=json.dumps(data, ensure_ascii=False, default=str),
                       source=source, created_at=utcnow())
        self.db.session.add(row)
        self.db.session.flush()
        return row

    @staticmethod
    def event_view(row: EventLog) -> dict:
        return {"type": "event", "seq": row.seq, "event_id": row.event_id,
                "session_id": row.session_id, "name": row.name,
                "data": json.loads(row.payload), "ts": iso_local(row.created_at)}

    def events(self, since: int = 0, limit: int = 500, session: str | None = None,
               types: list[str] | None = None) -> dict:
        with self._lock:
            query = select(EventLog).where(EventLog.seq > since)
            if session:
                query = query.where(EventLog.session_id == session)
            if types:
                query = query.where(EventLog.name.in_(types))
            rows = list(self.db.session.execute(query.order_by(EventLog.seq).limit(
                limit + 1)).scalars())
            more = len(rows) > limit
            items = [self.event_view(row) for row in rows[:limit]]
            head = self.db.session.execute(select(func.max(EventLog.seq))).scalar() or 0
            return {"items": items, "next_since": items[-1]["seq"] if items else since,
                    "has_more": more, "head_seq": head}

    def recent_events(self, limit: int = 200) -> dict:
        with self._lock:
            rows = list(self.db.session.execute(select(EventLog).order_by(
                EventLog.seq.desc()).limit(limit)).scalars())
            items = [self.event_view(row) for row in reversed(rows)]
            head = rows[0].seq if rows else 0
            return {"items": items, "next_since": head,
                    "has_more": False, "head_seq": head}

    def snapshot(self) -> dict:
        with self._lock:
            s = self.db.session
            active = self.current_session()
            spaces = list(s.execute(select(ParkingSpace).where(
                ParkingSpace.disabled == False).order_by(ParkingSpace.space_no)  # noqa: E712
            ).scalars())
            records = list(s.execute(select(ParkingRecord).where(
                ParkingRecord.status == "in")).scalars())
            barriers = {r.gate: {"state": r.state, "arm_position": r.arm_position,
                                  "keep_open": r.keep_open,
                                  "pending": r.pending, "last_cmd_id": r.last_cmd_id,
                                  "fault": r.fault,
                                  # 实现 A：物理可通行（不含业务授权判断）
                                  "passable": r.state in ("OPENING", "KEEP_OPEN")}
                        for r in s.execute(select(BarrierStateRow)).scalars()}
            for gate in ("in", "out"):
                barriers.setdefault(gate, {"state": "CLOSED", "arm_position": 0,
                                           "pending": 0, "last_cmd_id": None, "fault": None,
                                           "passable": False})
            free = {zone: sum(sp.zone == zone and sp.status == "free" for sp in spaces)
                    for zone in sorted({sp.zone for sp in spaces})}
            incidents = list(s.execute(select(IncidentLog).where(
                IncidentLog.status == "open")).scalars())
            head = s.execute(select(func.max(EventLog.seq))).scalar() or 0
            runtime = list(s.execute(select(VehicleRuntime).where(
                VehicleRuntime.session_id == active.id)).scalars()) if active else []
            scheduler_runtime = [json.loads(row.payload) for row in runtime if row.vehicle_id == "__scheduler__"]
            display_events = []
            if active:
                display_names = ("plate_recognized", "fee_quote", "payment_completed", "barrier_state")
                for gate in ("in", "out"):
                    rows = list(s.execute(select(EventLog).where(
                        EventLog.session_id == active.id, EventLog.name.in_(display_names),
                        func.coalesce(func.json_extract(EventLog.payload, "$.gate"), "out") == gate
                    ).order_by(EventLog.seq.desc()).limit(8)).scalars())
                    display_events.extend(self.event_view(row) for row in rows)
            return {
                "display_events": sorted(display_events, key=lambda e: e["seq"]),
                "scheduler_runtime": scheduler_runtime,
                "simulation": self.c.simulation.view(),
                "seq": head, "server_time": iso_local(utcnow()),
                "session": None if active is None else {
                    "id": active.id, "status": active.status,
                    "started_at": iso_local(active.started_at)},
                "layout": {"version": self.c.layout.current_version(),
                           "free_total": sum(free.values()), "free_by_zone": free,
                           "spaces": [self.c.space_repo.to_spec(sp) for sp in spaces]},
                "on_lot": [self._on_lot_view(rec) for rec in records],
                "paid_exits": [{"record_id":p.parking_record_id,"plate":p.plate,"vehicle_id":r.vehicle_id}
                    for p,r in s.execute(select(PaymentRecord,ParkingRecord).join(ParkingRecord,
                        ParkingRecord.id==PaymentRecord.parking_record_id).where(
                        ParkingRecord.session_id==active.id if active else ParkingRecord.id<0,
                        ParkingRecord.vehicle_id.in_([row.vehicle_id for row in runtime if row.vehicle_id!="__scheduler__"])).order_by(
                        PaymentRecord.id.desc())).all()],
                "vehicle_runtime": [json.loads(row.payload) for row in runtime if row.vehicle_id != "__scheduler__"],
                "barriers": barriers,
                "cameras": {gate: {"online": self.c.watchdog.status(gate)}
                            for gate in ("in", "out")},
                "pending_manual": self.c.pending_repo.list(
                    session_id=active.id if active else None),
                "open_incidents": [{"id": row.id, "kind": row.kind,
                                    "gate": row.gate, "detail": row.detail}
                                   for row in incidents],
                "config": {"animation": {
                    "open_sec": self.c.cfg.access.barrier_open_sec,
                    "close_sec": self.c.cfg.access.barrier_close_sec}},
            }

    def _on_lot_view(self, rec) -> dict:
        sim = self.c.simulation
        demo = bool(getattr(self.c.cfg.server, "demo_mode", False)) and sim is not None
        duration_sim = None
        if demo and rec.entry_sim_sec is not None:
            duration_sim = round(float(sim.sim_time) - float(rec.entry_sim_sec), 3)
        return {"record_id": rec.id, "vehicle_id": rec.vehicle_id,
                "plate": rec.plate, "color": rec.plate_color,
                "space_no": rec.space_no, "session_id": rec.session_id,
                "entry_time": iso_local(rec.entry_time), "temporary": rec.temporary,
                "entry_sim_sec": rec.entry_sim_sec,
                "duration_sim_sec": duration_sim}

    def reset(self, *, request_id: str, mode: str, operator: str,
              reason: str = "", force: bool = False) -> dict:
        if not request_id:
            raise ValidationError("request_id 不能为空")
        if mode not in ("soft", "hard"):
            raise ValidationError("mode 应为 soft 或 hard")
        body = json.dumps({"mode": mode, "reason": reason, "force": bool(force)},
                          sort_keys=True)
        with self._lock:
            existing = self.db.session.get(ResetRequest, request_id)
            if existing is not None:
                if existing.request_body != body:
                    raise StateConflict("idempotency_conflict",
                                        "同一 request_id 对应不同复位请求")
                return json.loads(existing.response)
            if not force:
                if any(b.zone_occupied for b in self.c.access.barriers.values()):
                    raise StateConflict("reset_unsafe", "闸区有车辆，无法安全复位")
                if any(self.c.access._pending.values()) or any(
                        b._ack_pending for b in self.c.access.barriers.values()):
                    raise StateConflict("reset_unsafe", "道闸仍有未完成动作，无法安全复位")
            old = self.current_session()
            if old is None:
                old = self.ensure_session()
            with self.db.scope():
                now = utcnow()
                biz_now = self.c.simulation.business_now()
                sim_now = float(self.c.simulation.sim_time)
                demo = bool(getattr(self.c.cfg.server, "demo_mode", False))
                records = list(self.db.session.execute(select(ParkingRecord).where(
                    ParkingRecord.session_id == old.id,
                    ParkingRecord.status == "in")).scalars())
                released = 0
                for rec in records:
                    rec.status = "aborted"
                    rec.exit_time = biz_now
                    rec.aborted_reason = "demo_reset"
                    if demo:
                        rec.exit_sim_sec = sim_now
                        if rec.entry_sim_sec is not None:
                            rec.duration_sim_sec = round(sim_now - float(rec.entry_sim_sec), 3)
                    if rec.space_no:
                        still_open = self.db.session.execute(select(func.count()).select_from(
                            ParkingRecord).where(ParkingRecord.space_no == rec.space_no,
                                                 ParkingRecord.status == "in")).scalar()
                        if not still_open:
                            self.c.space_repo.release(rec.space_no)
                            released += 1
                self.db.session.execute(delete(VehicleRuntime).where(
                    VehicleRuntime.session_id == old.id))
                resolved_incidents = list(self.db.session.execute(select(IncidentLog).where(
                    IncidentLog.session_id == old.id,
                    IncidentLog.status == "open")).scalars())
                for incident in resolved_incidents:
                    incident.status = "resolved"
                    incident.resolved_by = operator
                    incident.resolved_at = now
                    incident.resolution = "demo_reset"
                pending_rows = list(self.db.session.execute(select(PendingManual).where(
                    PendingManual.session_id == old.id,
                    PendingManual.status == "pending")).scalars())
                for row in pending_rows:
                    self.c.pending_repo.resolve(
                        row, status="rejected", operator=operator,
                        resolution="demo_reset")
                purged_records = 0
                if mode == "hard":
                    paid_ids = select(PaymentRecord.parking_record_id)
                    candidates = list(self.db.session.execute(select(ParkingRecord).where(
                        ParkingRecord.session_id == old.id,
                        ParkingRecord.id.not_in(paid_ids))).scalars())
                    for rec in candidates:
                        self.db.session.delete(rec)
                    purged_records = len(candidates)
                old.status = "closed"
                old.closed_at = now
                old.reset_mode = mode
                new = DemoSession(id=uuid4().hex, started_at=now,
                                  status="active", operator=operator)
                self.db.session.add(new)
                for gate in ("in", "out"):
                    row = self.db.session.get(BarrierStateRow, gate)
                    if row is None:
                        row = BarrierStateRow(gate=gate, state="CLOSED", arm_position=0,
                                              pending=0, updated_at=now)
                        self.db.session.add(row)
                    else:
                        row.state = "CLOSED"
                        row.arm_position = 0
                        row.keep_open = False
                        row.pending = 0
                        row.last_cmd_id = None
                        row.fault = None
                        row.updated_at = now
                interrupted_actions = 0
                if force:
                    # 强制重置：清理未完成道闸动作（审计状态 interrupted），避免恢复后卡在 pending
                    interrupted_actions = int(self.db.session.execute(
                        update(BarrierAction).where(
                            BarrierAction.status == "pending").values(
                            status="interrupted")).rowcount or 0)
                report = {"closed_records": len(records), "released_spaces": released,
                          "resolved_incidents": len(resolved_incidents),
                          "purged_records": purged_records, "force": bool(force),
                          # D/管理端联调字段（语义显式，便于前端直接展示）
                          "aborted_records": len(records),
                          "rejected_pending": len(pending_rows),
                          "interrupted_barrier_actions": interrupted_actions}
                self.db.session.add(OpLog(ts=now, operator=operator, action="demo_reset",
                                          target=old.id, session_id=old.id,
                                          detail=json.dumps({"mode": mode, "reason": reason,
                                                             "closed_pending": len(pending_rows),
                                                             **report}, ensure_ascii=False)))
                event = self.append_event("DEMO_RESET", {"type": "demo_reset",
                    "closed_session": old.id, "force": bool(force),
                    "new_session": new.id, "reset_report": report},
                    source="admin", session_id=new.id)
                self.c.simulation.reset()
                response = {"closed_session": old.id,
                            "new_session": new.id, "reset_report": report,
                            "seq": event.seq, "snapshot": self.snapshot()}
                self.db.session.add(ResetRequest(request_id=request_id,
                    request_body=body, response=json.dumps(response, ensure_ascii=False)))
            self.c.access.on_reconnect_reset()
            for incident in resolved_incidents:
                if incident.kind == "camera" and incident.gate:
                    self.c.watchdog._streak[incident.gate] = 0
            if self.c.broadcaster is not None:
                self.c.broadcaster.publish_event(self.event_view(event))
            return response

    def update_runtime(self, *, session_id: str, owner_id: str,
                       vehicles: list[dict], scheduler: dict | None = None) -> dict:
        with self._lock:
            active = self.current_session()
            if active is None or active.id != session_id:
                raise StateConflict("session_changed", "演示会话已切换，请重新获取快照",
                                    current_session=active.id if active else None)
            if not owner_id or len(owner_id) > 64 or len(vehicles) > 100:
                raise ValidationError("运行态上报超出允许范围")
            ids = [str(v.get("vehicle_id", "")) for v in vehicles]
            if not all(ids) or "__scheduler__" in ids or len(set(ids)) != len(ids):
                raise ValidationError("vehicle_id 缺失或重复")
            if len(json.dumps(vehicles, ensure_ascii=False)) > 1_000_000:
                raise ValidationError("运行态上报过大")
            for vehicle in vehicles:
                pose = vehicle.get("pose") or {}
                if any(not isinstance(pose.get(axis), (int, float)) or
                       not math.isfinite(pose[axis]) for axis in ("x", "z", "theta")):
                    raise ValidationError("车辆位姿必须为有限数字")
            if scheduler is not None:
                if len(json.dumps(scheduler, ensure_ascii=False)) > 100_000 or len(scheduler.get("pending", [])) > 100:
                    raise ValidationError("调度队列超出允许范围")
                for key in ("seed", "seq", "rng_state"):
                    if not isinstance(scheduler.get(key), int) or not 0 <= scheduler[key] <= 2**53:
                        raise ValidationError("调度随机状态必须为非负整数")
            now = utcnow()
            with self.db.scope():
                prior = self.db.session.execute(select(VehicleRuntime).where(
                    VehicleRuntime.session_id == session_id, VehicleRuntime.owner_id == owner_id,
                    VehicleRuntime.vehicle_id == "__scheduler__")).scalar_one_or_none()
                dispatcher = bool(prior and json.loads(prior.payload).get("dispatcher"))
                self.db.session.execute(delete(VehicleRuntime).where(
                    VehicleRuntime.session_id == session_id,
                    VehicleRuntime.owner_id == owner_id))
                for vehicle in vehicles:
                    self.db.session.add(VehicleRuntime(
                        session_id=session_id, owner_id=owner_id,
                        vehicle_id=vehicle["vehicle_id"],
                        payload=json.dumps({**vehicle, "owner_id": owner_id},
                                           ensure_ascii=False), updated_at=now))
                if scheduler is not None or dispatcher:
                    payload = {**(scheduler or {}), "owner_id":owner_id,"dispatcher":dispatcher}
                    self.db.session.add(VehicleRuntime(session_id=session_id,owner_id=owner_id,
                        vehicle_id="__scheduler__",payload=json.dumps(payload,ensure_ascii=False),updated_at=now))
            return {"ok": True, "count": len(vehicles)}

    def claim_dispatcher(self, session_id: str, owner_id: str) -> dict:
        """One driving browser per session; other browsers remain synchronized observers.

        Ownership persists across reloads and cannot silently expire under a vehicle.
        A safe demo reset starts a new session and releases it.
        """
        with self._lock, self.db.scope():
            active=self.current_session()
            if not active or active.id != session_id:
                raise StateConflict("session_changed", "演示会话已切换")
            if not owner_id or len(owner_id)>64:
                raise ValidationError("owner_id 无效")
            rows=self.db.session.execute(select(VehicleRuntime).where(
                VehicleRuntime.session_id==session_id,VehicleRuntime.vehicle_id=="__scheduler__")).scalars().all()
            for row in rows:
                if row.owner_id != owner_id and json.loads(row.payload).get("dispatcher"):
                    raise StateConflict("dispatcher_busy", "当前会话已有驾驶端，本页为观察端；安全清场后可重新派车")
            row=next((r for r in rows if r.owner_id==owner_id),None)
            if row is None:
                row=VehicleRuntime(session_id=session_id,owner_id=owner_id,vehicle_id="__scheduler__",payload="{}",updated_at=utcnow())
                self.db.session.add(row)
            payload=json.loads(row.payload)
            row.payload=json.dumps({**payload,"owner_id":owner_id,"dispatcher":True})
            return {"ok":True,"owner_id":owner_id}

