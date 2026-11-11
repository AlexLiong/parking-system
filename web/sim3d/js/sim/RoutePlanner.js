// 路网规划器（sim/RoutePlanner.js）。
//
// 纯逻辑模块：**不 import three**，可在 node --test 下直接单测。
// 数据源：assets_3d/layout/lot_v2.json（manifest，单一事实源）+ /api/layout 的 SpaceSpec[]。
// 坐标：manifest 为 Blender (x 东, y 北)；本模块对外输出的 Leg 一律为 **three 坐标 {x,z}**，
//       three_x = x, three_z = -y。
//
// 职责（契约 §3.1）：
//   - 由 manifest 的 nodes/edges 建有向图（115 节点 / 113 边），Dijkstra 求最短路
//     （时间权：长度/限速 + 转弯代价），确定性 tie-break (f, g, node_id)。
//   - planEntry(spaceNo)：IN_APRON → IN_STOP → … → 分岔区转向 → 支路入口车道链 →
//     车位口 → 横移 1.625 m 到支路中心线 → 倒车入库（Bezier，reverse=true，终点=车位中心）。
//   - planExit(spaceNo)：车位中心 → 前行出库（Bezier） → 支路中心线 → 出口车道链 →
//     分岔区转向 → X_STOP → X_GATE → X_OUT。
//   - 输出经过的冲突区序列 zones（Z1..Z6 来自 manifest 边的 zones 字段；Z7:<spaceNo> 动态）。
//   - entryStopZ(len) / exitStopZ(len)：按"车头停在杆前 0.3 m"反解车心停止 z。
//
// 说明：倒车入库段按契约用 Bezier；图内边按 manifest 折线（直线段）输出，
//       turn 边可选平滑为 Bezier（opts.smoothTurns=true），默认关闭以保证长度与 manifest 一致。

import {
  buildConflictZones,
  legLength,
  legPolyline,
  polylineLength,
  toThreeZ,
} from './ConflictZones.js';

/** 默认参数（可被 opts 覆盖）。 */
export const ROUTE_DEFAULTS = Object.freeze({
  straightSpeed: 2.5,     // m/s 直行限速
  turnSpeed: 1.2,         // m/s 分岔区限速
  turnPenalty: 2.0,       // s 每次转弯的等效代价
  stallAlignM: 1.625,     // 车道中心 → 支路中心线的横移量（6.5/4）
  // 倒车入库 Bezier 沿支路方向的控制点长度（m）。该值直接决定倒车时车身向"来向邻位"
  // 的横向摆幅：Bezier 中线最大横向偏移 = L*4/9，扫掠外缘还要加上车身半宽与转角。
  // 默认 1.2 由扫掠 OBB 与"邻位已停车辆"的碰撞实测反解（_qa_audit/runtime/three-route/
  // check-stall-curve.mjs）：最坏情况 van 5.2×2.0 × van 时
  //   L=2.6 → 碰撞（8 个采样穿透）; L=2.2 → 碰撞; L=2.0 → 间距仅 0.001 m（运行时不可用）;
  //   L=1.6 → 0.168 m; **L=1.2 → 0.358 m**；L=1.0 → 0.456 m；sedan 在 L=1.2 时 0.751 m。
  // 取 1.2 兼顾"零碰撞安全余量"与"肉眼可见的倒车弧线"。需要更保守时可传 {stallCurveM:1.0}。
  stallCurveM: 1.2,
  stallTangentM: 1.6,     // 倒车入库 Bezier 末端垂向控制点长度
  // ── 边界列（贴场界墙的首/末列：A-01/B-01/A-11/B-11/C-10/D-10/C-20/D-20）专用参数 ──
  // 这些车位中心距场界墙内缘仅 1.35 m（-34.5 vs -35.85），车身长 4.6~5.2 m，在车位
  // 中心处完成 90° 转向会让车头/前角扫出墙外（qa-sim headless 实测 0.48 m；解析扫掠
  // 复核 van 入库 1.315 m / 出库 1.596 m，sedan 1.0/1.284）。非边界列不受影响。
  // 修复：机动横向基准向内收 boundaryInsetM（到 ±33，即车道节点处），并把该处弧线
  // 收紧到 boundaryStallCurveM（小摆幅），再由 Bezier 摆入车位中心 ⇒ 车位落点/朝向不变。
  // 解析扫掠复核（entry+exit × sedan/van，见 _qa_audit/runtime/three-route/wall-sweep-analysis.json）：
  //   内收 1.5 + L=0.4 → 最大越墙 0.035 m（van，仅为墙面 0.15 m 公差层），sedan 0；邻位碰撞 0。
  //   只内收不收紧弧线（L=1.2）会让车尾摆向邻位 → 与已停邻车 OBB 相交，故必须同时收紧。
  boundaryInsetM: 1.5,
  boundaryStallCurveM: 0.4,
  maxLen: 5.2,            // 最长车型（van）长度，用于默认停止点
  stopClearanceM: 0.3,    // 车头与杆平面的最小净距
  // 注：图内转弯按 manifest 直线弦输出（未做圆弧平滑）；bezie 仅用于倒车入库/前行出库。
});

const EPS = 1e-9;
const PORT_EPS = 0.5;     // 长度小于该值的边视为"端口连接"，不计转弯代价

