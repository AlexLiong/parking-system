"""车位布局服务（layout/service.py）。

对外门面：把声明式布局落库（DB 为唯一事实源，§3.9），并向外广播 LayoutChanged
事件驱动前端热更新。持有 SpaceRepo（读写车位）与 EventBus（发事件），
不自行 commit —— 事务边界由调用方（gateway / seed）用 ``db.scope()`` 控制，
遵循"先落库后动作"（RR-4.1）。

route-v2（审查报告 §8.5/§8.9.4）后，``/api/layout`` 的 ``scene/gates`` 几何以
``assets_3d/layout/lot_v2.json`` **manifest 为准**（``layout/manifest.py``）：
- 出口闸南移 y=-34.0，出口停止线漆 -33.7，入口停止线漆 -25.8；
- 支路宽 6.5 m（北 y=19.5 / 南 y=-13.5），中央隔离带半宽 1.75 m，含分岔区；
- 场景值同时保留旧键（lot_x/top_y/bot_y/gate_y/stop_line_*/outer_road_y 等），
  前端（3D 端/管理端）按旧键或新键读取均可；
- config.scene 与 manifest 不一致时在启动期告警（manifest 为准）。

安全校验（运行时，需 DB 状态，故放在 service 而非 generator）：
- 缩减布局时不得移除**被占用**的车位（FR-2.6）→ LayoutConflict；
- 变更后剩余容量不得小于当前在场车辆数（此处以被占车位数近似）。
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

from sqlalchemy import func, select

from ..core.config import AppConfig, get_config
from ..core.domain import LotLayout, SpaceSpec, SpaceStatus, ZoneDecl
from ..core.errors import LayoutConflict, ParkingError
from ..core.events import EventBus, LayoutChanged
from ..core.timeutil import utcnow
from ..repository.models import LayoutHistory
from ..repository.space_repo import SpaceRepo
from .generator import LayoutGenerator, decls_from_dicts
from .manifest import (
    Manifest,
    get_manifest,
    legacy_gates_values,
    legacy_scene_values,
    manifest_gates_values,
    manifest_scene_values,
    path_text,
    scene_config_issues,
)

log = logging.getLogger("layout")

# ---------------------------------------------------------------------------
# 固定只读场地：运行时只允许 manifest 网格内数量收缩/恢复与类型变更；
# C25/自定义坐标仍返回结构化 409（保留 manifest 固定网格校验，不再提供预演/能力字段）。
# ---------------------------------------------------------------------------


class LayoutService:
    def __init__(self, space_repo: SpaceRepo, event_bus: EventBus,
                 cfg: Optional[AppConfig] = None,
                 generator: Optional[LayoutGenerator] = None,
                 manifest: Optional[Manifest] = None):
        self.repo = space_repo
        self.bus = event_bus
        self.cfg = cfg or get_config()
        # manifest：显式传入 > 配置/仓库默认位置；缺失则回退旧常量（generator 内告警）
        self.manifest: Optional[Manifest] = (
            manifest if manifest is not None else get_manifest(self.cfg))
        if generator is not None:
            self.gen = generator
        else:
            self.gen = LayoutGenerator(
                space_w=self.cfg.spaces.space_width_m,
                space_d=self.cfg.spaces.space_depth_m,
                manifest=self.manifest,
            )
        self._check_scene_config()

    # ----------------------------- manifest / 配置一致性 -----------------------------
    def _check_scene_config(self) -> None:
        """启动期校验 config.scene 与 manifest 的一致性（manifest 为准，仅告警）。"""
        if self.manifest is None:
            return
        issues = scene_config_issues(self.cfg, self.manifest)
        if issues:
            log.warning(
                "config.scene 与布局 manifest 不一致（以 manifest 为准，建议更新 config.yaml）：%s",
                "；".join(issues[:8]) + ("；…" if len(issues) > 8 else ""))

    @property
    def layout_source(self) -> str:
        return "manifest" if self.manifest is not None else "legacy"

    def manifest_meta(self) -> dict:
        """``/api/layout`` 顶层元信息（road_graph_version/manifest_hash 等）。"""
        if self.manifest is None:
            return {"road_graph_version": None, "manifest_version": None,
                    "manifest_hash": None, "manifest_path": None,
                    "layout_source": "legacy", "assets3d_url": "/assets3d"}
        return self.manifest.meta()

    def manifest_summary(self) -> Optional[dict]:
        return self.manifest.summary() if self.manifest is not None else None


    # ----------------------------- 场景 / 闸口 -----------------------------
    def _scene_dict(self) -> dict:
        """场景几何：config 旧键 + manifest 权威值（manifest 为准，保留旧字段）。"""
        scene = dict(self.cfg.scene or {})
        if self.manifest is not None:
            scene.update(manifest_scene_values(self.manifest))
        else:
            scene.update(legacy_scene_values(self.cfg.scene))
        return scene

    def _gates_dict(self) -> dict:
        """闸口/岗亭/外场道：route-v2 出口闸 y=-34.0，停止线按公式。"""
        if self.manifest is not None:
            return manifest_gates_values(self.manifest)
        return legacy_gates_values(self.cfg.scene)

    # ----------------------------- 版本 -----------------------------
    @property
    def _session(self):
        return self.repo.db.session

    def current_version(self) -> int:
        stmt = select(func.max(LayoutHistory.version))
        v = self._session.execute(stmt).scalar()
        return int(v or 0)

    def _next_version(self) -> int:
        return self.current_version() + 1

    # ----------------------------- 查询 -----------------------------
    def current_layout(self) -> LotLayout:
        """GET /api/layout 的数据源：当前生效布局 + 场景 + 闸口。"""
        spaces = [self._to_spec(sp) for sp in self.repo.list_active()]
        zones = self.repo.total_count_by_zone()
        free = self.repo.free_count_by_zone()
        return LotLayout(
            version=self.current_version(),
            spaces=spaces,
            zones=zones,
            free=free,
            free_total=self.repo.free_total(),
            scene=self._scene_dict(),
            gates=self._gates_dict(),
        )

    @staticmethod
    def _to_spec(sp) -> SpaceSpec:
        d = SpaceRepo.to_spec(sp)
        return SpaceSpec(
            space_no=d["space_no"], zone=d["zone"], kind=d["kind"],
            x=d["x"], z=d["z"], rotation=d["rotation"],
            status=SpaceStatus(d["status"]), disabled=d["disabled"],
            current_plate=d.get("current_plate"),
        )

    def specs_from_db(self) -> dict[str, SpaceSpec]:
        """当前生效布局的 space_no → SpaceSpec（用于 diff 的旧值）。"""
        return {sp.space_no: self._to_spec(sp) for sp in self.repo.list_active()}

    # ----------------------------- 变更 -----------------------------
    def _validate_declarations(self, decls: list[ZoneDecl]) -> list[dict]:
        """保留 manifest 固定网格校验（C25/自定义坐标仍 409，不再预演/能力字段）。"""
        problems: list[dict] = []
        if self.manifest is None:
            return problems
        for d in decls:
            zone = str(getattr(d, "zone", "") or "").upper()
            grid = self.manifest.grid(zone)
            rows = self.manifest.row_geoms(zone) or ()
            if grid is None or not rows:
                problems.append({"reason_code": "zone_unknown", "zone": zone,
                                 "message": f"未知区 {zone}；只读场地仅支持 A/B/C/D"})
                continue
            try:
                requested = int(d.count)
            except (TypeError, ValueError):
                problems.append({"reason_code": "invalid_count", "zone": zone,
                                 "message": f"{zone} 区车位数量非法: {getattr(d, 'count', None)!r}"})
                continue
            capacity = int(grid.columns) * len(rows)
            if requested > capacity:
                problems.append({
                    "reason_code": "capacity_exceeded", "zone": zone,
                    "requested": requested, "capacity": capacity,
                    "message": f"{zone} 区只读场地最多 {capacity} 个标准车位，"
                               f"请求 {requested} 个；不支持 C25/越界扩容",
                })
                continue
            try:
                generated = self.gen.generate([d])
            except ParkingError as exc:
                problems.append({"reason_code": "geometry_invalid", "zone": zone,
                                 "message": getattr(exc, "message", str(exc))})
                continue
            canonical = {sp.space_no: sp for sp in LayoutGenerator(
                manifest=self.manifest).generate(
                    [ZoneDecl(zone=zone, count=capacity)])}
            for sp in generated:
                expected = canonical.get(sp.space_no)
                if expected is None or any(
                        abs(float(getattr(sp, key)) - float(getattr(expected, key))) > 1e-6
                        for key in ("x", "z", "rotation")):
                    problems.append({
                        "reason_code": "geometry_out_of_manifest",
                        "zone": zone, "space_no": sp.space_no,
                        "message": (f"车位 {sp.space_no} 目标坐标超出只读 manifest 网格；"
                                    "运行时仅支持已有车位启停/类型调整"),
                    })
                    break
        return problems

    def apply_layout(self, decls: Iterable[ZoneDecl] | Iterable[dict],
                     operator: str = "system", *, publish: bool = True) -> "LayoutDiff":  # noqa: F821
        """按声明重排布局：生成 → 差异 → 安全校验 → upsert/disable → 留痕 → 广播。

        不自行 commit（由调用方 db.scope() 负责）。返回 LayoutDiff。
        """
        decl_list = self._as_decls(decls)
        problems = self._validate_declarations(decl_list)
        if problems:
            first = dict(problems[0])
            reason = first.pop("reason_code", "layout_conflict")
            msg = first.pop("message", "布局声明超出只读场地能力")
            raise LayoutConflict(msg, reason_code=reason, violations=problems, **first)
        new_specs = self.gen.generate(decl_list)         # 内部含静态校验
        self._check_manifest_geometry(new_specs)
        new_map = {s.space_no: s for s in new_specs}
        old_map = self.specs_from_db()

        diff = self.gen.diff(old_map, new_specs, version=self._next_version())
        self._safety_check(old_map, new_map, diff.removed)

        # 落库：先 upsert 新增/变更，再软删除移除项
        for s in new_specs:
            self.repo.upsert({
                "space_no": s.space_no, "zone": s.zone,
                "kind": s.kind.value, "x": s.x, "z": s.z,
                "rotation": s.rotation,
            })
        for space_no in diff.removed:
            self.repo.disable(space_no)
        self._session.flush()

        version = self._record_history(diff.version, decl_list, diff, operator)
        diff.version = version
        log.info("布局变更 v%s by %s: %s（layout_source=%s, road_graph=%s）",
                 version, operator, diff.summary, self.layout_source,
                 self.manifest.road_graph_version if self.manifest else "-")

        if publish:
            self.bus.publish(LayoutChanged(version=version, diff=diff.summary))
        return diff

    def bootstrap_default(self, operator: str = "system") -> bool:
        """首次启动/空库时按 config.default_layout 生成布局。

        已有生效车位则跳过（返回 False）。不自行 commit。
        """
        if self.repo.active_total() > 0:
            return False
        raw = self.cfg.default_layout or []
        if not raw:
            return False
        self.apply_layout(raw, operator=operator, publish=False)
        log.info("已按默认声明初始化布局: %s（manifest=%s）", raw,
                 path_text(self.manifest.path) if self.manifest else "legacy")
        return True

    def _as_decls(self, decls) -> list[ZoneDecl]:
        decls = list(decls)
        if decls and isinstance(decls[0], dict):
            return decls_from_dicts(decls)
        return list(decls)

    def _check_manifest_geometry(self, specs: list[SpaceSpec]) -> None:
        if self.manifest is None:
            return
        # Runtime routes and source GLBs are fixed to the manifest grid.
        canonical = {}
        for zone in "ABCD":
            grid = self.manifest.grid(zone)
            if grid is None:
                continue
            capacity = int(grid.columns) * len(self.manifest.row_geoms(zone))
            for sp in LayoutGenerator(manifest=self.manifest).generate([
                    ZoneDecl(zone=zone, count=capacity)]):
                canonical[sp.space_no] = sp
        for sp in specs:
            expected = canonical.get(sp.space_no)
            if expected is None or any(abs(float(getattr(sp, key)) - float(getattr(expected, key))) > 1e-6
                                       for key in ("x", "z", "rotation")):
                raise LayoutConflict(
                    f"车位 {sp.space_no} 超出只读manifest网格；运行时仅支持已有车位启停和类型调整",
                    space_no=sp.space_no)

    def _safety_check(self, old_map: dict[str, SpaceSpec],
                      new_map: dict[str, SpaceSpec],
                      removed: list[str]) -> None:
        # 1) 不得移除被占用的车位
        for space_no in removed:
            old = old_map.get(space_no)
            if old is not None and old.status == SpaceStatus.OCCUPIED:
                raise LayoutConflict(
                    f"车位 {space_no} 正被占用（{old.current_plate}），"
                    f"不能移除；请先引导车辆离场", space_no=space_no)
        # 2) 变更后容量不得小于当前占用数（防"缩到没位"）
        occupied = self.repo.occupied_count()
        new_capacity = len(new_map)
        if new_capacity < occupied:
            raise LayoutConflict(
                f"新布局容量 {new_capacity} 小于当前在场车辆数 {occupied}")

    def _record_history(self, version: int, decls: list[ZoneDecl],
                        diff, operator: str) -> int:
        hist = LayoutHistory(
            ts=utcnow(),
            operator=operator,
            decl=json.dumps([d.model_dump(mode="json") for d in decls],
                            ensure_ascii=False),
            diff=json.dumps(diff.model_dump(mode="json"), ensure_ascii=False),
            version=version,
        )
        self._session.add(hist)
        self._session.flush()
        return version
