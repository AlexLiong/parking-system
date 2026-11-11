// 路网门面（sim/PathNetwork.js）—— Route V2
//
// 纯逻辑模块：**不 import three**，可在 node --test 下直接单测。
//
// 职责：
//   1) 对外保留旧 API 名（entryApproach / entryToSpace / spaceToExitStop / exitContinue /
//      entryStop / exitStop / entryGate / exitGate / configure / aisleZ / outerZ），
//      内部**全部委托** RoutePlanner（不再使用旧折线逻辑）。
//   2) 暴露 v2 只读属性：`planner` / `reservations` / `zones` / `manifest`，供 three-scene
//      装配与 three-scheduler 集成（契约见 _qa_audit/route_v2/contract.md）。
//   3) 支持两阶段注入：`new PathNetwork({manifest, spaces})` 或
//      `new PathNetwork(sceneCfg, gatesCfg)` 之后 `paths.configure({manifest, spaces})`。
//      布局热更新用 `paths.setSpaces(spaces)`（保留 planner / reservations）。
//
// 兼容策略：
//   - 旧方法默认返回**旧格式 legs**（{ax,az,bx,bz,reverse}），bezier 已采样成多段直线，
//     因此现有 `Vehicle.setLegs` 可原样工作；
//   - 需要新格式（含 bezier/arc）时用 `planEntryRich()` / `planExitRich()`
//     或直接 `paths.planner.planEntry(spaceNo)`。

import { RoutePlanner } from './RoutePlanner.js';
import { ReservationTable } from './ReservationTable.js';
import { buildConflictZones, legPolyline, legLength } from './ConflictZones.js';

const r6 = (v) => Math.round(Number(v) * 1e6) / 1e6;

const OPT_KEYS = ['manifest', 'spaces', 'sceneCfg', 'gatesCfg', 'options'];

/** 判断参数是否是"对象式配置"（而非旧的 sceneCfg）。 */
function isOptionsArg(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false;
  return OPT_KEYS.some((k) => k in v);
}

export class PathNetwork {
  /**
   * 两种签名都支持：
   *   new PathNetwork({ manifest, spaces, sceneCfg, gatesCfg, options })
   *   new PathNetwork(sceneCfg, gatesCfg)          // 旧式，随后 configure({manifest, spaces})
   */
  constructor(a = {}, b = {}, c = {}) {
    this.sceneCfg = {};
    this.gatesCfg = {};
    this.options = {};
    this._manifest = null;
    this._planner = null;
    this._zones = null;
    this._reservations = null;
    this._spaces = [];
    if (isOptionsArg(a)) this.configure(a);
    else {
      this.sceneCfg = { ...(a || {}) };
      this.gatesCfg = { ...(b || {}) };
      this.options = { ...(c || {}) };
    }
  }

  // ==========================================================================
  // 装配 / 热更新
  // ==========================================================================
  /**
   * 配置入口。两种形式：
   *   configure(sceneCfg, gatesCfg)                                 // 旧式：只更新兜底字段
   *   configure({manifest, spaces, sceneCfg, gatesCfg, options})    // 新式：可全量重建
   * @returns {this}
   */
  configure(a = {}, b = {}, c = {}) {
    if (isOptionsArg(a)) {
      if (a.sceneCfg) this.sceneCfg = { ...this.sceneCfg, ...a.sceneCfg };
      if (a.gatesCfg) this.gatesCfg = { ...this.gatesCfg, ...a.gatesCfg };
      if (a.options) this.options = { ...this.options, ...a.options };
      if (a.manifest) this._attach(a.manifest, a.spaces);
      else if (a.spaces) this.setSpaces(a.spaces);
    } else {
      this.sceneCfg = { ...(a || {}) };
      this.gatesCfg = { ...(b || {}) };
      if (c && typeof c === 'object' && !Array.isArray(c)) this.options = { ...this.options, ...c };
    }
    return this;
  }