// ---------------------------------------------------------------------------
// 最小堆（确定性：dist 升序，同 dist 时 node id 字典序升序）
// ---------------------------------------------------------------------------
class MinHeap {
  constructor() { this.a = []; }
  get size() { return this.a.length; }
  static less(x, y) {
    if (x.d < y.d - EPS) return true;
    if (x.d > y.d + EPS) return false;
    return x.id < y.id;
  }
  push(item) {
    const a = this.a;
    a.push(item);
    let i = a.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (!MinHeap.less(a[i], a[p])) break;
      [a[i], a[p]] = [a[p], a[i]];
      i = p;
    }
  }
  pop() {
    const a = this.a;
    const top = a[0];
    const last = a.pop();
    if (a.length && last) {
      a[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1, r = l + 1;
        let m = i;
        if (l < a.length && MinHeap.less(a[l], a[m])) m = l;
        if (r < a.length && MinHeap.less(a[r], a[m])) m = r;
        if (m === i) break;
        [a[i], a[m]] = [a[m], a[i]];
        i = m;
      }
    }
    return top;
  }
}

/** 数值容差取整（避免浮点噪声写入路径）。 */
const r6 = (v) => Math.round(Number(v) * 1e6) / 1e6;

/** Blender (x,y) → three {x,z} 点。 */
const P = (x, y) => ({ x: r6(x), z: r6(toThreeZ(y)) });

/** 直线腿工厂（three 坐标）。 */
function lineLeg(a, b, extra = {}) {
  return { kind: 'line', ax: r6(a.x), az: r6(a.z), bx: r6(b.x), bz: r6(b.z), ...extra };
}

/** Bezier 腿工厂（three 坐标）。 */
function bezierLeg(p0, p1, p2, p3, extra = {}) {
  const c = (p) => ({ x: r6(p.x), z: r6(p.z) });
  return { kind: 'bezier', p0: c(p0), p1: c(p1), p2: c(p2), p3: c(p3), ...extra };
}

/** 去掉零长度直线腿（保留 Bezier）。 */
function cleanLegs(legs) {
  return legs.filter((l) => (l.kind === 'line' ? Math.hypot(l.bx - l.ax, l.bz - l.az) > 1e-6 : true));
}

/** 合并同向相邻直线腿（减少腿数；几何不变）。 */
function mergeLines(legs) {
  const out = [];
  for (const l of legs) {
    const prev = out[out.length - 1];
    if (prev && l.kind === 'line' && prev.kind === 'line') {
      const d1x = prev.bx - prev.ax, d1z = prev.bz - prev.az;
      const d2x = l.bx - l.ax, d2z = l.bz - l.az;
      const cross = d1x * d2z - d1z * d2x;
      const dot = d1x * d2x + d1z * d2z;
      const n1 = Math.hypot(d1x, d1z), n2 = Math.hypot(d2x, d2z);
      const collinear = Math.abs(cross) < 1e-6 * Math.max(1, n1 * n2);
      const sameDir = dot > 0;
      const touching = Math.abs(prev.bx - l.ax) < 1e-6 && Math.abs(prev.bz - l.az) < 1e-6;
      if (collinear && sameDir && touching) {
        prev.bx = l.bx; prev.bz = l.bz;
        continue;
      }
    }
    out.push({ ...l });
  }
  return out;
}

// ---------------------------------------------------------------------------
// RoutePlanner
// ---------------------------------------------------------------------------
export class RoutePlanner {
  /**
   * @param {object} manifest `assets_3d/layout/lot_v2.json`
   * @param {Array<object>} spaces `/api/layout` 的 SpaceSpec[]（x, z=Blender y, rotation, space_no, zone）
   * @param {object} [opts] 覆盖 ROUTE_DEFAULTS
   */
  constructor(manifest, spaces = [], opts = {}) {
    if (!manifest || !Array.isArray(manifest.nodes) || !Array.isArray(manifest.edges)) {
      throw new Error('RoutePlanner: 需要 lot_v2 manifest（含 nodes/edges）');
    }
    this.manifest = manifest;
    this.opts = { ...ROUTE_DEFAULTS, ...opts };
    this.cz = buildConflictZones(manifest);
    this._buildNodes();
    this._buildGraph();
    this._distCache = new Map();
    this._spaces = new Map();
    this.setSpaces(spaces);
  }

  // ------------------------------ 图构建 ------------------------------
  _buildNodes() {
    this.nodes = new Map();
    for (const n of this.manifest.nodes) {
      const x = Number(n.x), y = Number(n.y);
      this.nodes.set(n.id, { id: n.id, x, y, tx: x, tz: toThreeZ(y) });
    }
  }

  _buildGraph() {
    const O = this.opts;
    this.out = new Map();   // u -> [{v, edge}]
    this.inv = new Map();   // v -> [{v:u, edge}]  （反图，用于出场反向搜索）
    this.edges = new Map();
    for (const e of this.manifest.edges) {
      const u = this.nodes.get(e.u), v = this.nodes.get(e.v);
      if (!u || !v) throw new Error(`RoutePlanner: 边 ${e.id} 端点缺失 (${e.u}->${e.v})`);
      const len = Number.isFinite(Number(e.length))
        ? Number(e.length)
        : Math.hypot(v.x - u.x, v.y - u.y);
      const kind = e.kind || 'lane';
      const speed = kind === 'turn' ? O.turnSpeed : (Number(e.speed) || O.straightSpeed);
      const turn = kind === 'turn' && len > PORT_EPS ? O.turnPenalty : 0;
      const rec = {
        id: e.id, u: e.u, v: e.v, length: len, kind,
        speed, zones: Array.isArray(e.zones) ? e.zones.slice() : [],
        w: len / speed + turn,
      };
      this.edges.set(e.id, rec);
      if (!this.out.has(u.id)) this.out.set(u.id, []);
      this.out.get(u.id).push({ v: v.id, edge: rec });
      if (!this.inv.has(v.id)) this.inv.set(v.id, []);
      this.inv.get(v.id).push({ v: u.id, edge: rec });
    }
    // 邻接表按目标 id 排序，保证遍历顺序确定
    for (const list of this.out.values()) list.sort((a, b) => (a.v < b.v ? -1 : a.v > b.v ? 1 : 0));
    for (const list of this.inv.values()) list.sort((a, b) => (a.v < b.v ? -1 : a.v > b.v ? 1 : 0));
  }

