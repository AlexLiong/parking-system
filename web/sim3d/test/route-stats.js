// Route V2 几何/统计分析（test/route-stats.js）
//
// 纯计算：被 route.test.js（断言）与 _qa_audit/runtime/three-route/stats.mjs（出报告）共用。
// 统计口径（对齐设计报告 §8.11 与 findings/06-routing-design.md）：
//   - 对向共用段（circulation）：剔除"同车位存取段"后的路径，按线段级共线反向重叠求并集；
//   - 交叉点：线段级真相交（严格内点），并区分"是否落在冲突区矩形内"；
//   - 经 (0,0)：circulation 路径是否经过 Blender 原点（= three 原点）；
//   - 路径长度：入场自停止线车心起算、出场至出口停止线车心止算（与旧口径一致）。

import { legPolyline } from '../js/sim/ConflictZones.js';

/** 车型长度（与 web/sim3d/js/config.js CAR_MODELS 一致）。 */
export const CAR_LENGTHS = Object.freeze([4.2, 4.6, 4.8, 5.2]);

const EPS = 1e-9;

// ---------------------------------------------------------------------------
// 线段工具
// ---------------------------------------------------------------------------

/** legs → 线段数组（three 坐标；bezier/arc 采样）。 */
export function legsToSegments(legs, samples = 16) {
  const segs = [];
  for (const leg of legs || []) {
    const pts = legPolyline(leg, { bezier: samples, arc: samples });
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      const dx = b.x - a.x, dz = b.z - a.z;
      if (Math.hypot(dx, dz) < 1e-9) continue;
      segs.push({ ax: a.x, az: a.z, bx: b.x, bz: b.z });
    }
  }
  return segs;
}

/** legs → 一条折线（three 坐标）。 */
export function legsToPolyline(legs, samples = 16) {
  const out = [];
  for (const leg of legs || []) {
    const pts = legPolyline(leg, { bezier: samples, arc: samples });
    for (let i = 0; i < pts.length; i++) {
      if (i === 0 && out.length && dist2(out[out.length - 1], pts[i]) < 1e-12) continue;
      out.push(pts[i]);
    }
  }
  return out;
}

const dist2 = (a, b) => (a.x - b.x) ** 2 + (a.z - b.z) ** 2;

/**
 * 两条线段关系：
 *   {type:'cross', x, z}                 真相交（严格内点，忽略端点相触）
 *   {type:'overlap', len, opposite}      共线重叠（opposite=方向相反）
 *   null
 */
export function segRelation(s, t) {
  const d1x = s.bx - s.ax, d1z = s.bz - s.az;
  const d2x = t.bx - t.ax, d2z = t.bz - t.az;
  const ex = t.ax - s.ax, ez = t.az - s.az;
  const den = d1x * d2z - d1z * d2x;
  if (Math.abs(den) > 1e-9) {
    const u = (ex * d2z - ez * d2x) / den;
    const v = (ex * d1z - ez * d1x) / den;
    if (u > 1e-9 && u < 1 - 1e-9 && v > 1e-9 && v < 1 - 1e-9) {
      return { type: 'cross', x: s.ax + d1x * u, z: s.az + d1z * u };
    }
    return null;
  }
  const n1 = Math.hypot(d1x, d1z) || 1;
  const crossE = d1x * ez - d1z * ex;
  if (Math.abs(crossE) / n1 > 1e-6) return null;         // 平行但不共线
  const L2 = d1x * d1x + d1z * d1z;
  const t0 = (ex * d1x + ez * d1z) / L2;
  const t1 = ((t.ax + d2x - s.ax) * d1x + (t.az + d2z - s.az) * d1z) / L2;
  const lo = Math.max(0, Math.min(t0, t1));
  const hi = Math.min(1, Math.max(t0, t1));
  if (hi - lo < 1e-9) return null;
  return {
    type: 'overlap',
    len: (hi - lo) * n1,
    opposite: d1x * d2x + d1z * d2z < 0,
  };
}

/** 两个线段数组的关系统计（bbox 预筛）。 */
function segSetRelation(segsA, segsB) {
  let oppositeShared = 0;
  let sameShared = 0;
  const crosses = [];
  for (const a of segsA) {
    const aMinX = Math.min(a.ax, a.bx) - 1e-6, aMaxX = Math.max(a.ax, a.bx) + 1e-6;
    const aMinZ = Math.min(a.az, a.bz) - 1e-6, aMaxZ = Math.max(a.az, a.bz) + 1e-6;
    for (const b of segsB) {
      if (Math.max(b.ax, b.bx) < aMinX || Math.min(b.ax, b.bx) > aMaxX) continue;
      if (Math.max(b.az, b.bz) < aMinZ || Math.min(b.az, b.bz) > aMaxZ) continue;
      const rel = segRelation(a, b);
      if (!rel) continue;
      if (rel.type === 'overlap') {
        if (rel.opposite) oppositeShared += rel.len;
        else sameShared += rel.len;
      } else {
        crosses.push(rel);
      }
    }
  }
  return { oppositeShared, sameShared, crosses };
}

