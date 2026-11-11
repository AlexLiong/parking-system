"""布局 manifest 加载与校验（layout/manifest.py）——route-v2 单一事实源。

背景（审查报告第 8 章方案 C / `_qa_audit/route_v2/contract.md`）：
场地/路网/车位排布的**单一事实源**是 ``assets_3d/layout/lot_v2.json``（由
``_qa_audit/route_v2/build_manifest.py`` 生成，禁止手改）。Blender 建模、
Three.js 路网与后端布局三方共同消费该文件；本模块负责：

1. **定位** manifest（优先显式路径/环境变量/配置，其次仓库内默认位置）；
2. **加载 + 校验**：版本、``manifest_hash``（与生成脚本同一规范化算法）、
   节点/边完整性（id 唯一、端点存在、边长>0、冲突区引用有效）、车位网格/行
   （A–D 四区、80 车位）、闸口停止线公式（``paint = gate − 0.3``）、排队长度；
3. 校验失败或文件缺失 → **回退旧常量**（``legacy_*``），并记录告警（不崩溃，
   保证老部署/缺 manifest 时系统仍可启动，符合“缺失时回退旧常量并告警”要求）；
4. 向 ``LayoutService``/``LayoutGenerator`` 暴露 ``rows/aisles/gates/plazas/
   queue/space_grid`` 与派生场景值（``manifest_scene_values``）。

坐标约定（与 contract 一致）：manifest 全部为 **Blender 坐标**（x 东 / y 北 /
z 上，单位米）。后端 ``parking_space.x/z`` 直接存 manifest 的 ``x/y``（z 即
Blender y），与 3D 端 ``three_z = -blender_y`` 的换算由前端完成。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..core.config import PROJECT_ROOT, AppConfig

log = logging.getLogger("layout.manifest")

#: 项目根（parking-system/）的上一级目录通常是仓库根（assets_3d 所在）。
REPO_PARENT = PROJECT_ROOT.parent

#: manifest 的仓库内默认相对路径（相对“仓库根”，即 assets_3d 的父目录）。
DEFAULT_MANIFEST_REL = Path("assets_3d") / "layout" / "lot_v2.json"

#: 环境变量：显式指定 manifest 路径（跨平台/跨机器部署时使用）。
ENV_MANIFEST = "PARKING_LAYOUT_MANIFEST"

#: 冲突区 id 中允许动态生成的 Z7（逐车位，由前端 planner 生成，manifest 中 rect 为空）。
DYNAMIC_ZONE_IDS = frozenset({"Z7"})

#: 校验闸口停止线公式容差（米）。
_STOP_TOL = 1e-3

#: 旧版（route-v1）场景常量：**仅** 在 manifest 缺失/非法时作为回退。
LEGACY_ROWS: dict[str, tuple[tuple[float, float], ...]] = {
    "A": ((25.0, 180.0), (14.0, 0.0)),
    "C": ((25.0, 180.0), (14.0, 0.0)),
    "B": ((-14.0, 180.0), (-25.0, 0.0)),
    "D": ((-14.0, 180.0), (-25.0, 0.0)),
}
LEGACY_GATES = {
    "in": {"x": -3.5, "y": -25.5, "stop_paint_y": -27.5},
    "out": {"x": 3.5, "y": -25.5, "stop_paint_y": -23.5},
}
LEGACY_AISLE_W = 5.0
LEGACY_AISLE_N, LEGACY_AISLE_S = 14.0, -25.0
LEGACY_OUTER_ROAD_Y = -46.0
LEGACY_BOOTH = {"x": 0.0, "y": -21.5}
LEGACY_SPACE_GRID = {
    "A": {"x_start": -36.0, "pitch": 3.0, "columns": 10, "rows": LEGACY_ROWS["A"]},
    "B": {"x_start": -36.0, "pitch": 3.0, "columns": 10, "rows": LEGACY_ROWS["B"]},
    "C": {"x_start": 6.0, "pitch": 3.0, "columns": 10, "rows": LEGACY_ROWS["C"]},
    "D": {"x_start": 6.0, "pitch": 3.0, "columns": 10, "rows": LEGACY_ROWS["D"]},
}


class ManifestError(RuntimeError):
    """manifest 严格加载失败（``strict=True`` 时抛出）。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RowSpec:
    """一排车位：Blender y 坐标 + 车头朝向（度）。"""

    y: float
    rotation: float
    side: str = ""


@dataclass(frozen=True)
class SpaceGridSpec:
    """一个区的车位网格（x 起点/列距/列数/每排几何）。"""

    zone: str
    x_start: float
    pitch: float
    columns: int
    rows: tuple[RowSpec, ...]

    @property
    def capacity(self) -> int:
        return max(0, self.columns) * len(self.rows)