  /**
   * 单源最短路（Dijkstra，确定性 tie-break）。
   * @param {string} source 节点 id
   * @param {boolean} [reverse] true = 在反图上搜索（求"到 source 的最短路"）
   * @returns {{dist:Map<string,number>, prev:Map<string,string>}}
   */
  dijkstra(source, reverse = false) {
    const key = `${reverse ? 'R' : 'F'}:${source}`;
    const cached = this._distCache.get(key);
    if (cached) return cached;
    const adj = reverse ? this.inv : this.out;
    const dist = new Map([[source, 0]]);
    const prev = new Map();
    const settled = new Set();
    const heap = new MinHeap();
    heap.push({ d: 0, id: source });
    while (heap.size) {
      const cur = heap.pop();
      if (settled.has(cur.id)) continue;
      settled.add(cur.id);
      const list = adj.get(cur.id) || [];
      for (const { v, edge } of list) {
        if (settled.has(v)) continue;
        const nd = cur.d + edge.w;
        const old = dist.has(v) ? dist.get(v) : Infinity;
        if (nd < old - EPS) {
          dist.set(v, nd);
          prev.set(v, cur.id);
          heap.push({ d: nd, id: v });
        }
      }
    }
    const res = { dist, prev };
    this._distCache.set(key, res);
    return res;
  }

  /**
   * 由 prev 链回溯节点序列。
   * @param {string} origin 搜索起点（正向=出发节点；反向=目标节点 X_OUT）
   * @param {string} other 另一端（正向=目标；反向=路径起点）
   * @param {boolean} [reverse] true = 使用反图（求"到 origin 的最短路"）
   * @returns {string[]|null} 统一返回**行进顺序**的节点序列
   */
  _nodePath(origin, other, reverse = false) {
    const { dist, prev } = this.dijkstra(origin, reverse);
    if (!dist.has(other)) return null;
    const seq = [];
    const seen = new Set();
    let cur = other;
    while (cur !== undefined && cur !== origin) {
      if (seen.has(cur)) throw new Error('RoutePlanner: prev 链出现环');
      seen.add(cur);
      seq.push(cur);
      cur = prev.get(cur);
    }
    if (cur !== origin) return null;
    seq.push(origin);
    // 正向：prev 链是 other→origin，需反转成 origin→other；
    // 反向：反图里的 prev 指向"朝 origin（X_OUT）方向的下一步"，seq 已是行进顺序
    if (!reverse) seq.reverse();
    return seq;
  }

  /** 节点序列 → 边序列。 */
  _edgesOfPath(seq) {
    const out = [];
    for (let i = 1; i < seq.length; i++) {
      const list = this.out.get(seq[i - 1]) || [];
      const hit = list.find((e) => e.v === seq[i]);
      if (!hit) throw new Error(`RoutePlanner: 路径不含边 ${seq[i - 1]}->${seq[i]}`);
      out.push(hit.edge);
    }
    return out;
  }

  // ------------------------------ 车位与支路 ------------------------------
  /**
   * 归一化车位输入。约定 `space.z` 为 **Blender y**（`/api/layout` 口径）；
   * 兼容旧场景对象（`space.z` 为 three z）时请使用 `fromSceneSpace` 转换。
   */
  setSpaces(spaces) {
    this._spaces = new Map();
    for (const sp of spaces || []) {
      const no = String(sp.space_no ?? sp.no ?? '');
      if (!no) continue;
      const x = Number(sp.x);
      const y = Number(sp.z ?? sp.y);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      this._spaces.set(no, {
        no, x, y, zone: String(sp.zone || no.slice(0, 1)).toUpperCase(),
        rotation: Number(sp.rotation) || 0, raw: sp,
      });
    }
    this._accessCache = new Map();
    return this;
  }

  /** 把场景对象（z=three z）转成 planner 口径（z=Blender y）。 */
  static fromSceneSpace(space) {
    if (!space) return null;
    const no = space.no ?? space.space_no;
    return {
      space_no: no, zone: space.zone, rotation: space.rotation,
      x: space.x, z: -Number(space.z ?? 0),
    };
  }

  space(spaceNo) {
    const sp = this._spaces.get(String(spaceNo));
    if (!sp) throw new Error(`RoutePlanner: 未知车位 ${spaceNo}`);
    return sp;
  }

  hasSpace(spaceNo) { return this._spaces.has(String(spaceNo)); }

  /** manifest 里的 zone_lanes 记录。 */
  _zoneLane(zone) {
    const zl = (this.manifest.zone_lanes || {})[zone];
    if (!zl) throw new Error(`RoutePlanner: manifest.zone_lanes 缺少 ${zone} 区`);
    return zl;
  }