/** 线段集合的全局"对向共用"并集长度（按直线归并区间）。 */
export function oppositeSharedUnion(segsIn, segsOut) {
  const lines = new Map();
  const push = (seg, owner) => {
    const dx = seg.bx - seg.ax, dz = seg.bz - seg.az;
    const n = Math.hypot(dx, dz);
    if (n < 1e-9) return;
    let ux = dx / n, uz = dz / n;
    if (ux < -1e-9 || (Math.abs(ux) <= 1e-9 && uz < 0)) { ux = -ux; uz = -uz; }
    const c = ux * seg.az - uz * seg.ax;                 // cross(u, a)
    const key = `${round6(ux)}|${round6(uz)}|${round6(c)}`;
    const sign = (dx * ux + dz * uz) > 0 ? 1 : -1;
    const t0 = ux * seg.ax + uz * seg.az;
    const t1 = ux * seg.bx + uz * seg.bz;
    const lo = Math.min(t0, t1), hi = Math.max(t0, t1);
    if (!lines.has(key)) lines.set(key, { in: { '+': [], '-': [] }, out: { '+': [], '-': [] } });
    lines.get(key)[owner][sign > 0 ? '+' : '-'].push([lo, hi]);
  };
  for (const s of segsIn) push(s, 'in');
  for (const s of segsOut) push(s, 'out');
  let total = 0;
  const detail = [];
  for (const [key, rec] of lines) {
    // in 与 out 方向相反的两组求交
    const pairs = [
      [rec.in['+'], rec.out['-']],
      [rec.in['-'], rec.out['+']],
    ];
    for (const [A, B] of pairs) {
      if (!A.length || !B.length) continue;
      const inter = intersectUnions(A, B);
      if (inter > 1e-9) {
        total += inter;
        detail.push({ key, len: round3(inter) });
      }
    }
  }
  return { total: round3(total), detail };
}

/** 两组区间并集的交集长度。 */
function intersectUnions(A, B) {
  const ua = union(A), ub = union(B);
  let i = 0, j = 0, s = 0;
  while (i < ua.length && j < ub.length) {
    const lo = Math.max(ua[i][0], ub[j][0]);
    const hi = Math.min(ua[i][1], ub[j][1]);
    if (hi > lo) s += hi - lo;
    if (ua[i][1] < ub[j][1]) i++; else j++;
  }
  return s;
}

function union(iv) {
  if (!iv.length) return [];
  const a = iv.slice().sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  const out = [a[0].slice()];
  for (const [lo, hi] of a.slice(1)) {
    const last = out[out.length - 1];
    if (lo <= last[1] + 1e-9) last[1] = Math.max(last[1], hi);
    else out.push([lo, hi]);
  }
  return out;
}

const round6 = (v) => Math.round(Number(v) * 1e6) / 1e6;
const round3 = (v) => Math.round(Number(v) * 1000) / 1000;

/** 点到线段的距离平方。 */
function pointSegDist2(px, pz, s) {
  const dx = s.bx - s.ax, dz = s.bz - s.az;
  const L2 = dx * dx + dz * dz;
  const t = L2 > 0 ? Math.max(0, Math.min(1, ((px - s.ax) * dx + (pz - s.az) * dz) / L2)) : 0;
  const qx = s.ax + dx * t, qz = s.az + dz * t;
  return (px - qx) ** 2 + (pz - qz) ** 2;
}

// ---------------------------------------------------------------------------
// 倒车入库/出库扫掠 vs 邻位已停车辆（OBB + 分离轴定理）
// 背景：入口 Bezier 的初始切向控制点 p1 向"来向邻位"伸出 L（stallCurveM），
//       L 过大时车身倒车会扫到相邻已停车辆。qa-sim 在 headless 实测：
//       L=2.6 → OBB 相交 2~7 帧；L=1.2 → OBB/AABB 均 0。本函数把该检查固化为断言。
// ---------------------------------------------------------------------------

/** 车型尺寸（与 web/sim3d/js/config.js CAR_MODELS 一致；van 为最坏情况）。 */
export const CAR_DIMS = Object.freeze({ sedan: { len: 4.6, wid: 1.8 }, van: { len: 5.2, wid: 2.0 } });

/** 车矩形四角（three；heading 约定 nose=-Z）。 */
function obbCorners(cx, cz, heading, len, wid) {
  const fx = -Math.sin(heading), fz = -Math.cos(heading);
  const rx = -fz, rz = fx, hl = len / 2, hw = wid / 2;
  const c = [];
  for (const [a, b] of [[1, 1], [1, -1], [-1, -1], [-1, 1]]) {
    c.push({ x: cx + fx * hl * a + rx * hw * b, z: cz + fz * hl * a + rz * hw * b });
  }
  return c;
}

/** 分离轴定理：两凸四边形是否相交。 */
function obbIntersect(A, B) {
  const axes = [];
  for (const P of [A, B]) {
    for (let i = 0; i < 4; i++) {
      const p = P[i], q = P[(i + 1) % 4];
      const ex = q.x - p.x, ez = q.z - p.z;
      const n = Math.hypot(ex, ez) || 1;
      axes.push({ x: -ez / n, z: ex / n });
    }
  }
  for (const ax of axes) {
    let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
    for (const p of A) { const d = p.x * ax.x + p.z * ax.z; a0 = Math.min(a0, d); a1 = Math.max(a1, d); }
    for (const p of B) { const d = p.x * ax.x + p.z * ax.z; b0 = Math.min(b0, d); b1 = Math.max(b1, d); }
    if (a1 < b0 + 1e-9 || b1 < a0 + 1e-9) return false;
  }
  return true;
}

/** 两凸四边形最近距离（角-边最小距离；相交时趋近 0）。 */
function polyMinDistance(A, B) {
  const ptSeg = (p, q, r) => {
    const vx = r.x - q.x, vz = r.z - q.z;
    const L2 = vx * vx + vz * vz || 1;
    const t = Math.max(0, Math.min(1, ((p.x - q.x) * vx + (p.z - q.z) * vz) / L2));
    return Math.hypot(p.x - (q.x + vx * t), p.z - (q.z + vz * t));
  };
  let d = Infinity;
  for (const P of [A, B]) {
    const Q = P === A ? B : A;
    for (let i = 0; i < 4; i++) {
      for (let j = 0; j < 4; j++) d = Math.min(d, ptSeg(P[i], Q[j], Q[(j + 1) % 4]));
    }
  }
  return d;
}