  /**
   * 注入 manifest（+ 可选 spaces），全量重建 planner 与 reservations。
   * @param {object} manifest lot_v2.json 解析对象
   * @param {Array<object>} [spaces] SpaceSpec[]；省略则用 manifest 自动生成 80 位
   * @returns {this}
   */
  setManifest(manifest, spaces = null) {
    if (!manifest || !Array.isArray(manifest.nodes)) {
      throw new Error('PathNetwork.setManifest: manifest 非法（缺少 nodes）');
    }
    this._manifest = manifest;
    this._zones = buildConflictZones(manifest);
    const list = (spaces && spaces.length) ? spaces : RoutePlanner.spacesFromManifest(manifest);
    this._spaces = list;
    this._planner = new RoutePlanner(manifest, list, this.options);
    this._reservations = new ReservationTable(this._zones);
    return this;
  }

  /** attach 的别名（语义：把 layout 数据挂到门面上）。 */
  attach(manifest, spaces = null) { return this.setManifest(manifest, spaces); }
  _attach(manifest, spaces = null) { return this.setManifest(manifest, spaces); }

  /**
   * 布局热更新：只换车位表，保留 planner 的图与 reservations。
   * 传空数组时回退为 manifest 自动生成的 80 位。
   * @returns {this}
   */
  setSpaces(spaces = []) {
    const list = (spaces && spaces.length)
      ? spaces
      : (this._manifest ? RoutePlanner.spacesFromManifest(this._manifest) : []);
    this._spaces = list;
    if (this._planner) this._planner.setSpaces(list);
    return this;
  }

  /** 当前车位表（只读副本）。 */
  get spaces() { return this._spaces.slice(); }

  // ==========================================================================
  // v2 只读属性（three-scene / three-scheduler 集成用）
  // ==========================================================================
  /** manifest 对象本体（含 manifest_hash / road_graph_version）；未注入时 null。 */
  get manifest() { return this._manifest; }
  /** RoutePlanner 实例；未注入 manifest 时 null。 */
  get planner() { return this._planner; }
  /** ReservationTable 实例（含 snapshot()）；未注入 manifest 时 null。 */
  get reservations() { return this._reservations; }
  /** buildConflictZones(manifest) 的返回结构（zones/rect/rectThree/zonesForLeg/stallZone…）。 */
  get zones() { return this._zones; }
  /** manifest 是否已就绪。 */
  get manifestReady() { return !!this._manifest && !!this._planner; }
  /** 当前路径是否由 planner 生成（false = 尚未注入 manifest）。 */
  get usingPlanner() { return !!this._planner; }
  /** 外场道路中心 three z（= -outer_road.y），旧代码用它算出生点。 */
  get outerZ() {
    const m = this._manifest;
    if (m && m.outer_road && Number.isFinite(Number(m.outer_road.y))) return r6(-Number(m.outer_road.y));
    const legacy = this.sceneCfg ? this.sceneCfg.outer_road_y : null;
    return Number.isFinite(Number(legacy)) ? r6(-Number(legacy)) : 48.5;
  }
  /** 入口车道 x（three，= Blender x）。 */
  get entryX() {
    const m = this._manifest;
    if (m && m.corridor) return Number(m.corridor.in_x);
    return Number(this.sceneCfg.entry_gate_x ?? -3.5);
  }
  /** 出口车道 x（three）。 */
  get exitX() {
    const m = this._manifest;
    if (m && m.corridor) return Number(m.corridor.out_x);
    return Number(this.sceneCfg.exit_gate_x ?? 3.5);
  }

  _requirePlanner(what) {
    if (!this._planner) {
      throw new Error(`PathNetwork: ${what} 需要先注入 manifest（new PathNetwork({manifest}) 或 configure({manifest})）`);
    }
    return this._planner;
  }