  /** 某条车道链的节点数组（按 **行驶方向** 升序：index 增大 = 前进）。 */
  _chain(prefix) {
    const re = new RegExp(`^${prefix}_(\\d+)$`);
    const list = [];
    for (const id of this.nodes.keys()) {
      const m = re.exec(id);
      if (m) list.push({ idx: Number(m[1]), node: this.nodes.get(id) });
    }
    list.sort((a, b) => a.idx - b.idx);
    return list.map((e) => e.node);
  }

  _chains(zone) {
    const an = (this.manifest.aisle_nodes || {})[zone];
    if (!an) throw new Error(`RoutePlanner: manifest.aisle_nodes 缺少 ${zone} 区`);
    const k = `${zone}`;
    if (!this._chainCache) this._chainCache = new Map();
    if (!this._chainCache.has(k)) {
      this._chainCache.set(k, { entry: this._chain(an.entry), exit: this._chain(an.exit) });
    }
    return this._chainCache.get(k);
  }

  /**
   * 支路访问信息（契约 §3.1）。
   * @returns {{aisle:string, entryLaneY:number, exitLaneY:number, entryDir:number,
   *            exitDir:number, entryNode:string, exitNode:string,
   *            aisleY:number, entryChain:string, exitChain:string}}
   */
  spaceAccess(spaceNo) {
    const key = String(spaceNo);
    if (this._accessCache.has(key)) return this._accessCache.get(key);
    const sp = this.space(key);
    const zl = this._zoneLane(sp.zone);
    const an = (this.manifest.aisle_nodes || {})[sp.zone];
    const chains = this._chains(sp.zone);
    const aisleY = zl.aisle === 'n'
      ? Number(this.manifest.aisles.n_y)
      : Number(this.manifest.aisles.s_y);

    // 入口：沿行驶方向取"尚未越过车位"的最后一个节点
    const entryDir = Math.sign(zl.entry_dir) || 1;
    let entryNode = chains.entry[0];
    for (const n of chains.entry) {
      if (entryDir * (n.x - sp.x) <= EPS) entryNode = n;
      else break;
    }
    if (!entryNode) entryNode = chains.entry[0];
    // 出口：沿行驶方向取"已到达/越过车位"的第一个节点
    const exitDir = Math.sign(zl.exit_dir) || 1;
    let exitNode = chains.exit[chains.exit.length - 1];
    for (const n of chains.exit) {
      if (exitDir * (n.x - sp.x) >= -EPS) { exitNode = n; break; }
    }
    if (!exitNode) exitNode = chains.exit[chains.exit.length - 1];

    const info = {
      aisle: zl.aisle,
      entryLaneY: Number(zl.entry_y),
      exitLaneY: Number(zl.exit_y),
      entryDir,
      exitDir,
      entryNode: entryNode.id,
      exitNode: exitNode.id,
      aisleY,
      entryChain: an.entry,
      exitChain: an.exit,
      entryNodeX: entryNode.x,
      exitNodeX: exitNode.x,
      spaceX: sp.x,
      spaceY: sp.y,
    };
    this._accessCache.set(key, info);
    return info;
  }

  /** 入场接近节点的 id（车位口对应的支路入口车道节点）。 */
  approachNode(spaceNo) { return this.spaceAccess(spaceNo).entryNode; }

  // ------------------------------ 停止线 ------------------------------
  /** 入口车心停止 z（three）。公式：z = -gateY + clearance + len/2。 */
  entryStopZ(len = this.opts.maxLen) {
    const g = this.manifest.gates.in;
    const clear = Number.isFinite(Number(g.stop_clearance_m)) ? Number(g.stop_clearance_m) : this.opts.stopClearanceM;
    return r6(-Number(g.y) + clear + Number(len) / 2);
  }

  /** 出口车心停止 z（three）。公式：z = -gateY - clearance - len/2。 */
  exitStopZ(len = this.opts.maxLen) {
    const g = this.manifest.gates.out;
    const clear = Number.isFinite(Number(g.stop_clearance_m)) ? Number(g.stop_clearance_m) : this.opts.stopClearanceM;
    return r6(-Number(g.y) - clear - Number(len) / 2);
  }

  /** 入口接近路径（外场排队 → 入口停止线）。 */
  planApproach(spawnZ = null) {
    const g = this.manifest.gates.in;
    const z0 = spawnZ != null ? Number(spawnZ) : -Number((this.manifest.outer_road || {}).y ?? -48.5);
    const zStop = this.entryStopZ(this.opts.maxLen);
    return cleanLegs([lineLeg({ x: Number(g.x), z: z0 }, { x: Number(g.x), z: zStop })]);
  }

  /** 出口越闸后驶离（X_STOP → X_GATE → X_OUT）。 */
  planExitGate() {
    const seq = this._nodePath('X_OUT', 'X_STOP', true);
    if (!seq) throw new Error('RoutePlanner: 出口闸后路径不可达');
    return cleanLegs(this._legsOfNodes(seq));
  }