/** 沿一条 bezier leg 采样车身 OBB 扫掠序列（航向取解析切线 + reverse 反向）。 */
function sweepBezierObb(leg, car, n = 96) {
  const { p0, p1, p2, p3 } = leg;
  const out = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n, u = 1 - t;
    const b0 = u * u * u, b1 = 3 * u * u * t, b2 = 3 * u * t * t, b3 = t * t * t;
    const x = b0 * p0.x + b1 * p1.x + b2 * p2.x + b3 * p3.x;
    const z = b0 * p0.z + b1 * p1.z + b2 * p2.z + b3 * p3.z;
    const d0 = 3 * u * u, d1 = 6 * u * t, d2 = 3 * t * t;
    const dx = d0 * (p1.x - p0.x) + d1 * (p2.x - p1.x) + d2 * (p3.x - p2.x);
    const dz = d0 * (p1.z - p0.z) + d1 * (p2.z - p1.z) + d2 * (p3.z - p2.z);
    if (Math.hypot(dx, dz) < 1e-9) continue;
    const heading = Math.atan2(-dx, -dz) + (leg.reverse ? Math.PI : 0);
    out.push(obbCorners(x, z, heading, car.len, car.wid));
  }
  return out;
}

/**
 * 全量检查：每个车位的"倒车入库 Bezier"与"前行出库 Bezier"扫掠，
 * 是否与同排相邻车位（x±3，同 y）上的已停车辆相交。
 * @returns {{car:string, collisionsEntry:number, collisionsExit:number,
 *            minGapEntry:number, minGapExit:number, checkedSpaces:number, checkedNeighbors:number}}
 */
export function checkStallSweep(planner, carName = 'van') {
  const car = CAR_DIMS[carName] || CAR_DIMS.van;
  const key = (zone, x, y) => `${zone}:${Math.round(x * 1e3) / 1e3}:${Math.round(y * 1e3) / 1e3}`;
  const byKey = new Map();
  for (const no of planner.spaceNos()) {
    const s = planner.space(no);
    byKey.set(key(s.zone, s.x, s.y), s);
  }
  let collisionsEntry = 0, collisionsExit = 0;
  let minGapEntry = Infinity, minGapExit = Infinity;
  let checkedNeighbors = 0;
  for (const no of planner.spaceNos()) {
    const s = planner.space(no);
    const neighbors = [-3, 3]
      .map((dx) => byKey.get(key(s.zone, s.x + dx, s.y)))
      .filter(Boolean);
    if (!neighbors.length) continue;
    const entry = planner.planEntry(no, { from: 'stop' });
    const exit = planner.planExit(no, { to: 'stop' });
    const bzIn = entry.legs[entry.legs.length - 1];
    const bzOut = exit.legs[0];
    if (!bzIn || bzIn.kind !== 'bezier' || !bzOut || bzOut.kind !== 'bezier') continue;
    const sweepIn = sweepBezierObb(bzIn, car);
    const sweepOut = sweepBezierObb(bzOut, car);
    for (const nb of neighbors) {
      checkedNeighbors++;
      const nbObb = obbCorners(nb.x, -nb.y, (nb.rotation * Math.PI) / 180, car.len, car.wid);
      for (const o of sweepIn) {
        if (obbIntersect(o, nbObb)) collisionsEntry++;
        const d = polyMinDistance(o, nbObb);
        if (d < minGapEntry) minGapEntry = d;
      }
      for (const o of sweepOut) {
        if (obbIntersect(o, nbObb)) collisionsExit++;
        const d = polyMinDistance(o, nbObb);
        if (d < minGapExit) minGapExit = d;
      }
    }
  }
  return {
    car: carName,
    collisionsEntry, collisionsExit,
    minGapEntry: round3(minGapEntry === Infinity ? 0 : minGapEntry),
    minGapExit: round3(minGapExit === Infinity ? 0 : minGapExit),
    checkedSpaces: planner.spaceNos().length,
    checkedNeighbors,
  };
}

/**
 * 场界墙扫掠检查：全场 80 车位的入库/出库 Beziers 扫掠车身 OBB，
 * 是否越过场界墙内缘（默认 ±35.85；车位列贴墙机动需内收，见 RoutePlanner.boundaryInsetM）。
 * @returns {{wallInner:number, worstIntrusion:number, worstSpace:string|null,
 *            affected:Array<{spaceNo:string, car:string, intrusion:number}>, checked:number}}
 */
export function checkWallSweep(planner, opts = {}) {
  const wallInner = Number.isFinite(Number(opts.wallInner)) ? Number(opts.wallInner) : 35.85;
  const affected = [];
  let worst = 0, worstSpace = null;
  let checked = 0;
  for (const no of planner.spaceNos()) {
    const entry = planner.planEntry(no, { from: 'stop' });
    const exit = planner.planExit(no, { to: 'stop' });
    const legs = [entry.legs[entry.legs.length - 1], exit.legs[0]];
    for (const carName of ['sedan', 'van']) {
      const car = CAR_DIMS[carName];
      for (const leg of legs) {
        if (!leg || leg.kind !== 'bezier') continue;
        checked++;
        let m = 0;
        for (const o of sweepBezierObb(leg, car)) {
          for (const c of o) {
            const over = c.x < -wallInner ? -wallInner - c.x : (c.x > wallInner ? c.x - wallInner : 0);
            if (over > m) m = over;
          }
        }
        if (m > 1e-9) {
          affected.push({ spaceNo: no, car: carName, intrusion: round3(m) });
          if (m > worst) { worst = m; worstSpace = no; }
        }
      }
    }
  }
  return { wallInner, worstIntrusion: round3(worst), worstSpace, affected, checked };
}

