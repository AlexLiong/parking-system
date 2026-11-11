"""声明式车位布局生成器（layout/generator.py）。

职责：把管理员的"声明"（区 + 数量 + 类型，见 core/domain.py::ZoneDecl）翻译成
一组带精确 3D 坐标的车位规格（SpaceSpec）。坐标与 **route-v2 单一事实源**
``assets_3d/layout/lot_v2.json``（见 ``layout/manifest.py``）对齐，使前端自绘的
车位线与 glb 场景（闸口 / 岗亭 / 导视 / 环境）严丝合缝。

设计要点（§3.9 声明式布局 + 审查报告 §8.5.4/§8.9.4）：
- 生成器是**纯函数式**的：不碰数据库、不碰事件，输入声明输出规格，便于单测；
- 4 个标准区（A/B/C/D）的**行 y 坐标/朝向/支路宽度/排距来自 manifest**：
  A/C 远排 25.75、近排 13.25；B/D 近排 -7.25、远排 -19.75；支路 6.5 m，
  排距 = 车位深 6.0 + 支路 6.5 = 12.5 m（旧版为 25/14/-14/-25、排距 11.0）；
  x 起点/列距/列数沿用（-36/6、3.0、10 列）；
- manifest 缺失/非法时**回退旧版常量并告警**（兼容旧部署与离线测试）；
- 管理员也可用 anchor/columns/rows/spacing/heading 覆盖，生成自定义矩形网格
  （自定义排距同样取 manifest 的 ``row_pitch``，保证与标准区一致）；
- 只做几何与容量校验（LayoutConflict），不做"是否停用被占车位"这类运行时安全
  校验——那属于 LayoutService（需要 DB 状态）。

坐标约定（与场景一致，单位：米）：
- x 轴左右，z 轴前后（three.js 里映射为 x/z 平面；后端 z == Blender y）；
- rotation 用**度**（degrees），与场景 ``rotation_euler.z = radians(deg)`` 一致；
  车头朝向：北半区远排 180° / 近排 0°，南半区近排 180° / 远排 0°（manifest rows）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from ..core.domain import LayoutDiff, SpaceKind, SpaceSpec, ZoneDecl
from ..core.errors import LayoutConflict
from .manifest import Manifest, get_manifest

log = logging.getLogger("layout.generator")

# ----------------------------- 旧版场景几何常量（manifest 缺失时回退） -----------------------------
LOT_X = 36.0
SPACE_W, SPACE_D = 3.0, 6.0          # 车位宽 x 深
ZONE_L_X = -36.0                     # A/B 区起始 x（左半）
ZONE_R_X = 6.0                       # C/D 区起始 x（右半）
# route-v2：A/C 25.75/13.25；B/D -7.25/-19.75（下列常量保留旧值作回退）
ROW_TOP_FAR, ROW_TOP_NEAR = 25.0, 14.0
ROW_BOT_NEAR, ROW_BOT_FAR = -14.0, -25.0

# 车头朝向（度）：远排朝内(180)，近排朝内(0)
HEAD_FAR, HEAD_NEAR_TOP = 180.0, 0.0
HEAD_NEAR_BOT, HEAD_FAR_BOT = 180.0, 0.0

# 旧版支路宽度（route-v2 为 6.5）与排距（旧版 11.0；v2 = 6.0 + 6.5 = 12.5）
AISLE = 5.0
ROW_PITCH = SPACE_D + AISLE          # 11.0

#: ``LayoutGenerator(manifest=...)`` 的默认哨兵：自动加载 manifest（缺失则回退旧常量）。
_AUTO_MANIFEST = object()


@dataclass(frozen=True)
class _RowGeom:
    """一排车位的几何：z 坐标（Blender y）+ 车头朝向（度）。"""

    z: float
    heading: float


@dataclass(frozen=True)
class ZoneGeom:
    """一个标准区的默认几何：起始 x + 若干排（按编号顺序）。"""

    x_start: float
    rows: tuple[_RowGeom, ...]
    default_kind: SpaceKind = SpaceKind.NORMAL


# 4 个标准区（旧版几何；仅 manifest 缺失时使用。顺序即编号顺序）
DEFAULT_ZONE_GEOM: dict[str, ZoneGeom] = {
    "A": ZoneGeom(ZONE_L_X, (_RowGeom(ROW_TOP_FAR, HEAD_FAR),
                             _RowGeom(ROW_TOP_NEAR, HEAD_NEAR_TOP))),
    "C": ZoneGeom(ZONE_R_X, (_RowGeom(ROW_TOP_FAR, HEAD_FAR),
                             _RowGeom(ROW_TOP_NEAR, HEAD_NEAR_TOP)),
                  default_kind=SpaceKind.NE_PREFER),
    "B": ZoneGeom(ZONE_L_X, (_RowGeom(ROW_BOT_NEAR, HEAD_NEAR_BOT),
                             _RowGeom(ROW_BOT_FAR, HEAD_FAR_BOT))),
    "D": ZoneGeom(ZONE_R_X, (_RowGeom(ROW_BOT_NEAR, HEAD_NEAR_BOT),
                             _RowGeom(ROW_BOT_FAR, HEAD_FAR_BOT))),
}

DEFAULT_COLUMNS = 10
DEFAULT_ROWS = 2

_ZONE_DEFAULT_KIND = {"C": SpaceKind.NE_PREFER}


def zone_geom_from_manifest(m: Manifest) -> dict[str, ZoneGeom]:
    """由 manifest 的 ``rows`` + ``space_grid`` 构造 4 个标准区几何。

    行 y/朝向取 ``rows``（A: 25.75/13.25，B: -7.25/-19.75），x 起点取
    ``space_grid.<zone>.x_start``；C 区默认新能源优先（与历史约定一致）。
    """
    out: dict[str, ZoneGeom] = {}
    for zone in "ABCD":
        rows = [r for r in (m.row_geoms(zone) or ())]
        if not rows:  # manifest 缺该区 → 回退旧几何
            out[zone] = DEFAULT_ZONE_GEOM[zone]
            continue
        grid = m.grid(zone)
        x_start = float(grid.x_start) if grid is not None else DEFAULT_ZONE_GEOM[zone].x_start
        out[zone] = ZoneGeom(
            x_start=x_start,
            rows=tuple(_RowGeom(z=float(r.y), heading=float(r.rotation)) for r in rows),
            default_kind=_ZONE_DEFAULT_KIND.get(zone, SpaceKind.NORMAL),
        )
    return out


class LayoutGenerator:
    """把布局声明翻译为车位规格；无状态，可安全共享。

    ``manifest`` 参数：
    - 默认（不传）→ 自动加载 ``assets_3d/layout/lot_v2.json``；
    - 传入 :class:`Manifest` → 使用该 manifest（测试/多环境注入）；
    - 传入 ``None`` → 强制旧版常量（离线/回退测试）。
    """

    def __init__(self, *, space_w: float = SPACE_W, space_d: float = SPACE_D,
                 manifest: Manifest | None | object = _AUTO_MANIFEST,
                 row_pitch: Optional[float] = None):
        self.space_w = float(space_w)
        self.space_d = float(space_d)
        if manifest is _AUTO_MANIFEST:
            manifest = get_manifest()
        self.manifest: Optional[Manifest] = manifest if isinstance(manifest, Manifest) else None
        self._using_manifest = self.manifest is not None
        if self.manifest is not None:
            self.zone_geom: dict[str, ZoneGeom] = zone_geom_from_manifest(self.manifest)
            self.aisle_w = float(self.manifest.aisle_width)
            self.row_pitch = float(row_pitch if row_pitch is not None
                                   else self.manifest.row_pitch)
        else:
            self.zone_geom = dict(DEFAULT_ZONE_GEOM)
            self.aisle_w = AISLE
            self.row_pitch = float(row_pitch if row_pitch is not None else ROW_PITCH)
            log.warning(
                "布局 manifest 不可用，LayoutGenerator 回退旧版几何常量"
                "（A/C 25/14、B/D -14/-25、支路 5.0 m）；生产部署请确保 "
                "assets_3d/layout/lot_v2.json 存在")

    # ----------------------------- 元信息 -----------------------------
    @property
    def using_manifest(self) -> bool:
        return self._using_manifest

    def manifest_meta(self) -> dict:
        m = self.manifest
        if m is None:
            return {"layout_source": "legacy", "road_graph_version": None,
                    "manifest_hash": None, "manifest_path": None,
                    "aisle_w": AISLE, "row_pitch": ROW_PITCH}
        return {**m.meta(), "aisle_w": self.aisle_w, "row_pitch": self.row_pitch,
                "using_manifest": True}

    # ----------------------------- 校验 -----------------------------
    def validate(self, decls: Iterable[ZoneDecl]) -> list[ZoneDecl]:
        """静态校验声明（不涉及 DB）。失败抛 LayoutConflict。"""
        decls = list(decls)
        if not decls:
            raise LayoutConflict("布局声明为空")
        seen_zones: set[str] = set()
        for d in decls:
            zone = (d.zone or "").strip().upper()
            if not zone:
                raise LayoutConflict("区名不能为空")
            if zone in seen_zones:
                raise LayoutConflict(f"区重复声明: {zone}")
            seen_zones.add(zone)
            if d.count <= 0:
                raise LayoutConflict(f"{zone} 区车位数必须为正: {d.count}")
            columns = self._columns_for(d)
            rows = d.rows if d.rows and d.rows > 0 else DEFAULT_ROWS
            capacity = columns * rows
            if d.count > capacity:
                raise LayoutConflict(
                    f"{zone} 区声明 {d.count} 个车位，超过网格容量 "
                    f"{capacity}（columns={columns} × rows={rows}）")
        return decls

    # ----------------------------- 生成 -----------------------------
    def generate(self, decls: Iterable[ZoneDecl]) -> list[SpaceSpec]:
        """生成全部车位规格（已排序：按区、按编号）。"""
        decls = self.validate(decls)
        specs: list[SpaceSpec] = []
        for d in decls:
            specs.extend(self._gen_zone(d))
        self._check_no_overlap(specs)
        return specs

    # ----------------------------- 兼容旧接口（供其它模块读取） -----------------------------
    def default_zone_geom(self, zone: str) -> Optional[ZoneGeom]:
        return self.zone_geom.get(zone.strip().upper())

    def _columns_for(self, decl: ZoneDecl) -> int:
        if decl.columns and decl.columns > 0:
            return int(decl.columns)
        grid = self.manifest.grid(decl.zone.strip().upper()) if self.manifest else None
        return int(grid.columns) if grid is not None else DEFAULT_COLUMNS

    def _grid(self, zone: str):
        return self.manifest.grid(zone) if self.manifest else None

    def _gen_zone(self, decl: ZoneDecl) -> list[SpaceSpec]:
        zone = decl.zone.strip().upper()
        geom = self.zone_geom.get(zone)
        columns = self._columns_for(decl)
        rows = decl.rows if decl.rows and decl.rows > 0 else DEFAULT_ROWS
        grid = self._grid(zone)
        # 列距沿用 manifest space_grid.pitch（v2=3.0）；声明可覆盖
        if decl.spacing is not None:
            spacing = float(decl.spacing)
        elif grid is not None:
            spacing = float(grid.pitch)
        else:
            spacing = self.space_w

        # kind：声明优先，否则用该区默认（C 区默认新能源优先）
        kind = decl.kind if decl.kind is not None else (
            geom.default_kind if geom else SpaceKind.NORMAL)

        # 排几何：默认用 manifest/旧表；anchor 覆盖则生成向下(-z)递进的自定义网格
        row_geoms = self._resolve_rows(decl, geom, rows)
        x_start = self._resolve_x_start(decl, geom, grid)

        specs: list[SpaceSpec] = []
        idx = 0
        for r in range(rows):
            if idx >= decl.count:
                break
            rg = row_geoms[r]
            heading = decl.heading if decl.heading is not None else rg.heading
            for k in range(columns):
                if idx >= decl.count:
                    break
                x = x_start + spacing * (k + 0.5)
                specs.append(SpaceSpec(
                    space_no=f"{zone}-{idx + 1:02d}",
                    zone=zone,
                    kind=kind,
                    x=round(x, 4),
                    z=round(rg.z, 4),
                    rotation=round(float(heading), 4),
                ))
                idx += 1
        return specs

    def _resolve_x_start(self, decl: ZoneDecl, geom: Optional[ZoneGeom], grid) -> float:
        if decl.anchor is not None:
            return float(decl.anchor[0])
        if grid is not None:
            return float(grid.x_start)
        if geom is not None:
            return geom.x_start
        # 未知区且无 anchor：从 0 开始（一般不会发生，validate 已限制区名）
        return 0.0

    def _resolve_rows(self, decl: ZoneDecl, geom: Optional[ZoneGeom],
                      rows: int) -> list[_RowGeom]:
        # anchor 覆盖 或 非标准区 或 rows 与默认不符 → 生成自定义网格
        use_custom = (decl.anchor is not None or geom is None
                      or rows != len(geom.rows))
        if not use_custom:
            return list(geom.rows)
        z0 = float(decl.anchor[1]) if decl.anchor is not None else (
            geom.rows[0].z if geom else 0.0)
        out: list[_RowGeom] = []
        for r in range(rows):
            # 每排向 -z 方向推进一个 row_pitch（manifest 派生：6.0+6.5=12.5）；
            # 朝向交替（远排 180 / 近排 0）
            heading = HEAD_FAR if r % 2 == 0 else HEAD_NEAR_TOP
            out.append(_RowGeom(z0 - r * self.row_pitch, heading))
        return out

    def _check_no_overlap(self, specs: list[SpaceSpec]) -> None:
        """车位坐标不得重复（几何冲突保护）。"""
        seen_no: set[str] = set()
        seen_pos: set[tuple[float, float]] = set()
        for s in specs:
            if s.space_no in seen_no:
                raise LayoutConflict(f"车位编号重复: {s.space_no}")
            seen_no.add(s.space_no)
            pos = (round(s.x, 3), round(s.z, 3))
            if pos in seen_pos:
                raise LayoutConflict(f"车位坐标重叠: {s.space_no} @ {pos}")
            seen_pos.add(pos)

    # ----------------------------- 差异 -----------------------------
    def diff(self, old: dict[str, SpaceSpec], new: Iterable[SpaceSpec],
             *, version: int = 0) -> LayoutDiff:
        """比较旧布局（space_no → spec）与新布局，产出增删改摘要。"""
        new_map = {s.space_no: s for s in new}
        added = sorted(k for k in new_map if k not in old)
        removed = sorted(k for k in old if k not in new_map)
        changed: list[str] = []
        for k, ns in new_map.items():
            os_ = old.get(k)
            if os_ is None:
                continue
            if self._geom_differs(os_, ns):
                changed.append(k)
        changed.sort()
        summary = (f"+{len(added)} 新增, -{len(removed)} 移除, "
                   f"~{len(changed)} 变更")
        return LayoutDiff(version=version, added=added, removed=removed,
                          changed=changed, summary=summary)

    @staticmethod
    def _geom_differs(a: SpaceSpec, b: SpaceSpec) -> bool:
        return (abs(a.x - b.x) > 1e-6 or abs(a.z - b.z) > 1e-6
                or abs(a.rotation - b.rotation) > 1e-6
                or _kind_val(a.kind) != _kind_val(b.kind))


def _kind_val(k) -> str:
    return k.value if isinstance(k, SpaceKind) else str(k)


def decls_from_dicts(raw: Iterable[dict]) -> list[ZoneDecl]:
    """把配置/请求里的 dict 列表转成 ZoneDecl（大小写归一、类型宽容）。"""
    out: list[ZoneDecl] = []
    for item in raw or []:
        d = dict(item)
        if "zone" in d:
            d["zone"] = str(d["zone"]).strip().upper()
        if "kind" in d and d["kind"] is not None:
            d["kind"] = SpaceKind(str(d["kind"]).strip().lower())
        if "anchor" in d and d["anchor"] is not None:
            d["anchor"] = tuple(float(v) for v in d["anchor"])
        out.append(ZoneDecl(**d))
    return out