  // ------------------------------ 腿构建 ------------------------------
  _legsOfNodes(seq, safeTurns = false) {
    const legs = [];
    for (let i = 1; i < seq.length; i++) {
      const a = this.nodes.get(seq[i - 1]);
      const b = this.nodes.get(seq[i]);
      const edge = (this.out.get(a.id) || []).find((e) => e.v === b.id)?.edge;
      const extra = edge && edge.zones.length ? {zones:edge.zones.slice()} : {};
      if(safeTurns && edge?.kind==='turn' && Math.abs(a.tx-b.tx)>.1 && Math.abs(a.tz-b.tz)>.1) {
        // Keep the full vehicle clear of the median before changing heading.
        const north = a.tz < 0;
        const z = -(north ? this.manifest.aisles.n_y : this.manifest.aisles.s_y) + Math.sign(b.tx-a.tx)*0.20;
        if(Math.abs(Math.abs(b.tx)-6)<1e-6){
          const dx=b.tx-a.tx;
          legs.push(lineLeg({x:a.tx,z:a.tz},{x:a.tx,z},extra));
          const short=Math.abs(dx)<4;
          legs.push(bezierLeg({x:a.tx,z},short?{x:a.tx,z:b.tz}:{x:a.tx+dx*.4,z},short?{x:a.tx,z:b.tz}:{x:b.tx-dx*.4,z:b.tz},{x:b.tx,z:b.tz},extra));
          continue;
        }
        const dx=b.tx-a.tx;
        const short=Math.abs(dx)<4;
        legs.push(bezierLeg({x:a.tx,z:a.tz},short?{x:b.tx,z:a.tz}:{x:a.tx+dx*.4,z:a.tz},short?{x:b.tx,z:a.tz}:{x:b.tx-dx*.4,z},{x:b.tx,z},extra));
        legs.push(lineLeg({x:b.tx,z},{x:b.tx,z:b.tz},extra));
      } else legs.push(lineLeg({ x: a.tx, z: a.tz }, { x: b.tx, z: b.tz },extra));
    }
    return legs;
  }

  /**
   * 入库/出库机动的横向基准 x：
   *   - 普通车位 = 车位中心 x（与原实现一致）；
   *   - 边界列（紧贴场界墙的首/末列）= 车位中心向场内内收 boundaryInsetM（默认 1.5 m），
   *     避免车身在车位中心处 90° 转向时前角扫出场界墙（qa-sim 报告 + 解析扫掠复核）。
   * @returns {{x:number, boundary:boolean}}
   */
  _stallManeuverX(sp) {
    const grid = (this.manifest.space_grid || {})[sp.zone];
    const inset = Number(this.opts.boundaryInsetM);
    if (!grid || !Number.isFinite(inset) || inset <= 0) return { x: sp.x, boundary: false };
    const cols = Number(grid.columns) || 10;
    const pitch = Number(grid.pitch) || 3;
    const lotHalf = Number((this.manifest.lot || {}).x_max);
    if (!Number.isFinite(lotHalf)) return { x: sp.x, boundary: false };
    const edgeAbs = lotHalf - pitch / 2;                 // 贴墙列 |x|（如 36-1.5=34.5）
    if (Math.abs(Math.abs(sp.x) - edgeAbs) > 0.01) return { x: sp.x, boundary: false };
    const inward = -Math.sign(sp.x) || 1;                // 朝场内方向
    const x = sp.x + inward * Math.min(inset, pitch);    // 内收不超过一个车位宽
    return { x, boundary: true };
  }

  /**
   * 入场最后一段：车道 → 横移 1.625 m → 倒车入库 Bezier（终点=车位中心，reverse=true）。
   * @returns {Array<object>} 3 条腿
   */
  _entryStallLegs(sp, curveOverride = null, safeTurns = false, access = null) {
    const acc = access || this.spaceAccess(sp.no);
    const laneTz = toThreeZ(acc.entryLaneY);
    const aisleTz = toThreeZ(acc.aisleY);
    const stallTz = toThreeZ(sp.y);
    const dirZ = Math.sign(stallTz - aisleTz) || 1;
    const dirX = acc.entryDir;
    let L = Number.isFinite(Number(curveOverride)) ? Number(curveOverride) : this.opts.stallCurveM;
    if (safeTurns && !this._stallManeuverX(sp).boundary) L = Math.min(L, 1.0);
    const T = safeTurns && this._stallManeuverX(sp).boundary ? 3.0 : this.opts.stallTangentM;
    const node = this.nodes.get(acc.entryNode);
    // 边界列：机动横向基准内收 + 弧线收紧（车位落点/朝向不变，见 _stallManeuverX）
    const man = this._stallManeuverX(sp);
    const mx = man.x;
    if (man.boundary) L = Number(this.opts.boundaryStallCurveM) || L;
    const legs = [];
    if(safeTurns) {
      const startZ=acc.safeNear?aisleTz+acc.entryDir*.20:laneTz,dx=mx-node.x;
      legs.push(bezierLeg({x:node.x,z:startZ},{x:node.x+dx*.4,z:startZ},
        {x:mx-dx*.4,z:aisleTz},{x:mx,z:aisleTz}));
    } else {
    // ① 车道节点 → 机动基准 x（普通车位为 1.5 m 级连接段；边界列两者重合 ⇒ 无此段）
    if (Math.abs(node.x - mx) > 1e-6) {
      legs.push(lineLeg({ x: node.x, z: laneTz }, { x: mx, z: laneTz }));
    }
    // ② 横移：车道中心 → 支路中心线
    legs.push(lineLeg({ x: mx, z: laneTz }, { x: mx, z: aisleTz }));
    }
    // ③ 倒车入库 Bezier：
    //    起点在支路中心线、初始沿支路方向**后退**（p1-p0 = -dirX）；终点速度方向必须**指向车位**
    //    （p3-p2 = dirZ），这样 reverse 反推出的车头朝向 = 车位的 layout rotation（车头朝支路）。
    //    注意 p2 用 `stallTz - dirZ*T`（不是 +）：p2 在车位口的"来向"一侧。
    //    边界列：起点 x = mx（内收），终点 x = sp.x（车位中心）⇒ 曲线带横向摆入。
    legs.push(bezierLeg(
      { x: mx, z: aisleTz },
      { x: mx - dirX * L, z: aisleTz },
      { x: sp.x, z: stallTz - dirZ * T },
      { x: sp.x, z: stallTz },
      { reverse: true },
    ));
    return legs;
  }