@dataclass(frozen=True)
class Manifest:
    """校验通过后的 manifest 视图（不可变；``raw`` 保留原始字典）。"""

    path: Path
    raw: Mapping[str, Any]
    manifest_version: int
    road_graph_version: int
    manifest_hash: str
    coordinate_system: str
    lot: Mapping[str, Any]
    outer_road: Mapping[str, Any]
    apron: Mapping[str, Any]
    corridor: Mapping[str, Any]
    gates: Mapping[str, Any]
    aisles: Mapping[str, Any]
    plazas: Mapping[str, Any]
    rows: Mapping[str, tuple[RowSpec, ...]]
    space_grid: Mapping[str, SpaceGridSpec]
    zone_lanes: Mapping[str, Any]
    zone_ports: Mapping[str, Any]
    aisle_nodes: Mapping[str, Any]
    queue: Mapping[str, Any]
    conflict_zones: tuple[Mapping[str, Any], ...]
    nodes: Mapping[str, tuple[float, float]]
    edges: tuple[Mapping[str, Any], ...]
    props: Mapping[str, Any]
    palette: Mapping[str, Any]
    markings: Mapping[str, Any]
    features: Mapping[str, Any]
    issues: tuple[str, ...] = field(default_factory=tuple)

    # ----------------------------- 派生 -----------------------------
    @property
    def aisle_width(self) -> float:
        return float(self.aisles.get("width", LEGACY_AISLE_W))

    @property
    def row_pitch(self) -> float:
        """标准区两排的 y 间距（米）：优先由 A 区两排实测，回退 车位深+支路宽。"""
        rows_a = self.rows.get("A") or ()
        if len(rows_a) >= 2:
            pitch = abs(float(rows_a[0].y) - float(rows_a[1].y))
            if pitch > 0:
                return round(pitch, 6)
        depth = float(self.markings.get("space_d", 6.0) or 6.0)
        return round(depth + self.aisle_width, 6)

    def gate(self, which: str) -> Mapping[str, Any]:
        """``which`` ∈ {in, out}；返回 manifest 的闸口定义（缺失回退旧常量）。"""
        return self.gates.get(which) or LEGACY_GATES.get(which, {})

    def stop_paint_y(self, which: str) -> float:
        gate = self.gate(which)
        if "stop_paint_y" in gate:
            return float(gate["stop_paint_y"])
        y = float(gate.get("y", 0.0))
        return round(y - float(gate.get("stop_clearance_m", 0.3) or 0.3), 6)

    def stop_center_y(self, which: str) -> float:
        # queue 以 entry/exit 命名，gates 以 in/out 命名；两种写法都接受
        q = self._queue(which)
        if "from_y" in q:
            return float(q["from_y"])
        # 回退：闸口 ±（净距 + 最大车型半长 2.6 m）
        gate = self.gate(which)
        clearance = float(gate.get("stop_clearance_m", 0.3) or 0.3)
        sign = -1.0 if which in ("in", "entry") else 1.0
        return round(float(gate.get("y", 0.0)) + sign * (clearance + 2.6), 6)

    def queue_length(self, which: str) -> float:
        q = self._queue(which)
        if "length_m" in q:
            return float(q["length_m"])
        return 0.0

    def _queue(self, which: str) -> Mapping[str, Any]:
        key = {"in": "entry", "out": "exit"}.get(which, which)
        return (self.queue or {}).get(key) or {}

    def plaza(self, which: str) -> Mapping[str, Any]:
        return (self.plazas or {}).get(which) or {}

    def lane(self, aisle: str, direction: str) -> Optional[float]:
        lanes_key = "n_lanes" if aisle == "n" else "s_lanes"
        lanes = (self.aisles or {}).get(lanes_key) or {}
        value = lanes.get(direction)
        return None if value is None else float(value)

    def row_geoms(self, zone: str) -> Optional[tuple[RowSpec, ...]]:
        return self.rows.get(zone)

    def grid(self, zone: str) -> Optional[SpaceGridSpec]:
        return self.space_grid.get(zone)

    # ----------------------------- 摘要 -----------------------------
    def meta(self) -> dict:
        """``/api/layout`` 顶层元信息（不含大字段）。"""
        return {
            "road_graph_version": self.road_graph_version,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "manifest_path": path_text(self.path),
            "layout_source": "manifest",
            "assets3d_url": "/assets3d",
        }

    def summary(self) -> dict:
        """供前端/调试使用的路网摘要（不包含 nodes/edges 全量）。"""
        return {
            "version": self.road_graph_version,
            "manifest_version": self.manifest_version,
            "hash": self.manifest_hash,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "conflict_zones": [str(z.get("id")) for z in self.conflict_zones],
            "space_count": sum(g.capacity for g in self.space_grid.values()),
            "gates": {
                which: {
                    "x": float(self.gate(which).get("x", 0.0)),
                    "y": float(self.gate(which).get("y", 0.0)),
                    "stop_paint_y": self.stop_paint_y(which),
                    "stop_center_y": self.stop_center_y(which),
                }
                for which in ("in", "out")
            },
        }


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def _existing(candidates: Iterable[Optional[Path | str]]) -> Optional[Path]:
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if not p.is_absolute():
            candidates_abs = (PROJECT_ROOT / p, REPO_PARENT / p)
        else:
            candidates_abs = (p,)
        for ap in candidates_abs:
            try:
                if ap.is_file():
                    return ap.resolve()
            except OSError:  # pragma: no cover - 极端路径错误
                continue
    return None


def _cfg_raw(cfg: Optional[AppConfig]) -> Mapping[str, Any]:
    if cfg is None:
        return {}
    raw = getattr(cfg, "raw", None)
    return raw if isinstance(raw, Mapping) else {}


