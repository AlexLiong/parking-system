// 冲突区定义与几何换算（sim/ConflictZones.js）。
//
// 纯逻辑模块：**不 import three**，可在 node --test 下直接单测。
// 数据源：assets_3d/layout/lot_v2.json（manifest，单一事实源，禁止手改）。
// 坐标：manifest 全部为 Blender (x 东, y 北)；对外几何一律换算到 three (x, z=-y)。
//
// 冲突区清单（manifest.conflict_zones）：
//   Z1 入口杆区 / Z2 出口杆区 / Z3 北分岔区-西 / Z4 北分岔区-东
//   Z5 南分岔区-西 / Z6 南分岔区-东 / Z7 倒车入库区（动态，每车位一个实例 Z7:<spaceNo>）
//
// 用法：
//   import { buildConflictZones } from './ConflictZones.js';
//   const cz = buildConflictZones(manifest);
//   cz.rectThree('Z1')            // -> {xmin,zmin,xmax,zmax}  （three 坐标）
//   cz.zonesForLeg(leg)           // -> ['Z3']                 （几何命中，仅静态区）
//   cz.stallZone(space)           // -> {id:'Z7:A-01', rect:[xmin,ymin,xmax,ymax](Blender)}

const EPS = 1e-9;

/** manifest 冲突区固定顺序（Z7 为动态模板，实例 id 形如 `Z7:A-01`）。 */
export const ZONE_ORDER = Object.freeze(['Z1', 'Z2', 'Z3', 'Z4', 'Z5', 'Z6', 'Z7']);

/** 动态车位区前缀。 */
export const STALL_ZONE_PREFIX = 'Z7:';

/**
 * 派生冲突区（manifest 未声明、由几何分析补出的"分岔区中央"互斥区）。
 *
 * 原因：manifest 把北/南分岔区拆成 Z3/Z4（x∈[-6,-1.75] 与 [1.75,6]），中央隔离带
 * x∈(-1.75,1.75) 不在任何区内；而"入口→C（或 D）"与"A（或 B）出口→出口车道"两条
 * 转向轨迹恰好在该中央带相交（实测交点 (0, 16.849) 与 (0, -16.151)，Blender），
 * 若只按 manifest 的 edge.zones 预约，两车可同时进入而实际相撞。
 *
 * ── 派生规则（Lead 已采纳：**不改 manifest**；manifest 仍保持 7 区 Z1..Z7 为设计基准）──
 *   ZN = 北分岔区中央互斥区，rect [-1.75, 16.25, 1.75, 22.75]（Blender: xmin,ymin,xmax,ymax）
 *   ZS = 南分岔区中央互斥区，rect [-1.75, -16.75, 1.75, -10.25]
 *   hold_s = 6（与 Z3..Z6 分岔区一致）；capacity = 1；不属于 manifest，`derived:true`。
 *
 * ── "直行不占"判定（关键语义）──
 *   派生区**按腿（leg）逐段**判定：把每条 leg 采样成折线，用线段-矩形裁剪
 *   （Liang–Barsky，见 `segHitsBox`）判断它是否与 ZN/ZS 矩形相交；只有相交才把该区
 *   加入这条腿对应的冲突区序列（见 `derivedZonesForLeg`）。因此：
 *     - 中央走廊的直行车道在 x=∓3.5，落在中央带 x∈(-1.75,1.75) 之外 ⇒ 直行腿**永不命中**；
 *     - 只有"入口→C/D"与"A/B 出口→出口车道"这类**横跨中央带**的转向腿才命中。
 *   所以直行穿过分岔区的走廊车不占用 ZN/ZS，不会把南北分岔区过度串行化。
 *   统计佐证（80 车位全量、每路径计一次）：ZN 命中 40 条（20 条 C 入场 + 20 条 A 出场）、
 *   ZS 命中 40 条（20 条 D 入场 + 20 条 B 出场），直行走廊腿命中 0；
 *   route.test.js 的"派生区 ZN/ZS：仅转向轨迹穿越中央带时占用（直行不占）"用例锁定该语义。
 *
 * 关闭方式：manifest.features.derived_plaza_zones === false（默认开启）。
 * 后续若 D/E 反馈预约过粗，再评估是否写回 manifest 的 conflict_zones。
 */