  /** 出场前面一段：前行出库 Bezier → 横移 → 出口车道节点。 */
  _exitStallLegs(sp, curveOverride = null, safeTurns = false, access = null) {
    const acc = access || this.spaceAccess(sp.no);

    const laneTz = toThreeZ(acc.exitLaneY);
    const aisleTz = toThreeZ(acc.aisleY);
    const stallTz = toThreeZ(sp.y);
    const dirZ = Math.sign(stallTz - aisleTz) || 1;
    const dirX = acc.exitDir;
    let L = Number.isFinite(Number(curveOverride)) ? Number(curveOverride) : this.opts.stallCurveM;
    const T = safeTurns && this._stallManeuverX(sp).boundary ? 3.0 : this.opts.stallTangentM;
    const node = this.nodes.get(acc.exitNode);
    const man = this._stallManeuverX(sp);
    const mx = man.x;
    if (man.boundary) L = Number(this.opts.boundaryStallCurveM) || L;
    const legs = [];
    if(safeTurns) {
      const endX=node.tx,startX=man.boundary?mx:sp.x,distance=dirX*(endX-startX);
      const startZ=distance<8?aisleTz+dirX*.20:aisleTz;
      const initial=man.boundary?bezierLeg({x:sp.x,z:stallTz},{x:sp.x,z:stallTz-dirZ*T},
        {x:mx-dirX*L,z:startZ},{x:mx,z:startZ}):lineLeg({x:sp.x,z:stallTz},{x:sp.x,z:startZ});
      const endZ=distance<8?startZ:laneTz;
      return [initial,bezierLeg({x:startX,z:startZ},{x:startX+dirX*distance*.4,z:startZ},
        {x:endX-dirX*distance*.4,z:endZ},{x:endX,z:endZ})];
    }
    legs.push(bezierLeg(
      { x: sp.x, z: stallTz },
      { x: sp.x, z: stallTz - dirZ * T },
      { x: mx - dirX * L, z: aisleTz },
      { x: mx, z: aisleTz },
    ));
    legs.push(lineLeg({ x: mx, z: aisleTz }, { x: mx, z: laneTz }));
    if (Math.abs(mx - node.x) > 1e-6) {
      legs.push(lineLeg({ x: mx, z: laneTz }, { x: node.x, z: laneTz }));
    }
    return legs;
  }

  // ------------------------------ 对外主接口 ------------------------------
  /**
   * 由 manifest 自动生成 80 个车位（编号与 server/layout/generator.py 一致：
   * 区内跨排连续编号 01..10 = 第一排、11..20 = 第二排）。
   * 供 preview 模式（未拿到 /api/layout）与单测使用。
   * @param {object} manifest
   * @returns {Array<{space_no:string, zone:string, x:number, z:number, rotation:number}>}
   */
  static spacesFromManifest(manifest) {
    const grid = (manifest && manifest.space_grid) || {};
    const out = [];
    for (const zone of ['A', 'B', 'C', 'D']) {
      const g = grid[zone];
      if (!g) continue;
      const cols = Number(g.columns) || 10;
      const pitch = Number(g.pitch) || 3;
      const x0 = Number(g.x_start);
      let idx = 0;
      for (const row of g.rows || []) {
        for (let k = 0; k < cols; k++) {
          idx += 1;
          out.push({
            space_no: `${zone}-${String(idx).padStart(2, '0')}`,
            zone,
            x: Math.round((x0 + pitch * (k + 0.5)) * 1e6) / 1e6,
            z: Number(row.y),
            rotation: Number(row.rotation) || 0,
          });
        }
      }
    }
    return out;
  }