def resolve_manifest_path(cfg: Optional[AppConfig] = None,
                          path: Optional[str | Path] = None) -> Optional[Path]:
    """解析 manifest 路径（不读文件）。

    优先级：
    1. 显式 ``path``（测试/CLI）；
    2. 环境变量 ``PARKING_LAYOUT_MANIFEST``；
    3. ``config.paths.layout_manifest``（**待 core/config.py 扩展字段后启用**）；
    4. 顶层 ``layout_manifest``；
    5. ``scene.layout_manifest``；
    6. 仓库默认位置：``<repo>/assets_3d/layout/lot_v2.json``。

    相对路径依次按“项目根（parking-system）”与“仓库根（parking-system 的父目录，
    assets_3d 常见位置）”解析，命中即返回；全部不存在返回 None。
    """
    raw = _cfg_raw(cfg)
    paths_cfg = raw.get("paths") if isinstance(raw.get("paths"), Mapping) else {}
    scene_cfg = raw.get("scene") if isinstance(raw.get("scene"), Mapping) else {}
    # 显式参数/环境变量具有最高优先级（即使不存在也不静默回退到仓库默认，
    # 便于测试/多环境明确指定“用哪个 manifest”）；
    # 显式值缺失时交由上层回退旧常量并告警。
    if path:
        return _existing([path])
    env = os.environ.get(ENV_MANIFEST)
    if env:
        return _existing([env])
    return _existing([
        (paths_cfg or {}).get("layout_manifest"),
        raw.get("layout_manifest"),
        (scene_cfg or {}).get("layout_manifest"),
        DEFAULT_MANIFEST_REL,
    ])


def resolve_assets_root(cfg: Optional[AppConfig] = None,
                        manifest: Optional[Manifest] = None) -> Optional[Path]:
    """返回 ``assets_3d`` 目录（存在才返回），供 ``/assets3d`` 静态挂载。"""
    if manifest is not None:
        # manifest 位于 assets_3d/layout/lot_v2.json → 父目录即 assets_3d
        root = manifest.path.parent.parent
        if root.is_dir():
            return root
    p = resolve_manifest_path(cfg)
    if p is not None and p.parent.parent.is_dir():
        return p.parent.parent
    for base in (PROJECT_ROOT, REPO_PARENT):
        cand = base / "assets_3d"
        if cand.is_dir():
            return cand.resolve()
    return None