  // ==========================================================================
  // 旧 API（保留名字，内部委托 planner；返回旧格式 legs）
  // ==========================================================================
  /** 入场接近：外场排队 → 入口停止线（默认从外场道路中心起）。 */
  entryApproach(spawnZ = null) {
    const p = this._requirePlanner('entryApproach');
    const z0 = spawnZ != null ? Number(spawnZ) : this.outerZ;
    return this.toOldLegs(p.planApproach(z0));
  }

  /** 入场：停止线 → 车位中心（space 可为场景对象或 planner 口径）。 */
  entryToSpace(space, opts = {}) {
    const p = this._requirePlanner('entryToSpace');
    const no = this.resolveSpaceNo(space);
    const res = p.planEntry(no, { from: 'stop', startZ: opts.startZ });
    return opts.rich ? res : this.toOldLegs(res.legs, opts.samples);
  }

  /** 出场：车位中心 → 出口停止线。 */
  spaceToExitStop(space, opts = {}) {
    const p = this._requirePlanner('spaceToExitStop');
    const no = this.resolveSpaceNo(space);
    const res = p.planExit(no, { to: 'stop', endZ: opts.endZ });
    return opts.rich ? res : this.toOldLegs(res.legs, opts.samples);
  }

  /** 出场缴费后：停止线 → 杆 → 外场（X_STOP → X_GATE → X_OUT）。 */
  exitContinue() {
    const p = this._requirePlanner('exitContinue');
    return this.toOldLegs(p.planExitGate());
  }

  /** 入口停止线车心位置（three）。len 省略时取最长车型。 */
  entryStop(len = null) {
    const p = this._planner;
    const L = len == null ? (p ? p.opts.maxLen : 5.2) : Number(len);
    const m = this._manifest;
    const g = m && m.gates ? m.gates.in : null;
    const gy = g ? Number(g.y) : Number(this.sceneCfg.gate_y ?? -25.5);
    const clear = g && Number.isFinite(Number(g.stop_clearance_m)) ? Number(g.stop_clearance_m) : 0.3;
    return { x: this.entryX, z: r6(-gy + clear + L / 2) };
  }

  /** 出口停止线车心位置（three）。 */
  exitStop(len = null) {
    const p = this._planner;
    const L = len == null ? (p ? p.opts.maxLen : 5.2) : Number(len);
    const m = this._manifest;
    const g = m && m.gates ? m.gates.out : null;
    const gy = g ? Number(g.y) : Number(this.sceneCfg.gate_y ?? -25.5);
    const clear = g && Number.isFinite(Number(g.stop_clearance_m)) ? Number(g.stop_clearance_m) : 0.3;
    return { x: this.exitX, z: r6(-gy - clear - L / 2) };
  }

  /** 入口杆平面中心（three）。 */
  entryGate() {
    const m = this._manifest;
    const y = m && m.gates ? Number(m.gates.in.y) : Number(this.sceneCfg.gate_y ?? -25.5);
    return { x: this.entryX, z: r6(-y) };
  }

  /** 出口杆平面中心（three）。 */
  exitGate() {
    const m = this._manifest;
    const y = m && m.gates ? Number(m.gates.out.y) : Number(this.sceneCfg.gate_y ?? -25.5);
    return { x: this.exitX, z: r6(-y) };
  }

  /** 旧辅助：由车位的 three z 推出其所在支路中心线的 three z。 */
  aisleZ(spaceZ) {
    const m = this._manifest;
    const z = Number(spaceZ);
    if (m && m.aisles) {
      const y = z < 0 ? Number(m.aisles.n_y) : Number(m.aisles.s_y);
      return r6(-y);
    }
    return z < 0 ? -19.5 : 19.5;
  }

  /** 出生点 three z（外场道路中心 + 偏移）。 */
  spawnZ(off = 0) { return r6(this.outerZ + Number(off || 0)); }