  /**
   * 入场路径。
   * @param {string} spaceNo
   * @param {{from?:'apron'|'stop', access?:boolean}} [opts]
   * @returns {{spaceNo:string, legs:object[], zones:string[], length:number,
   *            graphLength:number, accessLength:number, accessFrom:number,
   *            from:string, nodes:string[], nodeIds:string[]}}
   */
  planEntry(spaceNo, opts = {}) {
    const sp = this.space(spaceNo);
    const from = opts.from === 'stop' ? 'IN_STOP' : 'IN_APRON';
    const chain = this._chains(sp.zone).entry;
    let acc=this.spaceAccess(sp.no);
    if(opts.safeTurns){
      const mx=this._stallManeuverX(sp).x;
      const node=chain.filter(n=>acc.entryDir*(mx-n.x)>=8).at(-1);
      const chosen=node||chain[0];
      acc={...acc,entryNode:chosen.id,entryNodeX:chosen.x,safeNear:!node};
    }
    const seq = this._nodePath(from, opts.safeTurns?acc.entryNode:this.approachNode(sp.no));
    if (!seq) throw new Error(`RoutePlanner: 入闸到车位 ${sp.no} 不可达`);
    const graphLegs = this._legsOfNodes(seq,opts.safeTurns);
    if(opts.safeTurns&&acc.safeNear){
      const turn=graphLegs.find(l=>l.kind==='bezier'&&Math.abs(Math.abs(l.p3.x)-6)<1e-6);
      if(!turn)throw new Error('RoutePlanner: safe entry plaza connection missing');
      turn.p1.z=turn.p2.z=turn.p3.z=-acc.aisleY+acc.entryDir*.20;
    }
    // 按车长微调起点：车心停止点 = entryStopZ(len)，比 manifest 的 IN_STOP（van 口径）更靠近闸
    if (Number.isFinite(Number(opts.startZ))) {
      trimFirstLegZ(graphLegs, Number(opts.startZ));
    }
    const graphEdges = this._edgesOfPath(seq);
    const withAccess = opts.access !== false;
    // 单次覆盖：邻位占用情况可由调度器传入更保守的弧线长度（默认用 opts.stallCurveM）
    const curve = Number.isFinite(Number(opts.stallCurveM)) ? Number(opts.stallCurveM) : this.opts.stallCurveM;
    const accessLegs = withAccess ? this._entryStallLegs(sp, curve, opts.safeTurns, acc) : [];
    const graphM = mergeLines(cleanLegs(graphLegs));
    const accessM = mergeLines(cleanLegs(accessLegs));
    const legs = [...graphM, ...accessM];
    const zones = [];
    for (let i = 0; i < graphLegs.length; i++) {
      if(opts.safeTurns) {
        for(const zone of this.runtimePlazasForLeg(graphLegs[i])) zones.push(zone);
        for(const zone of graphLegs[i].zones || []) if(zone==='Z1'||zone==='Z2') zones.push(zone);
      } else for (const z of graphLegs[i].zones || []) zones.push(z);
      // 派生区（分岔区中央 ZN/ZS）：转向轨迹穿过中央带时占用，直行不占用
      if(!opts.safeTurns) for (const z of this.cz.derivedZonesForLeg(graphLegs[i])) zones.push(z);
    }
    if(opts.safeTurns && withAccess) zones.push(`W_${sp.zone}`);
    if (withAccess) zones.push(this.cz.stallZone(sp).id);
    return {
      spaceNo: sp.no,
      legs,
      zones: uniq(zones),
      length: round3(this.length(legs)),
      graphLength: round3(this.length(graphM)),
      accessLength: round3(this.length(accessM)),
      accessFrom: graphM.length,
      from,
      nodes: seq.map((id) => this.nodes.get(id)),
      nodeIds: seq,
      chainLength: chain.length,
    };
  }

  /**
   * 出场路径。
   * @param {string} spaceNo
   * @param {{to?:'out'|'stop', access?:boolean}} [opts]
   * @returns {{spaceNo:string, legs:object[], zones:string[], length:number,
   *            graphLength:number, accessLength:number, accessTo:number,
   *            to:string, nodeIds:string[]}}
   */
  planExit(spaceNo, opts = {}) {
    const sp = this.space(spaceNo);
    const to = opts.to === 'stop' ? 'X_STOP' : 'X_OUT';
    let acc = this.spaceAccess(sp.no);
    if(opts.safeTurns) {
      const startX=this._stallManeuverX(sp).x;
      const chain=this._chains(sp.zone).exit;
      const node=chain.find(n=>acc.exitDir*(n.x-startX)>=8)||chain[chain.length-1];
      acc={...acc,exitNode:node.id,exitNodeX:node.x};
    }
    const seq = this._nodePath(to, acc.exitNode, true);
    if (!seq) throw new Error(`RoutePlanner: 车位 ${sp.no} 到出口不可达`);
    const graphLegs = this._legsOfNodes(seq,opts.safeTurns);
    // 按车长微调终点：车心停止点 = exitStopZ(len)，比 manifest 的 X_STOP（van 口径）更远离闸
    if (to === 'X_STOP' && Number.isFinite(Number(opts.endZ))) {
      trimLastLegZ(graphLegs, Number(opts.endZ));
    }
    if(opts.safeTurns && !this._stallManeuverX(sp).boundary && acc.exitDir*(acc.exitNodeX-sp.x)<8) {
      // Join the plaza directly. Keeping the original lane connector here
      // made the car travel to the old lane and reverse over the same segment.
      const turn=graphLegs.findIndex(l=>l.kind==='bezier'&&Math.abs(Math.abs(l.p0.x)-6)<1e-6&&Math.abs(Math.abs(l.p3.x)-3.5)<1e-6);
      if(turn<0)throw new Error('RoutePlanner: safe exit plaza connection missing');
      graphLegs.splice(0,turn);
      graphLegs[0].p0.z=graphLegs[0].p1.z=-acc.aisleY+acc.exitDir*.20;
    }
    const graphEdges = this._edgesOfPath(seq);
    const withAccess = opts.access !== false;
    const curve = Number.isFinite(Number(opts.stallCurveM)) ? Number(opts.stallCurveM) : this.opts.stallCurveM;
    const accessLegs = withAccess ? this._exitStallLegs(sp, curve, opts.safeTurns, acc) : [];
    const graphM = mergeLines(cleanLegs(graphLegs));
    const accessM = mergeLines(cleanLegs(accessLegs));
    const legs = [...accessM, ...graphM];
    const zones = [];
    if (withAccess) zones.push(this.cz.stallZone(sp).id);
    if(opts.safeTurns && withAccess) zones.push(`W_${sp.zone}`);
    for (let i = 0; i < graphLegs.length; i++) {
      if(opts.safeTurns) {
        for(const zone of this.runtimePlazasForLeg(graphLegs[i])) zones.push(zone);
        for(const zone of graphLegs[i].zones || []) if(zone==='Z1'||zone==='Z2') zones.push(zone);
      } else for (const z of graphLegs[i].zones || []) zones.push(z);
      if(!opts.safeTurns) for (const z of this.cz.derivedZonesForLeg(graphLegs[i])) zones.push(z);
    }
    return {
      spaceNo: sp.no,
      legs,
      zones: uniq(zones),
      length: round3(this.length(legs)),
      graphLength: round3(this.length(graphM)),
      accessLength: round3(this.length(accessM)),
      accessTo: accessM.length,
      to,
      nodeIds: seq,
    };
  }