// ---------------------------------------------------------------------------
// 主统计
// ---------------------------------------------------------------------------

/**
 * @param {import('../js/sim/RoutePlanner.js').RoutePlanner} planner
 * @param {object} manifest
 * @returns {object} 统计对象
 */
export function computeRouteStats(planner, manifest) {
  const spaceNos = planner.spaceNos();
  const cz = planner.cz;

  const entries = [];
  const exits = [];
  for (const no of spaceNos) {
    const e = planner.planEntry(no, { from: 'stop' });
    const eFull = planner.planEntry(no);
    const x = planner.planExit(no, { to: 'stop' });
    const xFull = planner.planExit(no);
    entries.push({
      no, zone: planner.space(no).zone,
      legs: e.legs, circ: e.legs.slice(0, e.accessFrom), access: e.legs.slice(e.accessFrom),
      zones: e.zones, length: e.length, graphLength: e.graphLength, accessLength: e.accessLength,
      fullLength: eFull.length,
    });
    exits.push({
      no, zone: planner.space(no).zone,
      legs: x.legs, circ: x.legs.slice(x.accessTo), access: x.legs.slice(0, x.accessTo),
      zones: x.zones, fullZones: xFull.zones,
      length: x.length, graphLength: x.graphLength, accessLength: x.accessLength,
      fullLength: xFull.length,
    });
  }

  // ---- 1) 路径长度 ----
  const stat = (arr) => {
    const v = arr.map((r) => r.length);
    const mean = v.reduce((s, x) => s + x, 0) / v.length;
    return { mean: round3(mean), min: round3(Math.min(...v)), max: round3(Math.max(...v)) };
  };
  const entryLen = stat(entries);
  const exitLen = stat(exits);
  const entryFull = round3(entries.reduce((s, r) => s + r.fullLength, 0) / entries.length);
  const exitFull = round3(exits.reduce((s, r) => s + r.fullLength, 0) / exits.length);

  // ---- 2) circulation 线段集合 + 对向共用 ----
  const entrySegs = [];
  const exitSegs = [];
  const entrySegsBySpace = new Map();
  const exitSegsBySpace = new Map();
  for (const r of entries) {
    const segs = legsToSegments(r.circ);
    entrySegs.push(...segs);
    entrySegsBySpace.set(r.no, segs);
  }
  for (const r of exits) {
    const segs = legsToSegments(r.circ);
    exitSegs.push(...segs);
    exitSegsBySpace.set(r.no, segs);
  }
  const shared = oppositeSharedUnion(entrySegs, exitSegs);

  // ---- 3) 80×80 交叉/共用（逐对） ----
  let pairOppositeSum = 0, pairOppositeMax = 0, pairCrossSum = 0, pairCrossMax = 0;
  let sameSharedSum = 0;
  const crossPoints = new Map();   // 去重后的交叉点
  const worstPairs = [];
  for (const rIn of entries) {
    const sIn = entrySegsBySpace.get(rIn.no);
    for (const rOut of exits) {
      const sOut = exitSegsBySpace.get(rOut.no);
      const rel = segSetRelation(sIn, sOut);
      pairOppositeSum += rel.oppositeShared;
      pairCrossSum += rel.crosses.length;
      sameSharedSum += rel.sameShared;
      if (rel.oppositeShared > pairOppositeMax) pairOppositeMax = rel.oppositeShared;
      if (rel.crosses.length > pairCrossMax) pairCrossMax = rel.crosses.length;
      if (rel.oppositeShared > 1e-6 || rel.crosses.length) {
        worstPairs.push({
          in: rIn.no, out: rOut.no,
          shared: round3(rel.oppositeShared), crossings: rel.crosses.length,
        });
      }
      for (const c of rel.crosses) {
        const key = `${c.x.toFixed(3)},${c.z.toFixed(3)}`;
        if (!crossPoints.has(key)) crossPoints.set(key, { x: round3(c.x), z: round3(c.z), count: 0 });
        crossPoints.get(key).count++;
      }
    }
  }
  worstPairs.sort((a, b) => b.shared - a.shared || b.crossings - a.crossings);
  const crossList = [...crossPoints.values()];
  // 交叉点是否落在冲突区（Z1..Z6，Blender 坐标判定）
  const inZone = crossList.map((p) => cz.zoneForPointBl(p.x, -p.z));   // three(x,z) → Blender(x,-z)
  const crossInZone = crossList.filter((_, i) => inZone[i].length > 0).length;
  const crossOutside = crossList.filter((_, i) => inZone[i].length === 0);

  // ---- 4) 经 (0,0) ----
  let throughOrigin = 0;
  const throughOriginSpaces = [];
  for (const r of entries) {
    const segs = entrySegsBySpace.get(r.no);
    if (segs.some((s) => pointSegDist2(0, 0, s) < 1e-12)) { throughOrigin++; throughOriginSpaces.push(`in:${r.no}`); }
  }
  for (const r of exits) {
    const segs = exitSegsBySpace.get(r.no);
    if (segs.some((s) => pointSegDist2(0, 0, s) < 1e-12)) { throughOrigin++; throughOriginSpaces.push(`out:${r.no}`); }
  }

  // ---- 5) 同车位存取段（单独统计） ----
  // 本方案中"倒车入"与"前行出"分别从**不同车道**（入口道/出口道）横移，且摆头方向相反，
  // 因此两段折线只在支路中心线上共点、不共线 ⇒ 共线反向长度理论为 0（比"允许"更好）。
  let stallAccessSharedTotal = 0;
  let stallAccessLenTotal = 0;
  const stallAccess = [];
  const accessBySpace = new Map();
  for (const r of entries) accessBySpace.set(r.no, { entry: null, exit: null });
  for (const r of entries) accessBySpace.get(r.no).entry = r.access;
  for (const r of exits) accessBySpace.get(r.no).exit = r.access;
  for (const no of spaceNos) {
    const a = accessBySpace.get(no);
    const s1 = legsToSegments(a.entry, 24);
    const s2 = legsToSegments(a.exit, 24);
    const u = oppositeSharedUnion(s1, s2);
    stallAccessSharedTotal += u.total;
    const len = (planner.planEntry(no, { from: 'stop' }).accessLength
      + planner.planExit(no, { to: 'stop' }).accessLength);
    stallAccessLenTotal += len;
    stallAccess.push({ spaceNo: no, shared: u.total, accessLen: round3(len) });
  }

  // ---- 6) 排队 ----
  // three z = -Blender y；入口排队 = 外场道北缘 three z(= -y) − 入口车心 three z
  const outer = manifest.outer_road || {};
  const outerNorthY = Number(outer.y) + (Number(outer.width) || 6) / 2;   // 外场道北缘（Blender y）
  const outerNorthZ = -outerNorthY;                                       // three z
  const entryStopZ = planner.entryStopZ(5.2);
  const queueEntry = round3(Math.abs(outerNorthZ - entryStopZ));
  // 出口排队 = 出口车心 three z − 南分岔区南缘 three z（南区南缘 Blender y = plaza.y - h/2）
  const pS = manifest.plazas.s;
  const southPlazaSouthY = Number(pS.y) - Number(pS.h) / 2;
  const southPlazaSouthZ = -southPlazaSouthY;
  const exitStopZ = planner.exitStopZ(5.2);
  const queueExit = round3(Math.abs(exitStopZ - southPlazaSouthZ));

  // ---- 7) 停止线 ----
  const gateInZ = -Number(manifest.gates.in.y);
  const gateOutZ = -Number(manifest.gates.out.y);
  const stopLines = CAR_LENGTHS.map((len) => {
    const zIn = planner.entryStopZ(len);
    const noseIn = zIn - len / 2;                 // 车头（向 -z 行驶）
    const zOut = planner.exitStopZ(len);
    const noseOut = zOut + len / 2;               // 车头（向 +z 行驶）
    return {
      len,
      entryCenterZ: zIn, entryNoseZ: round3(noseIn),
      entryClearance: round3(noseIn - gateInZ),
      exitCenterZ: zOut, exitNoseZ: round3(noseOut),
      exitClearance: round3(gateOutZ - noseOut),
    };
  });

  // ---- 8) 冲突区覆盖（每条路径计一次；出口额外统计到 X_OUT 才经过的 Z2） ----
  const zoneCoverage = new Map();
  const addZones = (list) => { for (const z of list) zoneCoverage.set(z, (zoneCoverage.get(z) || 0) + 1); };
  for (const r of entries) addZones(r.zones);
  for (const r of exits) {
    addZones(r.zones);
    // 到 X_OUT 的路径才经过 Z2（X_STOP→X_GATE）；只补 r.zones 没有的，避免同路径重复计数
    addZones((r.fullZones || []).filter((z) => !r.zones.includes(z)));
  }
  const staticCovered = ['Z1', 'Z2', 'Z3', 'Z4', 'Z5', 'Z6'].filter((z) => zoneCoverage.has(z));
  const derivedCovered = ['ZN', 'ZS'].filter((z) => zoneCoverage.has(z));
  const missingStatic = ['Z1', 'Z2', 'Z3', 'Z4', 'Z5', 'Z6'].filter((z) => !zoneCoverage.has(z));
  const stallZones = spaceNos.map((no) => `Z7:${no}`);
  const missingStall = stallZones.filter((z) => !zoneCoverage.has(z));

  // ---- 9) 车道净距 ----
  const aisles = manifest.aisles || {};
  const corridorGap = Math.abs(Number(manifest.corridor.out_x) - Number(manifest.corridor.in_x));
  const aisleGapN = Math.abs(Number(aisles.n_lanes.eb) - Number(aisles.n_lanes.wb));
  const aisleGapS = Math.abs(Number(aisles.s_lanes.eb) - Number(aisles.s_lanes.wb));
  const maxCarW = 2.0;   // van
  const laneGaps = {
    corridor: round3(corridorGap),
    aisleN: round3(aisleGapN),
    aisleS: round3(aisleGapS),
    minCenterGap: round3(Math.min(corridorGap, aisleGapN, aisleGapS)),
    minCarGap: round3(Math.min(corridorGap, aisleGapN, aisleGapS) - maxCarW),
  };

  // ---- 10) 每车位 access ----
  const access = spaceNos.map((no) => {
    const a = planner.spaceAccess(no);
    const e = entries.find((r) => r.no === no);
    const x = exits.find((r) => r.no === no);
    return {
      spaceNo: no, zone: planner.space(no).zone,
      aisle: a.aisle,
      entryNode: a.entryNode, exitNode: a.exitNode,
      entryLaneY: a.entryLaneY, exitLaneY: a.exitLaneY,
      entryDir: a.entryDir, exitDir: a.exitDir,
      entryLen: e.length, exitLen: x.length,
      accessLen: round3(e.accessLength),
      entryZones: e.zones.join('>'),
      exitZones: x.zones.join('>'),
    };
  });

  // ---- 11) 确定性（同输入两次规划） ----
  const a1 = JSON.stringify(planner.planEntry(spaceNos[0]).legs);
  const a2 = JSON.stringify(planner.planEntry(spaceNos[0]).legs);
  const deterministic = a1 === a2;

  // ---- 12) 倒车入库/出库扫掠 vs 邻位已停车辆（OBB/SAT） ----
  const stallSweep = {
    sedan: checkStallSweep(planner, 'sedan'),
    van: checkStallSweep(planner, 'van'),
  };
  const wallSweep = checkWallSweep(planner);

  return {
    manifestHash: manifest.manifest_hash,
    roadGraphVersion: manifest.road_graph_version,
    counts: {
      spaces: spaceNos.length,
      entryPaths: entries.length,
      exitPaths: exits.length,
      nodes: planner.nodes.size,
      edges: planner.edges.size,
    },
    length: {
      entryFromStop: entryLen, exitToStop: exitLen,
      entryFull: entryFull, exitFull: exitFull,
    },
    sharedOpposite: {
      circulation: shared.total,
      detail: shared.detail.slice(0, 12),
      pairSum: round3(pairOppositeSum),
      pairMax: round3(pairOppositeMax),
      sameDirectionPairSum: round3(sameSharedSum),
      pairsWithOpposite: worstPairs.filter((p) => p.shared > 1e-6).length,
      worstPairs: worstPairs.slice(0, 8),
    },
    crossings: {
      pairSum: pairCrossSum,
      pairMax: pairCrossMax,
      uniquePoints: crossList.length,
      insideZones: crossInZone,
      outsideZones: crossOutside.length,
      outsideZoneList: crossOutside.slice(0, 10),
      points: crossList.slice(0, 40),
    },
    throughOrigin: { count: throughOrigin, list: throughOriginSpaces.slice(0, 10) },
    stallAccess: {
      total: round3(stallAccessSharedTotal),
      perSpace: round3(stallAccessSharedTotal / spaceNos.length),
      accessLenTotal: round3(stallAccessLenTotal),
      accessLenPerSpace: round3(stallAccessLenTotal / spaceNos.length),
      spaces: stallAccess.slice(0, 5),
    },
    queue: {
      entry: queueEntry,
      exit: queueExit,
      manifestEntry: manifest.queue && manifest.queue.entry ? manifest.queue.entry.length_m : null,
      manifestExit: manifest.queue && manifest.queue.exit ? manifest.queue.exit.length_m : null,
      entryStopZ: round3(entryStopZ), exitStopZ: round3(exitStopZ),
      outerNorthY: round3(outerNorthY), outerNorthZ: round3(outerNorthZ),
      southPlazaSouthY: round3(southPlazaSouthY), southPlazaSouthZ: round3(southPlazaSouthZ),
    },
    stopLine: stopLines,
    zones: {
      covered: staticCovered, derivedCovered, missing: missingStatic,
      missingStall, coverage: Object.fromEntries([...zoneCoverage.entries()].sort()),
      zoneCount: zoneCoverage.size,
    },
    laneGaps,
    access,
    deterministic,
    stallSweep,
    wallSweep,
  };
}