export const DERIVED_ZONES = Object.freeze([
  {
    id: 'ZN', name: '北分岔区-中央', kind: 'mutex(1)',
    rect: [-1.75, 16.25, 1.75, 22.75], hold_s: 6, derived: true,
    note: '派生区：入口→C 与 A 出口转向轨迹在中央带的交点互斥（直行不占）',
  },
  {
    id: 'ZS', name: '南分岔区-中央', kind: 'mutex(1)',
    rect: [-1.75, -16.75, 1.75, -10.25], hold_s: 6, derived: true,
    note: '派生区：入口→D 与 B 出口转向轨迹在中央带的交点互斥（直行不占）',
  },
]);

/** Blender x → three x（同值，语义标注用）。 */
export const toThreeX = (x) => x;
/** Blender y → three z（three_z = -Blender_y）。 */
export const toThreeZ = (y) => -y;
/** three z → Blender y。 */
export const toBlenderY = (z) => -z;

/** 点（Blender）→ {x,z}（three）。 */
export const blPointToThree = (x, y) => ({ x: toThreeX(x), z: toThreeZ(y) });

// ---------------------------------------------------------------------------
// 腿（Leg）采样：把 line / arc / bezier 统一成折线 [{x,z}, ...]（three 坐标）
// ---------------------------------------------------------------------------

const BEZIER_SAMPLES = 20;
const ARC_SAMPLES = 24;

/**
 * 把一条 leg 采样为 three 坐标折线。
 * @param {{kind:string}} leg
 * @param {{bezier?:number, arc?:number}} [opts] 采样段数
 * @returns {Array<{x:number,z:number}>}
 */
export function legPolyline(leg, opts = {}) {
  if (!leg || typeof leg !== 'object') return [];
  const kind = leg.kind || (leg.p0 ? 'bezier' : (leg.r != null ? 'arc' : 'line'));
  if (kind === 'line') {
    const a = { ax: num(leg.ax), az: num(leg.az), bx: num(leg.bx), bz: num(leg.bz) };
    return [{ x: a.ax, z: a.az }, { x: a.bx, z: a.bz }];
  }
  if (kind === 'arc') {
    const n = Math.max(2, opts.arc ?? ARC_SAMPLES);
    const cx = num(leg.cx), cz = num(leg.cz), r = num(leg.r);
    const a0 = num(leg.a0), a1 = num(leg.a1);
    const out = [];
    for (let i = 0; i <= n; i++) {
      const a = a0 + (a1 - a0) * (i / n);
      out.push({ x: cx + r * Math.cos(a), z: cz + r * Math.sin(a) });
    }
    return out;
  }
  if (kind === 'bezier') {
    const n = Math.max(2, opts.bezier ?? BEZIER_SAMPLES);
    const { p0, p1, p2, p3 } = leg;
    const out = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n, u = 1 - t;
      const b0 = u * u * u, b1 = 3 * u * u * t, b2 = 3 * u * t * t, b3 = t * t * t;
      out.push({
        x: b0 * p0.x + b1 * p1.x + b2 * p2.x + b3 * p3.x,
        z: b0 * p0.z + b1 * p1.z + b2 * p2.z + b3 * p3.z,
      });
    }
    return out;
  }
  return [];
}

/** 单腿长度（line 精确；arc 解析；bezier 采样）。 */
export function legLength(leg) {
  if (!leg) return 0;
  const kind = leg.kind || (leg.p0 ? 'bezier' : (leg.r != null ? 'arc' : 'line'));
  if (kind === 'line') return Math.hypot(num(leg.bx) - num(leg.ax), num(leg.bz) - num(leg.az));
  if (kind === 'arc') return Math.abs(num(leg.r) * (num(leg.a1) - num(leg.a0)));
  if (kind === 'bezier') return polylineLength(legPolyline(leg, { bezier: 32 }));
  return 0;
}

/** 折线长度。 */
export function polylineLength(pts) {
  let s = 0;
  for (let i = 1; i < pts.length; i++) s += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].z - pts[i - 1].z);
  return s;
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

