"""布局 REST（gateway/api_layout.py）。

对应设计文档 §3.9 / §11.1：
- ``GET /api/layout``：前端（3D/管理端）渲染的**唯一依据**（车位坐标/类型/闸口/场景）；
- ``POST /api/admin/layout``：声明式调整布局（按区填数量/类型），返回 diff；
- ``GET/POST /api/admin/layout/export|import``：布局 JSON 导出/导入（备份）。

route-v2（审查报告 §8.9.4）补充：
- ``GET /api/layout`` 顶层新增 ``road_graph_version`` / ``manifest_hash`` /
  ``manifest_version`` / ``manifest_path`` / ``layout_source`` 与 ``road_graph``
  摘要；``scene`` 内新增 ``aisle_w/aisle_n/aisle_s/median_half/plaza/stop_paint/
  gate_out_y`` 等 manifest 派生键（**旧字段全部保留**，前端可继续按旧键读取）；
- ``GET /assets3d/*``（main.py 挂载）：静态只读提供 manifest 与 3D 资产，
  前端可直接拉 ``/assets3d/layout/lot_v2.json`` 作为路网单一事实源。

布局变更后**先提交事务再广播** LayoutChanged，避免前端重拉到未提交数据。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..core.events import LayoutChanged
from ..core.errors import ValidationError
from .auth import require_admin
from .context import Container, get_container

log = logging.getLogger("gateway.layout")
router = APIRouter(tags=["layout"])


class ZoneDeclIn(BaseModel):
    zone: str = Field(..., description="区名 A/B/C/D")
    count: int = Field(..., ge=1, description="车位数")
    kind: str = Field("normal", description="normal | ne_prefer")
    anchor: Optional[list[float]] = None
    columns: Optional[int] = None
    rows: Optional[int] = None
    spacing: Optional[float] = None
    heading: Optional[float] = None

    def to_decl_dict(self) -> dict:
        d: dict = {"zone": self.zone, "count": self.count, "kind": self.kind}
        if self.anchor is not None:
            d["anchor"] = [float(v) for v in self.anchor]
        for k in ("columns", "rows", "spacing", "heading"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


class LayoutApplyIn(BaseModel):
    zones: list[ZoneDeclIn]
    operator: Optional[str] = None


def _layout_payload(c: Container) -> dict:
    """``GET /api/layout`` 的响应体：布局 + 场景/闸口 + manifest 元信息。

    额外顶层字段（route-v2）：
    ``road_graph_version`` / ``manifest_version`` / ``manifest_hash`` /
    ``manifest_path`` / ``layout_source`` / ``road_graph``（摘要，不含 nodes/edges 全量）。
    """
    data = c.layout.current_layout().model_dump(mode="json")
    data.update(c.layout.manifest_meta())
    summary = c.layout.manifest_summary()
    if summary is not None:
        data["road_graph"] = summary
    return data


@router.get("/api/layout")
def get_layout(c: Container = Depends(get_container)) -> dict:
    return _layout_payload(c)


@router.post("/api/admin/layout")
def apply_layout(body: LayoutApplyIn, admin: dict = Depends(require_admin),
                 c: Container = Depends(get_container)) -> dict:
    if not body.zones:
        raise ValidationError("布局声明不能为空")
    decls = [z.to_decl_dict() for z in body.zones]
    operator = body.operator or admin.get("sub", "admin")
    with c.db.scope():
        diff = c.layout.apply_layout(decls, operator=operator, publish=False)
    # 提交后再广播，前端重拉 /api/layout 得到最新数据
    c.bus.publish(LayoutChanged(version=diff.version, diff=diff.summary))
    log.info("布局已调整 by %s: %s（layout_source=%s）", operator, diff.summary,
             c.layout.layout_source)
    return diff.model_dump(mode="json")


@router.get("/api/admin/layout/export")
def export_layout(admin: dict = Depends(require_admin),
                  c: Container = Depends(get_container)) -> dict:
    """导出为可再导入的声明（按区聚合数量/类型）+ route-v2 manifest 元信息。"""
    layout = c.layout.current_layout()
    zones: dict[str, dict] = {}
    for sp in layout.spaces:
        z = zones.setdefault(sp.zone, {"zone": sp.zone, "count": 0,
                                       "kind": sp.kind.value})
        z["count"] += 1
    payload = {"version": layout.version,
               "zones": sorted(zones.values(), key=lambda x: x["zone"])}
    payload.update(c.layout.manifest_meta())
    return payload


class LayoutImportIn(BaseModel):
    zones: list[ZoneDeclIn]
    operator: Optional[str] = None


@router.post("/api/admin/layout/import")
def import_layout(body: LayoutImportIn, admin: dict = Depends(require_admin),
                  c: Container = Depends(get_container)) -> dict:
    return apply_layout(LayoutApplyIn(zones=body.zones,
                                      operator=body.operator or admin.get("sub")),
                        admin, c)


@router.get("/api/spaces")
def list_spaces(zone: Optional[str] = Query(None),
                c: Container = Depends(get_container)) -> dict:
    """车位明细（管理端车位视图）。"""
    spaces = c.layout.current_layout().spaces
    if zone:
        spaces = [s for s in spaces if s.zone == zone.upper()]
    return {"total": len(spaces),
            "items": [s.model_dump(mode="json") for s in spaces]}