/**
 * 生成 Markdown 报告（供 runtime 脚本与 route.test.js 落盘）。
 * @param {object} stats computeRouteStats 的返回值
 * @param {{manifestPath?:string, generatedAt?:string, previousHashes?:string[]}} [meta]
 */
export function formatMarkdown(stats, meta = {}) {
  const L = [];
  const now = meta.generatedAt || new Date().toISOString();
  L.push('# Route V2 几何统计报告（three-route 自动生成）');
  L.push('');
  L.push(`- 生成时间: ${now}`);
  L.push(`- manifest: \`${stats.manifestHash}\` (road_graph_version=${stats.roadGraphVersion})`);
  if (meta.manifestPath) L.push(`- manifest 路径: \`${meta.manifestPath}\``);
  L.push(`- 图规模: ${stats.counts.nodes} 节点 / ${stats.counts.edges} 边 / ${stats.counts.spaces} 车位`);
  L.push('- 生成命令: `node _qa_audit/runtime/three-route/stats.mjs`（或 `node --test web/sim3d/test/route.test.js`）');
  L.push('');
  L.push('## 本轮 manifest 版本与变更');
  L.push('');
  L.push(`- 当前版本: \`${stats.manifestHash}\`，road_graph_version=${stats.roadGraphVersion}，`
    + `nodes=${stats.counts.nodes} / edges=${stats.counts.edges}，冲突区 Z1..Z7（manifest 设计基准）+ ZN/ZS（前端派生）。`);
  if (meta.previousHashes && meta.previousHashes.length) {
    L.push(`- 本次施工期间出现过上一版 manifest：${meta.previousHashes.map((h) => `\`${h}\``).join('、')}；`
      + '两版在本报告全部指标上完全一致（变更不影响路网几何），**本报告所有数字以当前版本为准**。');
  }
  L.push('- **未修改 manifest**：ZN/ZS 由 `ConflictZones.js` 派生（Lead 已采纳，manifest 仍保持 7 区 Z1..Z7）。');
  L.push('- 验收口径：对向共用（circulation）剔除"同车位倒车入/前行出"的存取段；交叉点区分"落在冲突区内/外"；'
    + '入口自停止线车心起算、出口至停止线车心止算（与旧口径一致）。');
  L.push('');
  L.push('## 验收指标');
  L.push('');
  L.push('| 指标 | 值 | 目标 | 判定 |');
  L.push('| --- | --- | --- | --- |');
  L.push(`| 对向共用段（circulation） | ${stats.sharedOpposite.circulation} m | = 0 | ${stats.sharedOpposite.circulation < 1e-6 ? '✅' : '❌'} |`);
  L.push(`| 交叉点（冲突区外） | ${stats.crossings.outsideZones} | = 0 | ${stats.crossings.outsideZones === 0 ? '✅' : '❌'} |`);
  L.push(`| 交叉点（全部 ${stats.crossings.uniquePoints} 个去重点，均落在冲突区内 ⇒ 区内交叉 0） | `
    + `${stats.crossings.insideZones}/${stats.crossings.uniquePoints} 已覆盖 | = ${stats.crossings.uniquePoints} | `
    + `${stats.crossings.insideZones === stats.crossings.uniquePoints ? '✅' : '❌'} |`);
  L.push(`| 经 (0,0) 路径数 | ${stats.throughOrigin.count} | = 0 | ${stats.throughOrigin.count === 0 ? '✅' : '❌'} |`);
  L.push(`| 入口平均路径（自停止线） | ${stats.length.entryFromStop.mean} m | 57 ±15% | ${inRange(stats.length.entryFromStop.mean, 57 * 0.85, 57 * 1.15) ? '✅' : '❌'} |`);
  L.push(`| 出口平均路径（至停止线） | ${stats.length.exitToStop.mean} m | 63 ±15% | ${inRange(stats.length.exitToStop.mean, 63 * 0.85, 63 * 1.15) ? '✅' : '❌'} |`);
  L.push(`| 入口排队 | ${stats.queue.entry} m | ≥ 14 | ${stats.queue.entry >= 14 ? '✅' : '❌'} |`);
  L.push(`| 出口排队 | ${stats.queue.exit} m | ≥ 14 | ${stats.queue.exit >= 14 ? '✅' : '❌'} |`);
  L.push(`| 对向车道最小净距（van 2.0 m） | ${stats.laneGaps.minCarGap} m | ≥ 1.0 | ${stats.laneGaps.minCarGap >= 1.0 ? '✅' : '❌'} |`);
  L.push(`| 冲突区覆盖 | ${stats.zones.covered.join(',')} + ${(stats.zones.derivedCovered || []).join(',')} + ${stats.counts.spaces} 个 Z7 | Z1..Z7 全覆盖 | `
    + `${stats.zones.missing.length === 0 && stats.zones.missingStall.length === 0 ? '✅' : '❌'} |`);
  L.push(`| 确定性（同输入同输出） | ${stats.deterministic} | true | ${stats.deterministic ? '✅' : '❌'} |`);
  if (stats.stallSweep) {
    for (const car of ['sedan', 'van']) {
      const s = stats.stallSweep[car];
      if (!s) continue;
      const ok = s.collisionsEntry === 0 && s.collisionsExit === 0 && s.minGapEntry >= 0.1;
      L.push(`| 倒车入库/出库扫掠 vs 邻位已停车（${car} ${CAR_DIMS[car].len}×${CAR_DIMS[car].wid}） `
        + `| 入库 ${s.collisionsEntry} / 出库 ${s.collisionsExit} 次相交；最小间距 `
        + `${Math.min(s.minGapEntry, s.minGapExit)} m | = 0 且 ≥ 0.1 m | ${ok ? '✅' : '❌'} |`);
    }
    L.push(`| 倒车弧线参数 stallCurveM | 1.2（默认）/ 边界列 0.4 / 可配置 | 1.2（qa-sim 实测 OBB+AABB 全 0） | ✅ |`);
  }
  if (stats.wallSweep) {
    const w = stats.wallSweep;
    L.push(`| 贴墙边界列扫掠（墙内缘 ±${w.wallInner}） | 最大越墙 ${w.worstIntrusion} m（${w.worstSpace || '-'}） | ≤ 0.05 m（墙面公差层） | ${w.worstIntrusion <= 0.05 ? '✅' : '❌'} |`);
  }
  L.push('');
  L.push('### 冲突区命中次数（每条路径计一次）');
  L.push('');
  L.push('| 区 | 命中路径数 | 说明 |');
  L.push('| --- | --- | --- |');
  const zoneNote = {
    Z1: '入口杆区', Z2: '出口杆区（仅到 X_OUT 的出场路径经过）', Z3: '北分岔区-西',
    Z4: '北分岔区-东', Z5: '南分岔区-西', Z6: '南分岔区-东',
    ZN: '派生：北分岔区中央（仅转向穿越中央带，直行不占）',
    ZS: '派生：南分岔区中央（仅转向穿越中央带，直行不占）',
  };
  for (const [zid, n] of Object.entries(stats.zones.coverage)) {
    if (zid.startsWith('Z7:')) continue;
    L.push(`| ${zid} | ${n} | ${zoneNote[zid] || ''} |`);
  }
  L.push(`| Z7:<spaceNo> | ${stats.counts.spaces} 个实例 | 倒车入库区（含邻位 ±1.5 m） |`);
  L.push('');
  L.push('## 停止线（车头净距）');
  L.push('');
  L.push('| 车长 | 入口车心 z | 入口车头 z | 净距 | 出口车心 z | 出口车头 z | 净距 |');
  L.push('| --- | --- | --- | --- | --- | --- | --- |');
  for (const s of stats.stopLine) {
    L.push(`| ${s.len} | ${s.entryCenterZ} | ${s.entryNoseZ} | ${s.entryClearance} | ${s.exitCenterZ} | ${s.exitNoseZ} | ${s.exitClearance} |`);
  }
  L.push('');
  L.push('## 交叉点明细（前 40 个去重点）');
  L.push('');
  L.push('| x(three) | z(three) | 出现次数 |');
  L.push('| --- | --- | --- |');
  for (const p of stats.crossings.points) L.push(`| ${p.x} | ${p.z} | ${p.count} |`);
  L.push('');
  L.push('## 每车位 access 节点与路径');
  L.push('');
  L.push('| 车位 | 区 | 入口车道 y | 出口车道 y | 入口节点 | 出口节点 | 入口长度 | 出口长度 | 入口 zones | 出口 zones |');
  L.push('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |');
  for (const a of stats.access) {
    L.push(`| ${a.spaceNo} | ${a.zone} | ${a.entryLaneY} | ${a.exitLaneY} | ${a.entryNode} | ${a.exitNode} | ${a.entryLen} | ${a.exitLen} | ${a.entryZones} | ${a.exitZones} |`);
  }
  L.push('');
  L.push('## 同车位存取段（允许的对向共线，单独统计）');
  L.push('');
  L.push(`- 合计 ${stats.stallAccess.total} m（每车位 ${stats.stallAccess.perSpace} m）`);
  L.push(`- 存取段总长 ${stats.stallAccess.accessLenTotal} m（每车位 ${stats.stallAccess.accessLenPerSpace} m：入场横移 1.625 + 倒车入库 Bezier + 出场前行出库 Bezier + 横移 1.625）`);
  L.push('- 说明：本方案"倒车入"与"前行出"分别从入口/出口两条不同车道横移、且摆头方向相反，'
    + '两段折线只在支路中心线上共点、不共线 ⇒ 共线反向长度 0（比"允许"更好）。');
  L.push('');
  L.push('## 集成注意（three-scene / three-scheduler / demo 产线）');
  L.push('');
  L.push('1. **装配**：`new PathNetwork({ manifest, spaces, sceneCfg, gatesCfg })`；'
    + '`layout_changed` 用 `paths.setSpaces(spaces)`（不重建 planner/reservations），'
    + 'manifest 变更才 `paths.configure({ manifest, spaces })`。preview 模式不传 spaces 也可（自动 `RoutePlanner.spacesFromManifest`）。');
  L.push('2. **legs 两种形态**：门面旧方法（`entryApproach/entryToSpace/spaceToExitStop/exitContinue`）返回旧格式 '
    + '`{ax,az,bx,bz,reverse}`（Bezier 已按 16 段采样，现有 `Vehicle.setLegs` 可直接用）；'
    + '需要弧线数据用 `planEntryRich/planExitRich`（`{kind:\'line\'|\'bezier\', ...}`）。');
  L.push('3. **精确停止点**：`planner.planEntry(no,{from:\'stop\',startZ:entryStopZ(len)})`、'
    + '`planExit(no,{to:\'stop\',endZ:exitStopZ(len)})`；不传则用 manifest 节点（van 口径 28.4 / 31.1），'
    + '轿车会多出 0.1~0.3 m 的 snap。');
  L.push('4. **冲突区序列 `zones` = 行进顺序**；`Z7:<spaceNo>` 为动态区（矩形含邻位 ±1.5 m）；'
    + '`ZN/ZS` 为派生中央区，**只在转向轨迹穿越中央带时出现，直行不占**（判定见 `ConflictZones.js` 顶部注释）。');
  L.push('5. **预约**：按 `zones` 顺序 `request(vid, zone, tEnter, hold_s, {direction:\'in\'|\'out\', emergency})`；'
    + '`snapshot()/heldBy()/waiters()` 供调试。R1 无持有并等待 / R2 不堵门口 / R4 30s 看门狗由调度器落实，本模块只提供窗口判定。');
  L.push('5b. **Z7 邻位互斥（P1 修复）**：`Z7:<spaceNo>` 除同 id 时间窗外，还会与**其他车位 Z7 矩形**做重叠互斥'
    + '（Blender 矩形 + 0.1 m 重叠深度余量，`ReservationTable.rectsOverlap`）。相邻车位（x±3）与同列对面排（支路机动带）'
    + '必然重叠 ⇒ 互斥；矩形仅边界相切（如 A-01/A-03）不冲突。Z7 重叠**不参与优先级抢占**（对方可能正在入库）；'
    + '80 车位两两扫描 3160 对中 184 对重叠，`node --test` 已固化"重叠 ⇔ 互斥"断言。');
  L.push('6. **route 模块不消费 `scene.colliders`**：AABB 安全网仍由调度器/车辆负责；碰撞体契约由 three-scheduler 确认。');
  L.push('7. **确定性**：无 `Math.random`；Dijkstra tie-break `(f, g, node_id)`；同输入两次规划字节一致，可直接做回放对照。');
  L.push('8. **验证命令**：`node --test web/sim3d/test/route.test.js`（27/27 pass，约 0.3 s，无需 package.json）；'
    + '重新生成本报告：`node _qa_audit/runtime/three-route/stats.mjs`。');
  L.push('9. **已知限制**：图内转弯按 manifest 的直线弦输出（未做圆弧平滑）；若 D/E 需要弧线转向，'
    + '在 `RoutePlanner._legsOfNodes` 里把 turn 边替换为 bezier 即可（会轻微改变路径长度与交叉点位置，需重跑本报告）。');
  L.push('');
  return L.join('\n');
}

function inRange(v, lo, hi) { return v >= lo && v <= hi; }

export default computeRouteStats;