// ---------------------------------------------------------------------------
// 矩形（Blender 轴对齐）与线段裁剪
// ---------------------------------------------------------------------------

/**
 * @typedef {[number,number,number,number]} RectBl rect = [xmin, ymin, xmax, ymax]（Blender）
 */

/**
 * 矩形（Blender）→ three 轴对齐盒 {xmin,zmin,xmax,zmax}（z=-y 会交换 ymin/ymax）。
 * @param {RectBl} rect
 */
export function rectBlToThree(rect) {
  if (!Array.isArray(rect) || rect.length !== 4) return null;
  const [xmin, ymin, xmax, ymax] = rect;
  return {
    xmin: Math.min(xmin, xmax),
    xmax: Math.max(xmin, xmax),
    zmin: -Math.max(ymin, ymax),
    zmax: -Math.min(ymin, ymax),
  };
}

/**
 * 线段与轴对齐矩形是否相交（Liang–Barsky 裁剪；退化线段按点判定）。
 * @param {number} ax @param {number} az @param {number} bx @param {number} bz
 * @param {{xmin:number,zmin:number,xmax:number,zmax:number}} box
 * @returns {boolean}
 */
export function segHitsBox(ax, az, bx, bz, box) {
  if (!box) return false;
  const dx = bx - ax, dz = bz - az;
  // 完全在矩形外（快速排除）
  if (Math.max(ax, bx) < box.xmin - EPS || Math.min(ax, bx) > box.xmax + EPS) return false;
  if (Math.max(az, bz) < box.zmin - EPS || Math.min(az, bz) > box.zmax + EPS) return false;
  if (Math.abs(dx) < EPS && Math.abs(dz) < EPS) {
    return ax >= box.xmin - EPS && ax <= box.xmax + EPS &&
      az >= box.zmin - EPS && az <= box.zmax + EPS;
  }
  // Liang–Barsky
  let t0 = 0, t1 = 1;
  const p = [-dx, dx, -dz, dz];
  const q = [ax - box.xmin, box.xmax - ax, az - box.zmin, box.zmax - az];
  for (let i = 0; i < 4; i++) {
    if (Math.abs(p[i]) < EPS) {
      if (q[i] < -EPS) return false;
      continue;
    }
    const r = q[i] / p[i];
    if (p[i] < 0) {
      if (r > t1) return false;
      if (r > t0) t0 = r;
    } else {
      if (r < t0) return false;
      if (r < t1) t1 = r;
    }
  }
  return t1 - t0 > -EPS;
}

/** 折线是否命中矩形。 */
export function polylineHitsBox(pts, box) {
  if (!box || pts.length === 0) return false;
  if (pts.length === 1) return segHitsBox(pts[0].x, pts[0].z, pts[0].x, pts[0].z, box);
  for (let i = 1; i < pts.length; i++) {
    if (segHitsBox(pts[i - 1].x, pts[i - 1].z, pts[i].x, pts[i].z, box)) return true;
  }
  return false;
}

/** 点（Blender）是否在矩形内。 */
export function pointInRectBl(x, y, rect) {
  if (!Array.isArray(rect) || rect.length !== 4) return false;
  const [xmin, ymin, xmax, ymax] = rect;
  return x >= Math.min(xmin, xmax) - EPS && x <= Math.max(xmin, xmax) + EPS &&
    y >= Math.min(ymin, ymax) - EPS && y <= Math.max(ymin, ymax) + EPS;
}

// ---------------------------------------------------------------------------
// 构建
// ---------------------------------------------------------------------------

/**
 * @param {object} manifest assets_3d/layout/lot_v2.json
 * @returns {{
 *   zones: Map<string, object>,
 *   order: string[],
 *   rect: (id:string)=>RectBl|null,
 *   rectThree: (id:string)=>{xmin,zmin,xmax,zmax}|null,
 *   holdSec: (id:string)=>number,
 *   zonesForLeg: (leg:object)=>string[],
 *   zonesForPolyline: (pts:Array<{x:number,z:number}>)=>string[],
 *   zoneForPointBl: (x:number,y:number)=>string[],
 *   stallZone: (space:object)=>{id:string, rect:RectBl, hold_s:number, spaceNo:string},
 *   stallRect: (space:object)=>RectBl,
 *   resolve: (id:string)=>object|undefined,
 * }}
 */
