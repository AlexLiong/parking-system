"""场景剧本管理 API（admin）。

- ``GET /api/admin/scenario``：返回场景对象（含 source=data|builtin）
- ``PUT /api/admin/scenario``：校验并保存到 ``data/scenario.json``，返回保存对象
- ``POST /api/admin/scenario/reset``：删除 data 文件，恢复内置默认
- 仿真端 ``GET /sim/scenario`` 由 api_sim 调用 ``scenario.load_scenario``，优先 data。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from .auth import require_admin
from .context import Container, get_container
from .. import scenario as scenario_service

log = logging.getLogger("gateway.scenario")
router = APIRouter(tags=["scenario"])


@router.get("/api/admin/scenario")
def get_admin_scenario(admin: dict = Depends(require_admin),
                       c: Container = Depends(get_container)) -> dict:
    scenario, _source = scenario_service.load_scenario(c.cfg)
    return scenario


@router.put("/api/admin/scenario")
def put_admin_scenario(body: dict, admin: dict = Depends(require_admin),
                       c: Container = Depends(get_container)) -> dict:
    try:
        saved = scenario_service.save_scenario(c.cfg, body)
    except ValueError as exc:
        raise HTTPException(422, detail={"reason": str(exc)}) from exc
    except OSError as exc:
        log.exception("保存场景剧本失败")
        raise HTTPException(500, detail={"reason": "保存场景剧本失败"}) from exc
    return {"ok": True, "scenario": saved, "source": "data"}


@router.post("/api/admin/scenario/reset")
def reset_admin_scenario(admin: dict = Depends(require_admin),
                         c: Container = Depends(get_container)) -> dict:
    restored = scenario_service.reset_scenario(c.cfg)
    return {"ok": True, "scenario": restored, "source": "builtin"}