  // ==========================================================================
  // v2 新 API（rich legs）
  // ==========================================================================
  /** 入场 rich 结果：{spaceNo, legs(含 bezier), zones, length, nodeIds, ...} */
  planEntryRich(spaceNo, opts = {}) {
    return this._requirePlanner('planEntryRich').planEntry(spaceNo, opts);
  }

  /** 出场 rich 结果。 */
  planExitRich(spaceNo, opts = {}) {
    return this._requirePlanner('planExitRich').planExit(spaceNo, opts);
  }

  /** 某车位支路访问信息（车道 y / 方向 / 入口节点 / 出口节点）。 */
  spaceAccess(spaceNo) {
    return this._requirePlanner('spaceAccess').spaceAccess(spaceNo);
  }

  /** 某车位的动态冲突区（Z7:<spaceNo>）。 */
  stallZone(spaceNo) {
    if (!this._zones || !this._planner) return null;
    const sp = this._planner.space(spaceNo);
    return this._zones.stallZone({ space_no: sp.no, zone: sp.zone, x: sp.x, z: sp.y });
  }

  /** 路径长度（rich legs）。 */
  length(legs) {
    let s = 0;
    for (const l of legs || []) s += legLength(l);
    return s;
  }

  // ==========================================================================
  // 工具
  // ==========================================================================
  /**
   * 把 rich legs 采样成旧格式 legs（{ax,az,bx,bz,reverse}）。
   * @param {Array<object>} legs
   * @param {number} [samples] bezier/arc 每段采样数
   */
  toOldLegs(legs, samples = 16) {
    const out = [];
    for (const leg of legs || []) {
      const pts = legPolyline(leg, { bezier: samples, arc: samples });
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i];
        if (Math.hypot(b.x - a.x, b.z - a.z) < 1e-6) continue;
        out.push({
          ax: r6(a.x), az: r6(a.z), bx: r6(b.x), bz: r6(b.z),
          reverse: !!leg.reverse,
        });
      }
    }
    return out;
  }

  /**
   * 解析车位号：支持
   *   - planner 口径对象 {space_no, x, z=Blender y}
   *   - 场景对象 {no, x, z=three z}
   *   - 字符串 'A-01'
   */
  resolveSpaceNo(space) {
    if (typeof space === 'string') {
      if (!this._planner || !this._planner.hasSpace(space)) {
        throw new Error(`PathNetwork: 未知车位 ${space}`);
      }
      return space;
    }
    if (!space || typeof space !== 'object') throw new Error('PathNetwork: 无效的车位参数');
    const no = space.space_no ?? space.no;
    if (no && this._planner && this._planner.hasSpace(no)) return String(no);
    const x = Number(space.x);
    const z = Number(space.z);
    if (this._planner && Number.isFinite(x) && Number.isFinite(z)) {
      const nos = this._planner.spaceNos();
      // 先按 planner 口径（z=Blender y），再按场景口径（z=three z）
      for (const cand of [{ y: z }, { y: -z }]) {
        for (const n of nos) {
          const sp = this._planner.space(n);
          if (Math.abs(sp.x - x) < 0.5 && Math.abs(sp.y - cand.y) < 0.5) return n;
        }
      }
    }
    if (no) throw new Error(`PathNetwork: planner 中不存在车位 ${no}`);
    throw new Error(`PathNetwork: 无法解析车位 ${JSON.stringify({ x: space.x, z: space.z })}`);
  }

  /** 调试摘要。 */
  describe() {
    return {
      manifestReady: this.manifestReady,
      hash: this._manifest ? this._manifest.manifest_hash : null,
      roadGraphVersion: this._manifest ? this._manifest.road_graph_version : null,
      planner: this._planner ? this._planner.describe() : null,
      spaces: this._spaces.length,
      zones: this._zones ? this._zones.staticOrder.concat(['Z7']) : null,
      outerZ: this.outerZ,
    };
  }
}

export default PathNetwork;
export { RoutePlanner, ReservationTable, buildConflictZones };