export function buildConflictZones(manifest) {
  const m = manifest || {};
  const rawZones = Array.isArray(m.conflict_zones) ? m.conflict_zones : [];
  const zones = new Map();
  for (const z of rawZones) {
    const rect = Array.isArray(z.rect) && z.rect.length === 4 ? z.rect.slice() : null;
    zones.set(z.id, {
      id: z.id,
      name: z.name ?? z.id,
      kind: z.kind ?? 'mutex(1)',
      rect,
      hold_s: Number.isFinite(z.hold_s) ? z.hold_s : 5,
      note: z.note ?? '',
      dynamic: rect === null,
      capacity: 1,
    });
  }

  const aisles = m.aisles || {};
  const aisleY = { A: aisles.n_y, C: aisles.n_y, B: aisles.s_y, D: aisles.s_y };
  const aisleHalf = (Number(aisles.width) || 6.5) / 2;
  const markings = m.markings || {};
  const spaceW = Number(markings.space_w) || 3.0;
  const spaceD = Number(markings.space_d) || 6.0;
  const neighborPad = 1.5;   // Z7 邻位互斥扩展（见报告 §8.7.1：真实后退入库后角侵入 1.06~1.58 m）

  // 静态区（Z1..Z6）的 three 盒缓存
  const staticBoxes = new Map();
  for (const [id, z] of zones) {
    if (z.rect) staticBoxes.set(id, rectBlToThree(z.rect));
  }
  // 派生区（ZN/ZS）：是否启用可通过 manifest.features 关闭
  const features = m.features || {};
  const useDerived = features.derived_plaza_zones !== false;
  const derivedIds = [];
  if (useDerived) {
    for (const dz of DERIVED_ZONES) {
      if (zones.has(dz.id)) continue;           // manifest 已声明则以其为准
      zones.set(dz.id, {
        id: dz.id, name: dz.name, kind: dz.kind, rect: dz.rect.slice(),
        hold_s: dz.hold_s, note: dz.note, dynamic: false, capacity: 1, derived: true,
      });
      staticBoxes.set(dz.id, rectBlToThree(dz.rect));
      derivedIds.push(dz.id);
    }
  }
  const derivedSet = new Set(derivedIds);
  const staticOrder = [...ZONE_ORDER.filter((id) => staticBoxes.has(id)), ...derivedIds];

  /** 取车位中心的 Blender y（spaces 输入约定：z = Blender y）。 */
  function spaceBlY(space) {
    const y = Number(space && (space.z ?? space.y));
    return Number.isFinite(y) ? y : NaN;
  }
  function spaceBlX(space) {
    const x = Number(space && space.x);
    return Number.isFinite(x) ? x : NaN;
  }

  /**
   * 车位倒车入库包络矩形（Blender）：
   *   x ∈ 车位中心 ± (车位宽/2 + 邻位 1.5 m)
   *   y ∈ [min(车位后线, 支路外缘), max(车位口线, 支路外缘)]  即"车位 + 支路侧机动带"
   */
  function stallRect(space) {
    const x = spaceBlX(space);
    const y = spaceBlY(space);
    const ay = aisleY[(space && space.zone) || ''] ?? 0;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    const halfW = spaceW / 2;
    const ymin = Math.min(y - spaceD / 2, ay - aisleHalf);
    const ymax = Math.max(y + spaceD / 2, ay + aisleHalf);
    return [
      round4(x - halfW - neighborPad), round4(ymin),
      round4(x + halfW + neighborPad), round4(ymax),
    ];
  }

  function stallZone(space) {
    const rect = stallRect(space);
    const no = (space && (space.space_no ?? space.no)) || 'unknown';
    return {
      id: STALL_ZONE_PREFIX + no,
      rect,
      hold_s: zones.get('Z7')?.hold_s ?? 4.5,
      spaceNo: no,
      dynamic: true,
      capacity: 1,
      neighbors: [neighborPad],
    };
  }

  function rect(id) {
    if (zones.has(id)) return zones.get(id).rect;
    if (id && id.startsWith(STALL_ZONE_PREFIX)) return stallRectById(id); // 动态区：按 spaceNo 反解
    return null;
  }

  function rectThree(id) {
    if (id && id.startsWith(STALL_ZONE_PREFIX)) {
      const r = stallRectById(id);
      return r ? rectBlToThree(r) : null;
    }
    return staticBoxes.get(id) || null;
  }

  /**
   * 由动态区 id（`Z7:<spaceNo>`）反解车位包络矩形（Blender）。
   * 不依赖外部传入的 spaces：用 manifest.space_grid（区内跨排连续编号）还原 x/y。
   * 用于 ReservationTable 做"相邻车位 Z7 矩形重叠 ⇒ 互斥"判定。
   * @param {string} zoneId 形如 'Z7:A-01'
   * @returns {RectBl|null}
   */
  function stallRectById(zoneId) {
    if (!zoneId || !String(zoneId).startsWith(STALL_ZONE_PREFIX)) return null;
    const no = String(zoneId).slice(STALL_ZONE_PREFIX.length);
    const m2 = /^([A-Za-z]+)-(\d+)$/.exec(no);
    if (!m2) return null;
    const zone = m2[1].toUpperCase();
    const idx = Number(m2[2]);
    const grid = (m.space_grid || {})[zone];
    if (!grid || !Number.isFinite(idx) || idx < 1) return null;
    const rows = grid.rows || [];
    const cols = Number(grid.columns) || 10;
    if (!rows.length) return null;
    const r = Math.floor((idx - 1) / cols);
    const c = (idx - 1) % cols;
    if (r >= rows.length) return null;
    const x = Number(grid.x_start) + (Number(grid.pitch) || 3) * (c + 0.5);
    const y = Number(rows[r].y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return stallRect({ zone, x, y });
  }

  function holdSec(id) {
    if (zones.has(id)) return zones.get(id).hold_s;
    if (id && id.startsWith(STALL_ZONE_PREFIX)) return zones.get('Z7')?.hold_s ?? 4.5;
    return 5;
  }

  /** 折线（three 坐标）命中的静态冲突区（按 ZONE_ORDER 排序）。 */
  function zonesForPolyline(pts) {
    const hit = [];
    for (const id of staticOrder) {
      const box = staticBoxes.get(id);
      if (polylineHitsBox(pts, box)) hit.push(id);
    }
    return hit;
  }

  /** 单条 leg 命中的静态冲突区。 */
  function zonesForLeg(leg) {
    return zonesForPolyline(legPolyline(leg));
  }

  /** 点（Blender）落在哪些静态区内。 */
  function zoneForPointBl(x, y) {
    const out = [];
    for (const id of staticOrder) {
      if (pointInRectBl(x, y, zones.get(id).rect)) out.push(id);
    }
    return out;
  }

  /** 折线命中的**派生**区（ZN/ZS）；直行穿过分岔区的走廊车不会命中（x 不在中央带）。 */
  function derivedZonesForPolyline(pts) {
    const hit = [];
    for (const id of derivedIds) {
      if (polylineHitsBox(pts, staticBoxes.get(id))) hit.push(id);
    }
    return hit;
  }

  /** 单条 leg 命中的派生区。 */
  function derivedZonesForLeg(leg) {
    return derivedZonesForPolyline(legPolyline(leg));
  }

  return {
    zones,
    order: ZONE_ORDER.slice(),
    staticOrder,
    derivedIds: derivedIds.slice(),
    isDerived: (id) => derivedSet.has(id),
    rect,
    rectThree,
    holdSec,
    zonesForLeg,
    zonesForPolyline,
    derivedZonesForLeg,
    derivedZonesForPolyline,
    zoneForPointBl,
    stallZone,
    stallRect,
    stallRectById,
    resolve: (id) => zones.get(id),
    neighborPad,
    spaceW,
    spaceD,
  };
}

function round4(v) {
  return Math.round(Number(v) * 1e4) / 1e4;
}

export const __internal = { EPS, round4 };