def path_text(path: Path) -> str:
    """尽量以仓库内相对路径输出（便于跨平台日志/接口展示）。"""
    try:
        return path.relative_to(REPO_PARENT).as_posix()
    except ValueError:
        pass
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def canonical_hash(raw: Mapping[str, Any]) -> str:
    """与 ``_qa_audit/route_v2/build_manifest.py`` 完全一致的规范化哈希。

    生成脚本：``json.dumps(manifest_without_hash, ensure_ascii=False,
    sort_keys=True, separators=(",", ":"))`` 的 sha256 前 16 位。
    """
    body = {k: v for k, v in raw.items() if k != "manifest_hash"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()[:16]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rect_ok(rect: Any) -> bool:
    return (isinstance(rect, (list, tuple)) and len(rect) == 4
            and all(_is_num(x) for x in rect) and rect[0] < rect[2] and rect[1] < rect[3])


def validate_manifest(raw: Mapping[str, Any],
                      path: Optional[Path] = None) -> tuple[bool, list[str]]:
    """校验 manifest 结构与完整性。

    返回 ``(ok, issues)``；``issues`` 为空表示完全通过。**致命项**（结构缺失/
    hash 不符/版本不支持/边引用错误等）会导致 ``ok=False`` → 上层回退旧常量。
    """
    issues: list[str] = []

    def fatal(msg: str) -> None:
        issues.append(f"[FATAL] {msg}")

    def warn(msg: str) -> None:
        issues.append(f"[WARN] {msg}")

    if not isinstance(raw, Mapping) or not raw:
        return False, ["[FATAL] manifest 为空或不是 JSON 对象"]

    # 1) 必需顶层段
    required = ("manifest_version", "road_graph_version", "coordinate_system",
                "lot", "outer_road", "apron", "corridor", "gates", "aisles",
                "plazas", "rows", "space_grid", "queue", "conflict_zones",
                "nodes", "edges")
    missing = [k for k in required if k not in raw]
    if missing:
        fatal(f"缺少必需字段: {missing}")

    # 2) 版本
    mv = raw.get("manifest_version")
    if not isinstance(mv, int) or mv < 2:
        fatal(f"manifest_version 需为 int 且 ≥2，实际 {mv!r}")
    rgv = raw.get("road_graph_version")
    if not isinstance(rgv, int) or rgv < 1:
        fatal(f"road_graph_version 需为 int 且 ≥1，实际 {rgv!r}")
    if raw.get("coordinate_system") != "blender_xy":
        fatal(f"coordinate_system 需为 'blender_xy'，实际 {raw.get('coordinate_system')!r}")

    # 3) hash（禁止手改 JSON 的防线）
    stored = raw.get("manifest_hash")
    if not isinstance(stored, str) or not stored.startswith("sha256:"):
        fatal(f"manifest_hash 缺失或格式错误: {stored!r}")
    else:
        calc = canonical_hash(raw)
        if calc != stored:
            fatal(f"manifest_hash 不匹配（stored={stored} computed={calc}）："
                  f"manifest 可能被手工修改，请用 build_manifest.py 重新生成")

    # 4) 节点与边完整性
    nodes_raw = raw.get("nodes")
    node_ids: set[str] = set()
    if not isinstance(nodes_raw, list) or not nodes_raw:
        fatal("nodes 需为非空数组")
    else:
        for n in nodes_raw:
            if not isinstance(n, Mapping) or "id" not in n or "x" not in n or "y" not in n:
                fatal(f"节点结构非法: {n!r}")
                continue
            nid = str(n["id"])
            if nid in node_ids:
                fatal(f"节点 id 重复: {nid}")
            node_ids.add(nid)
            if not (_is_num(n["x"]) and _is_num(n["y"])):
                fatal(f"节点 {nid} 坐标非数值")
    edges_raw = raw.get("edges")
    if not isinstance(edges_raw, list) or not edges_raw:
        fatal("edges 需为非空数组")
    else:
        edge_ids: set[str] = set()
        for e in edges_raw:
            if not isinstance(e, Mapping):
                fatal(f"边结构非法: {e!r}")
                continue
            eid = str(e.get("id", "?"))
            if eid in edge_ids:
                fatal(f"边 id 重复: {eid}")
            edge_ids.add(eid)
            u, v = e.get("u"), e.get("v")
            if u not in node_ids:
                fatal(f"边 {eid} 起点节点不存在: {u!r}")
            if v not in node_ids:
                fatal(f"边 {eid} 终点节点不存在: {v!r}")
            if u == v:
                fatal(f"边 {eid} 为自环: {u}")
            length = e.get("length")
            if not _is_num(length):
                fatal(f"边 {eid} 长度非法: {length!r}")
            elif float(length) < 0:
                fatal(f"边 {eid} 长度为负: {length!r}")
            elif float(length) == 0 and e.get("kind") != "turn":
                fatal(f"边 {eid} 长度为 0（仅 turn 连接边允许零长）: {length!r}")
        zero_turn = [str(e.get("id")) for e in edges_raw
                     if isinstance(e, Mapping) and e.get("kind") == "turn"
                     and _is_num(e.get("length")) and float(e["length"]) == 0]
        if zero_turn:
            warn(f"{len(zero_turn)} 条 turn 连接边长度为 0（端口/支路节点坐标重合，"
                 f"按别名节点处理）：{zero_turn[:8]}")

    # 5) 冲突区引用有效
    zones_raw = raw.get("conflict_zones")
    zone_ids: set[str] = set()
    if not isinstance(zones_raw, list) or not zones_raw:
        fatal("conflict_zones 需为非空数组")
    else:
        for z in zones_raw:
            if not isinstance(z, Mapping) or "id" not in z:
                fatal(f"冲突区结构非法: {z!r}")
                continue
            zid = str(z["id"])
            zone_ids.add(zid)
            if zid in DYNAMIC_ZONE_IDS:
                continue
            if not _rect_ok(z.get("rect")):
                fatal(f"冲突区 {zid} rect 需为 [xmin,ymin,xmax,ymax]: {z.get('rect')!r}")
        if edges_raw:
            for e in edges_raw:
                if not isinstance(e, Mapping):
                    continue
                for zid in e.get("zones") or []:
                    if zid not in zone_ids:
                        fatal(f"边 {e.get('id')} 引用了不存在的冲突区 {zid!r}")

    # 6) 闸口与停止线（paint = gate_y − clearance，入/出口符号不同）
    gates = raw.get("gates") or {}
    for which in ("in", "out"):
        g = gates.get(which) if isinstance(gates, Mapping) else None
        if not isinstance(g, Mapping):
            fatal(f"缺少闸口 gates.{which}")
            continue
        for key in ("x", "y"):
            if not _is_num(g.get(key)):
                fatal(f"闸口 {which}.{key} 非数值: {g.get(key)!r}")
        paint = g.get("stop_paint_y")
        clearance = g.get("stop_clearance_m")
        if not _is_num(paint) or not _is_num(clearance):
            fatal(f"闸口 {which} 缺少 stop_paint_y/stop_clearance_m")
        elif abs(abs(float(g["y"]) - float(paint)) - float(clearance)) > _STOP_TOL:
            fatal(f"闸口 {which} 停止线公式不成立: |y-paint| != clearance "
                  f"({g['y']} / {paint} / {clearance})")

    # 7) 行与车位网格（A–D 四区、20 车位/区、行与网格一致）
    rows = raw.get("rows") or {}
    grid = raw.get("space_grid") or {}
    for zone in "ABCD":
        zone_rows = rows.get(zone) if isinstance(rows, Mapping) else None
        if not isinstance(zone_rows, list) or len(zone_rows) != 2:
            fatal(f"rows.{zone} 需为 2 排，实际 {zone_rows!r}")
            continue
        for r in zone_rows:
            if not isinstance(r, Mapping) or not _is_num(r.get("y")) or not _is_num(r.get("rotation")):
                fatal(f"rows.{zone} 排行结构非法: {r!r}")
        g = grid.get(zone) if isinstance(grid, Mapping) else None
        if not isinstance(g, Mapping):
            fatal(f"缺少 space_grid.{zone}")
            continue
        if not _is_num(g.get("x_start")) or not _is_num(g.get("pitch")) \
                or not isinstance(g.get("columns"), int) or g.get("columns", 0) < 1:
            fatal(f"space_grid.{zone} x_start/pitch/columns 非法")
            continue
        if float(g["pitch"]) <= 0:
            fatal(f"space_grid.{zone} pitch 必须为正")
        grows = g.get("rows")
        if not isinstance(grows, list) or len(grows) != len(zone_rows):
            fatal(f"space_grid.{zone}.rows 与 rows.{zone} 排数不一致")
        elif isinstance(zone_rows, list):
            for a, b in zip(zone_rows, grows):
                if not isinstance(b, Mapping):
                    fatal(f"space_grid.{zone}.rows 结构非法")
                    break
                if abs(float(a["y"]) - float(b.get("y", 1e9))) > 1e-6 or \
                        abs(float(a["rotation"]) - float(b.get("rotation", 1e9))) > 1e-6:
                    fatal(f"space_grid.{zone}.rows 与 rows.{zone} 坐标不一致")
        capacity = int(g.get("columns", 0)) * len(zone_rows)
        if capacity != 20:
            warn(f"{zone} 区网格容量 {capacity} != 20（非默认 80 车位，允许自定义）")
    # 分区端口节点必须存在
    zone_ports = raw.get("zone_ports") or {}
    for zone in "ABCD":
        ports = zone_ports.get(zone) or {}
        for key in ("entry", "exit"):
            nid = ports.get(key)
            if nid and nid not in node_ids:
                fatal(f"zone_ports.{zone}.{key} 节点不存在: {nid}")
    # 关键节点必须存在（入口/出口链路）
    for nid in ("IN_APRON", "IN_STOP", "IN_GATE", "X_STOP", "X_GATE", "X_OUT"):
        if node_ids and nid not in node_ids:
            fatal(f"缺少关键节点 {nid}")

    # 8) 支路/排队/走廊
    aisles = raw.get("aisles") or {}
    if not isinstance(aisles, Mapping) or not _is_num(aisles.get("width")) \
            or float(aisles.get("width", 0)) <= 0:
        fatal("aisles.width 缺失或非正")
    queue = raw.get("queue") or {}
    for which in ("entry", "exit"):
        q = queue.get(which) if isinstance(queue, Mapping) else None
        if not isinstance(q, Mapping) or not _is_num(q.get("length_m")) \
                or float(q["length_m"]) <= 0:
            fatal(f"queue.{which}.length_m 缺失或非正")
    corridor = raw.get("corridor") or {}
    for key in ("in_x", "out_x", "median_half"):
        if isinstance(corridor, Mapping) and not _is_num(corridor.get(key)):
            fatal(f"corridor.{key} 非数值")

    # 9) 非致命提示
    if not isinstance(raw.get("features"), Mapping):
        warn("缺少 features（静态壳/程序化车位线/预约开关），前端将使用默认值")
    if not isinstance(raw.get("props"), Mapping):
        warn("缺少 props（岗亭/相机/导视等），使用旧常量回退")
    if not isinstance(raw.get("markings"), Mapping):
        warn("缺少 markings（标线尺寸），使用默认值")

    ok = not any(i.startswith("[FATAL]") for i in issues)
    return ok, issues


def _build_manifest(raw: Mapping[str, Any], path: Path,
                    issues: list[str]) -> Manifest:
    rows: dict[str, tuple[RowSpec, ...]] = {}
    for zone, items in (raw.get("rows") or {}).items():
        if not isinstance(items, list):
            continue
        rows[str(zone)] = tuple(
            RowSpec(y=float(r["y"]), rotation=float(r["rotation"]),
                    side=str(r.get("side", "")))
            for r in items if isinstance(r, Mapping) and _is_num(r.get("y"))
            and _is_num(r.get("rotation")))
    space_grid: dict[str, SpaceGridSpec] = {}
    for zone, g in (raw.get("space_grid") or {}).items():
        if not isinstance(g, Mapping):
            continue
        grows = []
        for r in g.get("rows") or []:
            if isinstance(r, Mapping) and _is_num(r.get("y")) and _is_num(r.get("rotation")):
                grows.append(RowSpec(y=float(r["y"]), rotation=float(r["rotation"])))
        space_grid[str(zone)] = SpaceGridSpec(
            zone=str(zone), x_start=float(g.get("x_start", 0.0)),
            pitch=float(g.get("pitch", 3.0)), columns=int(g.get("columns", 10)),
            rows=tuple(grows))
    nodes: dict[str, tuple[float, float]] = {}
    for n in raw.get("nodes") or []:
        if isinstance(n, Mapping) and "id" in n:
            nodes[str(n["id"])] = (float(n.get("x", 0.0)), float(n.get("y", 0.0)))
    edges = tuple(dict(e) for e in (raw.get("edges") or []) if isinstance(e, Mapping))
    zones = tuple(dict(z) for z in (raw.get("conflict_zones") or [])
                  if isinstance(z, Mapping))
    return Manifest(
        path=path,
        raw=dict(raw),
        manifest_version=int(raw.get("manifest_version", 0)),
        road_graph_version=int(raw.get("road_graph_version", 0)),
        manifest_hash=str(raw.get("manifest_hash", "")),
        coordinate_system=str(raw.get("coordinate_system", "")),
        lot=dict(raw.get("lot") or {}),
        outer_road=dict(raw.get("outer_road") or {}),
        apron=dict(raw.get("apron") or {}),
        corridor=dict(raw.get("corridor") or {}),
        gates=dict(raw.get("gates") or {}),
        aisles=dict(raw.get("aisles") or {}),
        plazas=dict(raw.get("plazas") or {}),
        rows=rows,
        space_grid=space_grid,
        zone_lanes=dict(raw.get("zone_lanes") or {}),
        zone_ports=dict(raw.get("zone_ports") or {}),
        aisle_nodes=dict(raw.get("aisle_nodes") or {}),
        queue=dict(raw.get("queue") or {}),
        conflict_zones=zones,
        nodes=nodes,
        edges=edges,
        props=dict(raw.get("props") or {}),
        palette=dict(raw.get("palette") or {}),
        markings=dict(raw.get("markings") or {}),
        features=dict(raw.get("features") or {}),
        issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# 缓存加载
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[str, float, int], Manifest] = {}
_WARNED: set[str] = set()
_LOCK = threading.Lock()


def _load_from_path(path: Path) -> Manifest:
    """读取 + 校验 + 构造 Manifest；失败抛 ManifestError（含全部问题）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"读取/解析 manifest 失败: {path} ({exc})") from exc
    if not isinstance(raw, Mapping):
        raise ManifestError(f"manifest 顶层必须是 JSON 对象: {path}")
    ok, issues = validate_manifest(raw, path)
    if not ok:
        fatal = [i for i in issues if i.startswith("[FATAL]")]
        raise ManifestError(
            f"manifest 校验失败（{path}）：" + "；".join(fatal[:5])
            + (f"；…共 {len(fatal)} 项" if len(fatal) > 5 else ""))
    warns = tuple(i for i in issues if not i.startswith("[FATAL]"))
    return _build_manifest(raw, path, list(warns))


def load_manifest(path: Optional[str | Path] = None,
                  cfg: Optional[AppConfig] = None, *,
                  strict: bool = False,
                  refresh: bool = False) -> Optional[Manifest]:
    """加载 manifest；缺失/非法时返回 None 并告警（除非 ``strict=True`` 抛异常）。

    ``path`` 为 None 时按 :func:`resolve_manifest_path` 解析。结果按
    ``(路径, mtime, size)`` 缓存，文件更新后自动失效。
    """
    resolved = resolve_manifest_path(cfg, path)
    if resolved is None:
        msg = ("未找到布局 manifest（assets_3d/layout/lot_v2.json 或 "
               "PARKING_LAYOUT_MANIFEST），将回退旧版布局常量")
        if strict:
            raise ManifestError(msg)
        _warn_once(f"missing::{path}", msg)
        return None
    try:
        stat = resolved.stat()
    except OSError as exc:  # pragma: no cover
        if strict:
            raise ManifestError(str(exc)) from exc
        _warn_once(f"stat::{resolved}", f"manifest 不可读: {resolved} ({exc})；回退旧常量")
        return None
    key = (str(resolved), float(stat.st_mtime), int(stat.st_size))
    with _LOCK:
        cached = None if refresh else _CACHE.get(key)
        if cached is not None:
            return cached
    try:
        manifest = _load_from_path(resolved)
    except ManifestError as exc:
        if strict:
            raise
        _warn_once(f"invalid::{resolved}::{stat.st_mtime}",
                   f"{exc}；将回退旧版布局常量（旧坐标：A 25/14，B -14/-25，支路 5 m）")
        return None
    for item in manifest.issues:
        log.warning("布局 manifest 警告: %s", item)
    with _LOCK:
        # 仅保留当前文件版本的缓存项，避免无界增长
        for old_key in [k for k in _CACHE if k[0] == str(resolved) and k != key]:
            _CACHE.pop(old_key, None)
        _CACHE[key] = manifest
    if manifest.issues:
        log.info("布局 manifest 已加载（含 %d 条警告）：%s", len(manifest.issues),
                 path_text(manifest.path))
    else:
        log.info("布局 manifest 已加载：%s（v%s, hash=%s, nodes=%d, edges=%d）",
                 path_text(manifest.path), manifest.manifest_version,
                 manifest.manifest_hash, len(manifest.nodes), len(manifest.edges))
    return manifest


def get_manifest(cfg: Optional[AppConfig] = None, *, refresh: bool = False) -> Optional[Manifest]:
    """返回全局 manifest（缓存）；不可用时返回 None（调用方回退旧常量）。"""
    return load_manifest(None, cfg, refresh=refresh)


def _warn_once(key: str, message: str) -> None:
    with _LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    log.warning(message)


def clear_cache() -> None:
    """清空 manifest 缓存（测试/热更新用）。"""
    with _LOCK:
        _CACHE.clear()
        _WARNED.clear()


# ---------------------------------------------------------------------------
# 场景值映射（manifest → /api/layout 的 scene/gates）
# ---------------------------------------------------------------------------
def manifest_scene_values(m: Manifest) -> dict:
    """由 manifest 派生的、``/api/layout.scene`` 权威值（manifest 为准）。

    既包含 route-v2 新键（``aisle_w/aisle_n/aisle_s/median_half/plaza/stop_paint/
    gate_out_y/road_graph_version/manifest_hash`` 等），也覆盖旧键（``lot_x/top_y/
    bot_y/entry_gate_x/exit_gate_x/gate_y/outer_road_y/stop_line_*``），保证新旧前端
    都能只读 ``scene`` 拿到正确几何。
    """
    lot = m.lot or {}
    corridor = m.corridor or {}
    aisle_n = float((m.aisles or {}).get("n_y", 0.0))
    aisle_s = float((m.aisles or {}).get("s_y", 0.0))
    plaza_n = m.plaza("n")
    plaza_s = m.plaza("s")
    apron = m.apron or {}
    return {
        # ---------- 旧键（保留兼容，值更新为 v2）----------
        "lot_x": float(lot.get("x_max", 36.0)),
        "top_y": float(lot.get("y_max", 29.5)),
        "bot_y": float(lot.get("y_min", -30.0)),
        "main_road_half": float((m.raw.get("scene") or {}).get("main_road_half", 3.0))
        if isinstance(m.raw.get("scene"), Mapping) else 3.0,
        "entry_gate_x": float(m.gate("in").get("x", -3.5)),
        "exit_gate_x": float(m.gate("out").get("x", 3.5)),
        "gate_y": float(m.gate("in").get("y", -25.5)),
        "outer_road_y": float((m.outer_road or {}).get("y", LEGACY_OUTER_ROAD_Y)),
        "stop_line_entry_y": m.stop_paint_y("in"),
        "stop_line_exit_y": m.stop_paint_y("out"),
        # ---------- route-v2 新键（与 web/sim3d/js/config.js SCENE_DEFAULT 对齐）----------
        "lot_x_min": float(lot.get("x_min", -36.0)),
        "lot_x_max": float(lot.get("x_max", 36.0)),
        "lot_y_min": float(lot.get("y_min", -30.0)),
        "lot_y_max": float(lot.get("y_max", 29.5)),
        "apron_y_min": float(apron.get("y_min", -45.5)),
        "apron_y_max": float(apron.get("y_max", -30.0)),
        "gate_out_y": float(m.gate("out").get("y", -34.0)),
        "stop_entry_center_y": m.stop_center_y("in"),
        "stop_exit_center_y": m.stop_center_y("out"),
        "stop_paint": {"in": m.stop_paint_y("in"), "out": m.stop_paint_y("out")},
        "aisle_w": m.aisle_width,
        "aisle_n": aisle_n,
        "aisle_s": aisle_s,
        "median_half": float(corridor.get("median_half", 1.75)),
        "corridor_in_x": float(corridor.get("in_x", -3.5)),
        "corridor_out_x": float(corridor.get("out_x", 3.5)),
        "corridor_lane_w": float(corridor.get("lane_w", 3.5)),
        "lane_n_eb": m.lane("n", "eb"),
        "lane_n_wb": m.lane("n", "wb"),
        "lane_s_eb": m.lane("s", "eb"),
        "lane_s_wb": m.lane("s", "wb"),
        "plaza": {
            "n": dict(plaza_n),
            "s": dict(plaza_s),
        },
        # 与前端 SCENE_DEFAULT 同名（CameraRig/ProceduralGround 直接读 plaza_n/plaza_s）
        "plaza_n": dict(plaza_n),
        "plaza_s": dict(plaza_s),
        "plaza_n_x": float(plaza_n.get("x", 0.0)),
        "plaza_n_y": float(plaza_n.get("y", aisle_n)),
        "plaza_n_w": float(plaza_n.get("w", 12.0)),
        "plaza_n_h": float(plaza_n.get("h", m.aisle_width)),
        "plaza_s_x": float(plaza_s.get("x", 0.0)),
        "plaza_s_y": float(plaza_s.get("y", aisle_s)),
        "plaza_s_w": float(plaza_s.get("w", 12.0)),
        "plaza_s_h": float(plaza_s.get("h", m.aisle_width)),
        "queue_entry_length_m": m.queue_length("entry"),
        "queue_exit_length_m": m.queue_length("exit"),
        "road_graph_version": m.road_graph_version,
        "manifest_version": m.manifest_version,
        "manifest_hash": m.manifest_hash,
        "manifest_path": path_text(m.path),
        "assets3d_url": "/assets3d",
        "shell_glb": str((m.features or {}).get("static_shell", "lot_shell.glb")),
        "layout_source": "manifest",
    }


def manifest_gates_values(m: Manifest) -> dict:
    """``/api/layout.gates`` 的权威值（保持旧结构，只增字段）。"""
    booth = (m.props or {}).get("booth") or LEGACY_BOOTH
    gate_in, gate_out = m.gate("in"), m.gate("out")
    return {
        "entry": {
            "id": "in",
            "x": float(gate_in.get("x", -3.5)),
            "y": float(gate_in.get("y", -25.5)),
            "stop_line_y": m.stop_paint_y("in"),
            "stop_paint_y": m.stop_paint_y("in"),
            "stop_center_y": m.stop_center_y("in"),
            "stop_center": m.stop_center_y("in"),
            "stop_clearance_m": float(gate_in.get("stop_clearance_m", 0.3) or 0.3),
        },
        "exit": {
            "id": "out",
            "x": float(gate_out.get("x", 3.5)),
            "y": float(gate_out.get("y", -34.0)),
            "stop_line_y": m.stop_paint_y("out"),
            "stop_paint_y": m.stop_paint_y("out"),
            "stop_center_y": m.stop_center_y("out"),
            "stop_center": m.stop_center_y("out"),
            "stop_clearance_m": float(gate_out.get("stop_clearance_m", 0.3) or 0.3),
        },
        "booth": {
            "x": float((booth or {}).get("x", 0.0)),
            "y": float((booth or {}).get("y", -27.5)),
        },
        "outer_road_y": float((m.outer_road or {}).get("y", LEGACY_OUTER_ROAD_Y)),
    }


def legacy_scene_values(scene_cfg: Optional[Mapping[str, Any]] = None) -> dict:
    """manifest 缺失时的回退场景值（以 config.scene 旧值为准）。"""
    s = dict(scene_cfg or {})
    lot_x = float(s.get("lot_x", 36.0))
    top_y = float(s.get("top_y", 29.5))
    bot_y = float(s.get("bot_y", -30.0))
    plaza_cfg = s.get("plaza") if isinstance(s.get("plaza"), Mapping) else {}
    legacy_plaza_n = dict((plaza_cfg or {}).get("n") or {"x": 0.0, "y": 19.5, "w": 12.0, "h": 6.5})
    legacy_plaza_s = dict((plaza_cfg or {}).get("s") or {"x": 0.0, "y": -13.5, "w": 12.0, "h": 6.5})
    return {
        "lot_x": lot_x,
        "top_y": top_y,
        "bot_y": bot_y,
        "lot_x_min": float(s.get("lot_x_min", -lot_x)),
        "lot_x_max": float(s.get("lot_x_max", lot_x)),
        "lot_y_min": float(s.get("lot_y_min", bot_y)),
        "lot_y_max": float(s.get("lot_y_max", top_y)),
        "apron_y_min": float(s.get("apron_y_min", -45.5)),
        "apron_y_max": float(s.get("apron_y_max", -30.0)),
        "main_road_half": float(s.get("main_road_half", 3.0)),
        "entry_gate_x": float(s.get("entry_gate_x", -3.5)),
        "exit_gate_x": float(s.get("exit_gate_x", 3.5)),
        "gate_y": float(s.get("gate_y", -25.5)),
        "outer_road_y": float(s.get("outer_road_y", LEGACY_OUTER_ROAD_Y)),
        "stop_line_entry_y": float(s.get("stop_line_entry_y", -27.5)),
        "stop_line_exit_y": float(s.get("stop_line_exit_y", -23.5)),
        "stop_entry_center_y": float(s.get("stop_entry_center_y", -28.4)),
        "stop_exit_center_y": float(s.get("stop_exit_center_y", -31.1)),
        "gate_out_y": float(s.get("gate_out_y", -25.5)),
        "stop_paint": {
            "in": float(s.get("stop_line_entry_y", -27.5)),
            "out": float(s.get("stop_line_exit_y", -23.5)),
        },
        "aisle_w": float(s.get("aisle_w", LEGACY_AISLE_W)),
        "aisle_n": float(s.get("aisle_n", LEGACY_AISLE_N)),
        "aisle_s": float(s.get("aisle_s", LEGACY_AISLE_S)),
        "median_half": float(s.get("median_half", 1.75)),
        "lane_n_eb": s.get("lane_n_eb"), "lane_n_wb": s.get("lane_n_wb"),
        "lane_s_eb": s.get("lane_s_eb"), "lane_s_wb": s.get("lane_s_wb"),
        "plaza": {"n": legacy_plaza_n, "s": legacy_plaza_s},
        "plaza_n": legacy_plaza_n,
        "plaza_s": legacy_plaza_s,
        "road_graph_version": int(s.get("road_graph_version", 1) or 1),
        "layout_source": "legacy",
    }


def legacy_gates_values(scene_cfg: Optional[Mapping[str, Any]] = None) -> dict:
    """manifest 缺失时的回退 gates 值（旧版两块闸都在 gate_y）。"""
    s = dict(scene_cfg or {})
    gate_y = float(s.get("gate_y", -25.5))
    clearance = 0.3
    return {
        "entry": {"id": "in", "x": float(s.get("entry_gate_x", -3.5)), "y": gate_y,
                  "stop_line_y": float(s.get("stop_line_entry_y", -27.5)),
                  "stop_paint_y": float(s.get("stop_line_entry_y", -27.5)),
                  "stop_center_y": round(gate_y - clearance - 2.6, 6),
                  "stop_center": round(gate_y - clearance - 2.6, 6),
                  "stop_clearance_m": clearance},
        "exit": {"id": "out", "x": float(s.get("exit_gate_x", 3.5)), "y": gate_y,
                 "stop_line_y": float(s.get("stop_line_exit_y", -23.5)),
                 "stop_paint_y": float(s.get("stop_line_exit_y", -23.5)),
                 "stop_center_y": round(gate_y + clearance + 2.6, 6),
                 "stop_center": round(gate_y + clearance + 2.6, 6),
                 "stop_clearance_m": clearance},
        "booth": dict(LEGACY_BOOTH),
        "outer_road_y": float(s.get("outer_road_y", LEGACY_OUTER_ROAD_Y)),
    }


# ---------------------------------------------------------------------------
# 配置一致性校验（启动告警；manifest 为准）
# ---------------------------------------------------------------------------
#: config.scene 中会被 manifest 覆盖的键（用于启动时提示“配置与 manifest 不一致”）。
SCENE_AUTHORITATIVE_KEYS = (
    "lot_x", "top_y", "bot_y", "entry_gate_x", "exit_gate_x", "gate_y",
    "outer_road_y", "stop_line_entry_y", "stop_line_exit_y", "gate_out_y",
    "stop_entry_center_y", "stop_exit_center_y", "aisle_w", "aisle_n",
    "aisle_s", "median_half", "lane_n_eb", "lane_n_wb", "lane_s_eb",
    "lane_s_wb", "road_graph_version",
)


def scene_config_issues(cfg: Optional[AppConfig], m: Manifest) -> list[str]:
    """比较 config.scene 与 manifest 派生值，返回不一致项（manifest 为准）。"""
    if cfg is None or m is None:
        return []
    scene = getattr(cfg, "scene", None) or {}
    if not isinstance(scene, Mapping):
        return []
    expected = manifest_scene_values(m)
    issues: list[str] = []
    for key in SCENE_AUTHORITATIVE_KEYS:
        if key not in scene or key not in expected:
            continue
        want, got = expected[key], scene[key]
        if want is None or got is None or isinstance(want, (dict, list)):
            continue
        try:
            same = abs(float(got) - float(want)) <= 1e-6
        except (TypeError, ValueError):
            same = str(got) == str(want)
        if not same:
            issues.append(f"scene.{key} 配置值 {got!r} != manifest 值 {want!r}")
    return issues