  runtimePlazasForLeg(leg) {
    const points=legPolyline(leg), result=[];
    for(const [id,key] of [['JT_N','n_y'],['JT_S','s_y']]) {
      const z=-this.manifest.aisles[key];
      if(points.some(p=>p.x>=-9 && p.x<=9 && p.z>=z-3.25 && p.z<=z+3.25) ||
        points.some((p,i)=>i && Math.min(p.z,points[i-1].z)<=z+3.25 && Math.max(p.z,points[i-1].z)>=z-3.25 && Math.min(p.x,points[i-1].x)<=9 && Math.max(p.x,points[i-1].x)>=-9)) result.push(id);
    }
    // A long northbound/southbound line must reserve plazas in travel order.
    if(points.at(-1)?.z < points[0]?.z) result.reverse();
    return result;
  }

  /** 车辆朝向（three rotation.y）：沿 (dx,dz) 行驶时 atan2(-dx,-dz)。 */
  static headingTo(dx, dz) { return Math.atan2(-dx, -dz); }

  /** 路径总长（line 精确 / arc 解析 / bezier 采样 32 段）。 */
  length(legs) {
    let s = 0;
    for (const l of legs || []) s += legLength(l);
    return s;
  }

  /** 转成 three 折线（调试/统计用）。 */
  toPolyline(legs, sampleOpts = {}) {
    const pts = [];
    for (const l of legs || []) {
      const seg = legPolyline(l, sampleOpts);
      for (let i = 0; i < seg.length; i++) {
        if (i === 0 && pts.length && Math.hypot(seg[i].x - pts[pts.length - 1].x, seg[i].z - pts[pts.length - 1].z) < 1e-9) continue;
        pts.push(seg[i]);
      }
    }
    return pts;
  }

  /** 所有车位号。 */
  spaceNos() { return [...this._spaces.keys()]; }

  /** 全部边（只读）。 */
  edgeList() { return [...this.edges.values()]; }

  /** 统计信息（调试）。 */
  describe() {
    return {
      nodes: this.nodes.size,
      edges: this.edges.size,
      spaces: this._spaces.size,
      zones: this.cz.staticOrder.concat(['Z7']),
      entryStopZ: this.entryStopZ(),
      exitStopZ: this.exitStopZ(),
    };
  }
}

/** 简单去重保序。 */
function uniq(arr) {
  const out = [];
  const seen = new Set();
  for (const v of arr) {
    if (!seen.has(v)) { seen.add(v); out.push(v); }
  }
  return out;
}

/**
 * 把路径起点沿第一条"南北向"直线腿收缩到 startZ（three z）。
 * 仅当 startZ 落在 [leg.bz, leg.az] 区间内才生效（入场：车心比 manifest 的 IN_STOP 更靠近闸）。
 */
function trimFirstLegZ(legs, startZ) {
  const l = legs[0];
  if (!l || l.kind !== 'line' || Math.abs(l.ax - l.bx) > 1e-6) return false;
  const lo = Math.min(l.az, l.bz), hi = Math.max(l.az, l.bz);
  if (!(startZ >= lo - 1e-6 && startZ <= hi + 1e-6)) return false;
  if (Math.abs(l.az - l.bz) < 1e-9) return false;
  l.az = r6(startZ);
  return true;
}

/**
 * 把路径终点沿最后一条"南北向"直线腿外延/收缩到 endZ（three z）。
 * 出口车心随车长变化：车的车头固定在停止线漆前 0.3 m，车越短车心越靠近闸
 * （three z 越大）⇒ endZ 可能比 manifest 的 X_STOP（van 口径）更靠前，需要**外延**。
 * 允许收缩（仍在段内）或外延 ≤10 m；异常值直接忽略。
 */
function trimLastLegZ(legs, endZ) {
  const l = legs[legs.length - 1];
  if (!l || l.kind !== 'line' || Math.abs(l.ax - l.bx) > 1e-6) return false;
  const travel = Math.sign(l.bz - l.az);
  if (travel === 0) return false;
  const delta = endZ - l.bz;
  if (delta * travel < -1e-6) {
    // 收缩：新端点必须仍在线段内
    if (Math.abs(delta) > Math.abs(l.bz - l.az) + 1e-6) return false;
  } else if (delta * travel > 10) {
    return false;
  }
  l.bz = r6(endZ);
  return true;
}

const round3 = (v) => Math.round(Number(v) * 1000) / 1000;

export { polylineLength, legLength, legPolyline };
export default RoutePlanner;
