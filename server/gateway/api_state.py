"""Authoritative demo session, snapshot, and event history endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
import json
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..repository.models import BarrierAction
from ..core.errors import StateConflict
from ..core.timeutil import iso_local

from .auth import require_admin
from .context import Container, get_container

router = APIRouter(tags=["state"])


class ResetIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    mode: str = "soft"
    new_session: bool = True
    reason: str = ""
    force: bool = False   # 实现 B：force=true 跳过"闸区有车/道闸动作未完成"安全检查


class SessionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    seed: int | None = None


class RuntimeIn(BaseModel):
    session_id: str
    owner_id: str
    vehicles: list[dict]
    scheduler: dict | None = None


@router.get("/api/state/snapshot")
def snapshot(session: str | None = None, since: int | None = Query(None, ge=0),
             admin: dict = Depends(require_admin),
             c: Container = Depends(get_container)) -> dict:
    result = c.state.snapshot()
    current = result["session"]["id"] if result["session"] else None
    if session is not None and session != current:
        raise StateConflict("session_changed", "演示会话已切换，请重新获取当前会话",
                            current_session=current)
    return result


@router.get("/api/events")
def events(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=500),
           session: str | None = None, types: str | None = None,
           last: int | None = Query(None, ge=1, le=500),
           admin: dict = Depends(require_admin),
           c: Container = Depends(get_container)) -> dict:
    if last is not None:
        return c.state.recent_events(last)
    return c.state.events(since, limit, session,
                          types.split(",") if types else None)


@router.get("/api/demo/session/current")
def current_session(admin: dict = Depends(require_admin),
                    c: Container = Depends(get_container)) -> dict:
    snap = c.state.snapshot()
    return {"session": snap["session"], "snapshot": snap}


@router.post("/api/demo/session")
def start_session(body: SessionIn, admin: dict = Depends(require_admin),
                  c: Container = Depends(get_container)) -> dict:
    c.state.ensure_session(operator=admin["sub"], seed=body.seed)
    snap = c.state.snapshot()
    return {"session": snap["session"], "snapshot": snap}


@router.post("/api/demo/reset")
def reset(body: ResetIn, admin: dict = Depends(require_admin),
          c: Container = Depends(get_container)) -> dict:
    if not body.new_session:
        from ..core.errors import ValidationError
        raise ValidationError("reset 必须创建新会话")
    return c.state.reset(request_id=body.request_id, mode=body.mode,
                         operator=admin["sub"], reason=body.reason,
                         force=body.force)


@router.post("/sim/state")
def report_runtime(body: RuntimeIn, admin: dict = Depends(require_admin),
                   c: Container = Depends(get_container)) -> dict:
    return c.state.update_runtime(session_id=body.session_id,
                                  owner_id=body.owner_id,
                                  vehicles=body.vehicles, scheduler=body.scheduler)


class DispatcherIn(BaseModel):
    session_id: str
    owner_id: str = Field(min_length=1,max_length=64)


@router.post("/sim/dispatcher")
def claim_dispatcher(body: DispatcherIn, admin: dict = Depends(require_admin),
                     c: Container = Depends(get_container)):
    return c.state.claim_dispatcher(body.session_id,body.owner_id)


@router.get("/api/barrier/actions")
def barrier_actions(gate: str | None = None, since: int | None = Query(None, ge=0),
                    limit: int = Query(100, ge=1, le=500),
                    admin: dict = Depends(require_admin),
                    c: Container = Depends(get_container)) -> dict:
    query = select(BarrierAction)
    if gate:
        query = query.where(BarrierAction.gate == gate)
    if since is not None:
        query = query.where(BarrierAction.id > since)
    rows = c.db.session.execute(query.order_by(BarrierAction.id.desc()).limit(
        limit)).scalars()
    return {"items": [{"id": row.id, "cmd_id": row.cmd_id, "gate": row.gate,
                        "cmd": row.cmd, "reason": row.reason,
                        "status": row.status,
                        "operator": row.operator,
                        "created_at": iso_local(row.created_at),
                        "acked_at": iso_local(row.acked_at)}
                       for row in rows]}


@router.get("/api/barrier/actions/export")
def export_actions(gate: str | None = None, admin: dict = Depends(require_admin),
                    c: Container = Depends(get_container)):
    result = barrier_actions(gate=gate, since=None, limit=500, admin=admin, c=c)
    return Response(json.dumps(result, ensure_ascii=False, indent=2),
                    media_type="application/json", headers={
                        "Content-Disposition": 'attachment; filename="barrier-actions.json"'})


class SimulationIn(BaseModel):
    paused: bool | None = None
    speed: float | None = Field(None, ge=.25, le=10)


@router.get('/api/simulation')
def simulation_state(admin: dict = Depends(require_admin), c: Container = Depends(get_container)):
    return c.simulation.view()


@router.post('/api/simulation/control')
def simulation_control(body: SimulationIn, admin: dict = Depends(require_admin), c: Container = Depends(get_container)):
    if not c.cfg.server.demo_mode:
        from fastapi import HTTPException
        raise HTTPException(403,'仅显式演示模式支持暂停/调速')
    return c.simulation.control(paused=body.paused,speed=body.speed)
