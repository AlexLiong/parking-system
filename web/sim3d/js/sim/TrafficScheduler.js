// 交通调度器（sim/TrafficScheduler.js）—— Route-v2 重写。
//
// 职责：管理全部车辆的生命周期状态机（入场排队→停止线→越闸→入库→出场→驶离），
// 每帧按 **RoutePlanner 的冲突区序列 + ReservationTable 的时间窗** 做速度门控与仲裁，
// 并向后端上报**物理事实**（到达 / 杆区占用 / 越闸 / 驶离）。开闸许可仍由后端裁决。
//
// Route-v2 关键改造（对照审查报告 §8.7 / §8.8 与 `_qa_audit/route_v2/contract.md`）：
//   1) 路径来自 planner（line/arc/bezier），不再使用旧 PathNetwork 折线；
//   2) 入场在停止线等 **Z1 预约 + 道闸许可** 双门控；出场在停止线等 **Z2 预约 + 缴费放行**；
//   3) 每帧 `_reserveNext` / `_releaseOnExit`：按 planner.zones 顺序预约下一冲突区，
//      BLOCKED 时在冲突区外停车；分岔区(Z3~Z6)/杆区(Z1,Z2)/车位机动区(Z7) 互斥由预约表串行化；
//   4) 入口排队限流：场内排队 ≤ `queueMax`（默认 3）辆，满则外场等待（hold_entry）；
//   5) 停止线按车长：`planner.entryStopZ(len)` / `planner.exitStopZ(len)`；闸区按**车头**判定；
//   6) 静态碰撞体安全网（scene.staticColliders / scene.colliders）；
//   7) 新路网无对向共线，删除“对向车忽略”逻辑；交叉冲突一律由预约串行化；
//   8) 保留 arrived/passed/left/zone 上报与 AABB 最后防线；提供 reset()/hydrate() 占位接口。
//
// 兼容：旧 main.js 签名 `new TrafficScheduler({scene, paths, onEvent})` 且 paths 仍是旧
// PathNetwork（无 planEntry）时，自动进入 legacy 折线模式并 console.warn 一次（分阶段集成用）。
import { Vehicle } from '../entities/Vehicle.js';
import { Collider } from '../entities/Collider.js';
import { CAR_MODELS, PLATE_IMAGES, KIN, ZONE_HALF } from '../config.js';
import { Rest } from '../api.js';

const DEFAULTS = { straightSpeed: 2.5, minFollowGap: 1.0, turnSpeed: 1.2 };
const QK = (k) => (typeof KIN?.[k] === 'number' ? KIN[k] : DEFAULTS[k]);

const QUEUE_MAX_ENTRY = 3;       // 入口场内排队上限（辆）——超出后外场等待
// R-04：贴墙边界车位停放后的**视觉-only**向场内偏移（m）；OBB/路线/车位中心不变。
const BOUNDARY_VISUAL_SHIFT = 0.12;
const ZONE_LOOKAHEAD_S = 8;      // 提前多少秒预约冲突区
const ZONE_STOP_MARGIN = 2.0;    // 未获预约时，距冲突区边界 ≤ 该距离必须停车（“停在区外”）
const TWO_PHASE_DIST = 1.2;      // R1：进入当前区前，下一区预约必须已授予（两段式）
const ZONE_RELEASE_PAD = 0.25;   // 车盒完全离开冲突区（含余量）才释放
const WARN_INTERVAL = 4;         // 同类告警节流（秒，仿真时间）

function seeded(seed, state = seed) {
  let value = Number(state) >>> 0;
  const next = () => { value = (value + 0x6D2B79F5) >>> 0; let t = value;
    t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296; };
  next.state = () => value;
  return next;
}

function hyp(dx, dz) { return Math.hypot(dx, dz); }

function angleDiff(a, b) {
  let d = (b - a) % (Math.PI * 2);
  if (d > Math.PI) d -= Math.PI * 2;
  if (d < -Math.PI) d += Math.PI * 2;
  return d;
}

/** planner leg 的起点/终点（three 坐标），兼容 line/arc/bezier/旧折线。 */
function legStartPoint(leg) {
  if (!leg) return null;
  if (leg.kind === 'line' || typeof leg.ax === 'number') return { x: leg.ax, z: leg.az };
  if (leg.kind === 'arc') return { x: leg.cx + leg.r * Math.cos(leg.a0), z: leg.cz + leg.r * Math.sin(leg.a0) };
  if (leg.kind === 'bezier') return { x: leg.p0.x, z: leg.p0.z };
  return null;
}

function legEndPoint(leg) {
  if (!leg) return null;
  if (leg.kind === 'line' || typeof leg.bx === 'number') return { x: leg.bx, z: leg.bz };
  if (leg.kind === 'arc') return { x: leg.cx + leg.r * Math.cos(leg.a1), z: leg.cz + leg.r * Math.sin(leg.a1) };
  if (leg.kind === 'bezier') return { x: leg.p3.x, z: leg.p3.z };
  return null;
}

/**
 * 预约表缺省实现（R3 ReservationTable 未接线时的降级；接口与 contract §3.2 一致）。
 * 语义：时间窗 [tEnter, tEnter+hold*1.3]；同车重复申请幂等；冲突则 BLOCKED 并返回 until。
 * 注意：优先级策略（出口>入口、已进入>请求）由 R3 的 ReservationTable 实现；此处仅 FIFO。
 */
class FallbackReservations {
  constructor() { this._zones = new Map(); }
  _list(zoneId) { return this._zones.get(zoneId) || []; }
  request(vehicleId, zoneId, tEnter, holdSec) {
    const t0 = Number(tEnter) || 0;
    const dur = Math.max(0.2, Number(holdSec) || 1) * 1.3;
    const t1 = t0 + dur;
    const list = this._list(zoneId);
    // 同车窗口：先移除旧窗（刷新）
    for (let i = list.length - 1; i >= 0; i--) if (list[i].vehicleId === vehicleId) list.splice(i, 1);
    let until = t1;
    for (const w of list) {
      if (t0 < w.t1 && t1 > w.t0) { until = Math.max(until, w.t1); }
    }
    if (until > t1 + 1e-9) {
      list.push({ t0, t1: until, vehicleId, pending: true });
      list.sort((a, b) => a.t0 - b.t0);
      return { granted: false, blocked: true, until };
    }
    list.push({ t0, t1, vehicleId, pending: false });
    list.sort((a, b) => a.t0 - b.t0);
    return { granted: true, blocked: false, until: t1 };
  }
  release(vehicleId, zoneId) {
    const list = this._list(zoneId);
    for (let i = list.length - 1; i >= 0; i--) if (list[i].vehicleId === vehicleId) list.splice(i, 1);
  }
  releaseAll(vehicleId) { for (const id of this._zones.keys()) this.release(vehicleId, id); }
  isFree(zoneId, tEnter, holdSec) {
    const t0 = Number(tEnter) || 0;
    const t1 = t0 + Math.max(0.2, Number(holdSec) || 1) * 1.3;
    return this._list(zoneId).every((w) => !(t0 < w.t1 && t1 > w.t0));
  }
  snapshot() {
    const out = {};
    for (const [id, list] of this._zones.entries()) {
      if (list.length) out[id] = list.map((w) => ({ ...w }));
    }
    return out;
  }
}

export class TrafficScheduler {
  /**
   * @param {object} deps
   * @param {import('../scene/ParkingScene.js').ParkingScene} deps.scene
   * @param {object} deps.paths PathNetwork 门面（route-v2）或旧 PathNetwork（legacy）
   * @param {object} [deps.planner] RoutePlanner（缺省取 paths.planner）
   * @param {object} [deps.manifest] lot_v2.json（缺省取 paths.manifest / scene.manifest）
   * @param {object} [deps.reservations] ReservationTable（缺省取 paths.reservations）
   * @param {object} [deps.conflictZones] ConflictZones（缺省取 paths.zones）
   * @param {object} [deps.options] {queueMax, zoneLookaheadS, zoneStopMargin, staticColliders}
   * @param {(e:object)=>void} [deps.onEvent] HUD 事件回调
   */
  constructor({ scene, paths, planner, manifest, onEvent = () => {},
               reservations, conflictZones, options = {} } = {}) {
    this.scene = scene;
    this.paths = paths || null;
    this.assets = scene?.assets || null;
    this.markers = scene?.markers || null;
    this.onEvent = onEvent;

    this.planner = planner || paths?.planner || null;
    this.manifest = manifest || paths?.manifest || scene?.manifest || null;
    this.conflictZones = conflictZones || paths?.zones || paths?.conflictZones || null;
    this.reservations = reservations || paths?.reservations || paths?.resv || null;

    this.options = {
      queueMax: QUEUE_MAX_ENTRY, zoneLookaheadS: ZONE_LOOKAHEAD_S,
      zoneStopMargin: ZONE_STOP_MARGIN, neighborStallCurveM: 1.0, ...options,
    };
    this._legacy = !(this.planner && typeof this.planner.planEntry === 'function')
      && !(this.paths && typeof this.paths.planEntry === 'function');
    if (!this.reservations) {
      this.reservations = new FallbackReservations();
      this._warnGlobal('未接入 ReservationTable，使用内置 FIFO 降级预约表（仅用于分阶段集成/单测）');
    }
    this._zoneCache = new Map();
    this._warned = new Map();
    this._staticColliders = null;

    this.vehicles = new Map();
    this._remoteLoading = new Set();
    this._runtimeEpoch = 0;
    this._seq = 0;
    this.simTime = 0;
    this._queueHoldCount = 0;
    this.seed = Number(options.seed ?? 20261020) >>> 0;
    this._rng = seeded(this.seed);
    this._spawnRequests = [];
    this._queuedIds = new Set();
    this._spawning = false;
    this._gateOccupied = {in:false,out:false};
    this._zoneReports = {in:{ack:false,busy:false,retryAt:0},out:{ack:false,busy:false,retryAt:0}};
    this._entryRetries = [];
    this.ownerId = options.ownerId || null;
    this.sessionId = null;
    this._claimPromise = null;

    this._initInfra();
    if (this._legacy) {
      this._warnGlobal('TrafficScheduler 进入 legacy PathNetwork 模式（无 RoutePlanner.planEntry）；'
        + 'route-v2 正式运行应接入 PathNetwork 门面的 planner/reservations/zones。');
    }
  }

  get barriers() { return this.scene?.gates || {}; }

  // =====================================================================
  // 基础设施：闸口 / 停止线 / 外场 / 车位 / 冲突区
  // =====================================================================
  _initInfra() {
    const m = this.manifest;
    const sg = this.scene?.sceneCfg || {};
    const gatesCfg = this.scene?.gatesCfg || {};
    const legacyEntry = gatesCfg.entry || {};
    const legacyExit = gatesCfg.exit || {};

    const gateBlenderY = m?.gates?.in?.y ?? legacyEntry.y ?? sg.gate_y ?? -25.5;
    const entryX = m?.gates?.in?.x ?? legacyEntry.x ?? sg.entry_gate_x ?? -3.5;
    const entryPaintB = m?.gates?.in?.stop_paint_y ?? legacyEntry.stop_line_y ?? (gateBlenderY + 0.3);
    const exitY = m?.gates?.out?.y ?? legacyExit.y ?? -34.0;
    const exitX = m?.gates?.out?.x ?? legacyExit.x ?? sg.exit_gate_x ?? 3.5;
    const exitPaintB = m?.gates?.out?.stop_paint_y ?? legacyExit.stop_line_y ?? (exitY + 0.3);

    this.entry = { x: entryX, gateZ: -gateBlenderY, paintZ: -entryPaintB };
    this.exit = { x: exitX, gateZ: -exitY, paintZ: -exitPaintB };
    this.outerZ = -(m?.outer_road?.y ?? sg.outer_road_y ?? -46);
    this.apronZ = -(m?.apron?.y_max ?? -30.0);                 // 场前开口（three z）
    const outerWidth = m?.outer_road?.width ?? 6.0;
    // 外场等待点：外场道路南缘再往南 0.5 m，避免占住场前开口
    this.outerSouthZ = this.outerZ + outerWidth / 2 - .2;
    this.holdZ = this.outerSouthZ;

    // 区内支路中心（three z）：用于 Z7 兜底矩形与排队 ETA
    this.aisleZ = {
      n: -(m?.aisles?.n_y ?? 19.5),
      s: -(m?.aisles?.s_y ?? -13.5),
    };
    this.queueEntryFromZ = -(m?.queue?.entry?.from_y ?? -28.4);
    this.queueEntryLength = m?.queue?.entry?.length_m ?? 17.1;

    if (this._legacy) {
      // 旧 PathNetwork：保留其停止线/闸口坐标（scene.gatesCfg 生效率更高）
      try {
        const es = this.paths?.entryStop?.();
        const xs = this.paths?.exitStop?.();
        const eg = this.paths?.entryGate?.();
        const xg = this.paths?.exitGate?.();
        if (es) this.entry.paintZ = es.z;
        if (xs) this.exit.paintZ = xs.z;
        if (eg) this.entry.gateZ = eg.z;
        if (xg) this.exit.gateZ = xg.z;
      } catch (_) { /* ignore */ }
      this.holdZ = this.outerZ + 6;
    }
  }

  _warnGlobal(msg) { console.warn('[TrafficScheduler] ' + msg); }

  _warn(v, msg) {
    const key = `${v?.id || 'global'}:${msg}`;
    const last = this._warned.get(key) ?? -1e9;
    if (this.simTime - last < WARN_INTERVAL) return;
    this._warned.set(key, this.simTime);
    this._emit({ type: 'warn', msg, id: v?.id });
  }

  _emit(e) { try { this.onEvent(e); } catch (_) { /* HUD 不应影响调度 */ } }

  // ---------------- 车位信息 ----------------
  /**
   * R-04：计算边界车位的视觉-only横向偏移。A/B 最西列 → +x；C/D 最东列 → -x；
   * 仅移动渲染模型，不修改车位中心/pos/dims/poseBox/路径/碰撞。
   */
  _boundaryVisualShift(spaceNo) {
    const sp = this._spaceInfo(spaceNo);
    if (!sp || !Number.isFinite(Number(sp.x))) return 0;
    const grid = this.manifest?.space_grid?.[sp.zone];
    if (!grid) return 0;
    const cols = Number(grid.columns) || 0;
    const pitch = Number(grid.pitch) || 3.0;
    const start = Number(grid.x_start);
    if (!cols || !Number.isFinite(start)) return 0;
    const x = Number(sp.x);
    const leftEdge = start + pitch / 2;                       // 西侧最外列中心
    const rightEdge = start + pitch * (cols - 0.5);           // 东侧最外列中心
    const eps = Math.max(0.05, pitch * 0.05);
    const shift = Number(this.options?.boundaryVisualShift ?? BOUNDARY_VISUAL_SHIFT);
    // 只有西侧 A/B 最西列（x<0）与东侧 C/D 最东列（x>0）贴墙；
    // 不能把 C/D 靠中心的西列（如 C-01）误判为边界。
    if (x < 0 && Math.abs(x - leftEdge) <= eps) return Math.abs(shift);   // 西墙 → 朝 +x
    if (x > 0 && Math.abs(x - rightEdge) <= eps) return -Math.abs(shift);  // 东墙 → 朝 -x
    return 0;
  }

  /** 停放边界车位时应用视觉偏移；非停放/非边界自动归零。返回偏移量（m，带方向）。 */
  _syncBoundaryVisualOffset(v) {
    if (!v || typeof v.setVisualOffset !== 'function') return 0;
    const spaceNo = v.meta?.spaceNo;
    const sp = spaceNo ? this._spaceInfo(spaceNo) : null;
    const base = spaceNo ? this._boundaryVisualShift(spaceNo) : 0;
    let active = 0;
    if (base && sp && Number.isFinite(Number(sp.x))) {
      if (v.state === 'parked') {
        active = base;
      } else if (v.state === 'to_space' || v.state === 'to_exit') {
        // 仅在车心进入边界车位附近（含最后倒车/出库扫掠）时偏移，避免整条路径视觉漂移。
        const radius = Number(this.options?.boundaryVisualRadius ?? 6.5);
        const dx = v.pos.x - Number(sp.x);
        const dz = v.pos.z - Number(sp.z);
        if (dx * dx + dz * dz <= radius * radius) active = base;
      }
    }
    v.setVisualOffset(active, 0);
    return active;
  }

  _applyParkedVisualOffset(v) { return this._syncBoundaryVisualOffset(v); }

  /**
   * duration 新语义：从“通过入口道闸”到“通过出口道闸”的总时长。
   * 用本车实际入场行驶时间估算出场行驶时间，反推 startExit 时刻。
   */
  _computeExitSchedule(v, now = this.simTime) {
    const duration = Number(v?.meta?.duration) || 0;
    const parkedAt = Number.isFinite(Number(v?.meta?.parkedAtSim))
      ? Number(v.meta.parkedAtSim) : Number(now) || 0;
    const entryGateAtRaw = Number(v?.meta?.entryGateAtSim);
    const hasEntryGate = Number.isFinite(entryGateAtRaw) && entryGateAtRaw > 0;
    const entryGateAt = hasEntryGate ? entryGateAtRaw : parkedAt;
    const entryTravel = Math.max(0, parkedAt - entryGateAt);
    // 先按实际入场行驶时间估算；无有效入场时间时用车型长度的保守行驶估算。
    const exitTravelEst = entryTravel > 0.2
      ? entryTravel
      : Math.max(4, (Number(v?.dims?.len) || 4.4) * 1.2);
    const scheduledExitAt = duration > 0 ? entryGateAt + duration : null;
    const autoExitAt = duration > 0
      ? Math.max(Number(now) || 0, scheduledExitAt - exitTravelEst)
      : null;
    return { duration, entryGateAt, parkedAt, entryTravel, exitTravelEst,
      scheduledExitAt, autoExitAt, hasEntryGate };
  }

  /** duration 到期：按入口→出口总时长反推的 startExit 时刻；到点 startExit。 */
  _maybeAutoExit(v) {
    if (!v || v.state !== 'parked') return false;
    // null/undefined 表示“不自动出场”；Number(null)=0 会被误判为已到期（e2e 实测）。
    if (v.meta?.autoExitAt == null) return false;
    const due = Number(v.meta.autoExitAt);
    if (!Number.isFinite(due) || this.simTime + 1e-9 < due) return false;
    v.meta.autoExitAt = null;
    const started = this.startExit(v.id);
    if (started) {
      this._emit({ type: 'auto_exit', id: v.id, vid: v.vid, plate: v.meta?.plate || null,
        entryTravel: v.meta?.entryTravel, exitTravelEst: v.meta?.exitTravelEst,
        scheduledExitAt: v.meta?.scheduledExitAt,
        reason: `入口→出口 ${Number(v.meta?.duration) || 0}s 目标 ${Number(v.meta?.scheduledExitAt || 0).toFixed(1)}s 到期` });
    }
    return !!started;
  }

  _spaceInfo(spaceNo) {
    if (!spaceNo) return null;
    const byNo = this.scene?.spaceByNo?.(spaceNo);
    if (byNo && typeof byNo.x === 'number') return byNo;
    // 兜底：由 manifest.space_grid 解析 space_no（如 A-01 → zone A, idx 0）
    const m = this.manifest?.space_grid;
    const mm = /^([A-D])-(\d+)$/i.exec(spaceNo);
    if (!mm || !m) return null;
    const zone = mm[1].toUpperCase();
    const idx = Math.max(0, parseInt(mm[2], 10) - 1);
    const grid = m[zone];
    if (!grid) return null;
    const columns = grid.columns || 10;
    const row = Math.floor(idx / columns);
    const k = idx % columns;
    const rg = grid.rows[row] || grid.rows[0];
    return {
      no: spaceNo, zone, x: grid.x_start + (grid.pitch || 3.0) * (k + 0.5),
      z: -rg.y, rotation: rg.rotation || 0,
    };
  }

  // ---------------- 闸口信息 ----------------
  _gateInfo(gateId) {
    if (gateId === 'in') {
      return { id: 'in', x: this.entry.x, gateZ: this.entry.gateZ, paintZ: this.entry.paintZ,
               dir: -1, // 行驶 z 递减
               stopZ: (len) => this._entryStopZ(len) };
    }
    if (gateId === 'out') {
      return { id: 'out', x: this.exit.x, gateZ: this.exit.gateZ, paintZ: this.exit.paintZ,
               dir: +1, // 行驶 z 递增
               stopZ: (len) => this._exitStopZ(len) };
    }
    return null;
  }

  _gateObject(gateId) {
    return this.barriers?.[gateId] || this.scene?.gates?.[gateId] || null;
  }

  /** 统一物理通行条件：OPEN/KEEP_OPEN/MANUAL_OPEN/故障人工抬杆。 */
  _gatePassable(gateId) {
    const g = this._gateObject(gateId);
    if (!g) return false;
    return typeof g.isPassable === 'boolean' ? g.isPassable : !!g.isOpen;
  }

  /** 黑名单物理闸杆请求：优先后端 /api/sim/blacklist-gate；失败时本地物理兜底。 */
  _requestDeniedGate(gateId, action, v) {
    const key = `_deniedGateReq_${gateId}_${action}`;
    if (v.meta?.[key]) return;
    if (!v.meta) v.meta = {};
    v.meta[key] = this.simTime;
    const local = () => {
      const g = this._gateObject(gateId);
      try {
        if (!g) return;
        if (action === 'open') g.manualOpen?.();
        else g.manualClose?.();
      } catch (_) { /* ignore */ }
    };
    const body = { gate: gateId, action, vehicle_id: v.id,
      plate: v.meta?.plate || null, reason: 'blacklist_passthrough' };
    Promise.resolve()
      .then(() => Rest.blacklistGate(body))
      .then((res) => {
        const g = this._gateObject(gateId);
        if (res && res.passable !== undefined) g?.setPassable?.(res.passable, {
          manual: res.manual, authorization: res.authorization, keep_open: res.keep_open });
        this._emit({ type: 'blacklist_gate', gate: gateId, action,
          id: v.id, vid: v.vid, plate: v.meta?.plate || null,
          state: res?.state || null, passable: res?.passable,
          manual: res?.manual, cmd_id: res?.cmd_id || null, ok: res?.ok !== false });
      })
      .catch((err) => {
        const msg = String(err?.message || err || '');
        const zoneOccupied = err?.status === 409 || /zone_occupied|409/.test(msg);
        if (action === 'close' && zoneOccupied) {
          // 杆区仍有车：安全侧不强制落杆，稍后重试。
          delete v.meta[key];
          const tries = Number(v.meta?._deniedGateCloseTries || 0) + 1;
          v.meta._deniedGateCloseTries = tries;
          if (tries <= 5) {
            setTimeout(() => {
              if (this.vehicles.has(v.id)) this._requestDeniedGate(gateId, 'close', v);
            }, 800);
          }
          this._emit({ type: 'warn', msg: `blacklist-gate ${gateId}/close 杆区有车，安全等待重试` });
          return;
        }
        this._emit({ type: 'warn', msg: `blacklist-gate ${gateId}/${action} 接口不可用，走本地物理杆` });
        local();
      });
  }

  /** 等待 denied 状态所需闸杆条件；API 长时间未产生 barrier_cmd 时本地物理兜底。 */
  _deniedGateReady(gateId, action, v) {
    const g = this._gateObject(gateId);
    const reqAt = v.meta?.[`_deniedGateReq_${gateId}_${action}`] || 0;
    const waited = this.simTime - reqAt;
    if (action === 'open') {
      if (g?.isPassable) return true;
      if (reqAt && waited > 1.2) {
        try { g?.manualOpen?.(); } catch (_) { /* ignore */ }
      }
      return !!g?.isPassable;
    }
    // close：闸杆已不可通行即满足；杆区有车时安全等待，无车且超时才本地落杆。
    if (!g?.isPassable) return true;
    const occupied = !!this._gateOccupied?.[gateId];
    if (reqAt && waited > .8 && !occupied) {
      try { g?.manualClose?.(); } catch (_) { /* ignore */ }
    }
    return !g?.isPassable;
  }

  _deniedBypassLegs(v) {
    const laneZ = this.aisleZ?.n ?? -19.5;
    const outStopZ = this._exitStopZ(v.dims.len, this._visualLen(v));
    return [
      { kind: 'line', ax: v.pos.x, az: v.pos.z, bx: this.entry.x, bz: laneZ, reverse: false },
      { kind: 'line', ax: this.entry.x, az: laneZ, bx: this.exit.x, bz: laneZ, reverse: false },
      { kind: 'line', ax: this.exit.x, az: laneZ, bx: this.exit.x, bz: outStopZ, reverse: false },
    ];
  }

  refreshLayout() {
    this.planner = this.paths?.planner || this.planner;
    this.conflictZones = this.paths?.zones || this.conflictZones;
    this._zoneCache.clear();
  }

  /** P2-17：视觉长度（含后视镜/饰件）；OBB 碰撞仍用 v.dims。 */
  _visualLen(v) {
    const n = Number(v?.visualLen);
    if (Number.isFinite(n) && n > 0) return Math.max(n, Number(v?.dims?.len) || 0);
    return Collider.visualDimsOf(v?.spec?.family).len;
  }

  /** P2-17：视觉宽度（仅用于并排/跟车侧向安全判定，不参与 OBB 碰撞）。 */
  _visualWid(v) {
    const n = Number(v?.visualWid);
    if (Number.isFinite(n) && n > 0) return Math.max(n, Number(v?.dims?.wid) || 0);
    return Collider.visualDimsOf(v?.spec?.family).wid;
  }

  /**
   * 入口车心停止 z（three）：优先 planner.entryStopZ(len)，否则“停止线漆 + 半车长”。
   * visualLen 可选：P2-17 用视觉车长把车心向远离闸杆侧微调，保证视觉车头也留原有安全间隙；
   * 不传时行为与旧版完全一致（既有单测/planner 调用不变）。
   */
  _entryStopZ(len, visualLen = null) {
    const base = (() => {
      if (this.planner && typeof this.planner.entryStopZ === 'function') {
        try { const z = this.planner.entryStopZ(len); if (isFinite(z)) return z; } catch (_) { /* fall */ }
      }
      return this.entry.paintZ + Math.max(2.0, len) / 2;
    })();
    const extra = visualLen ? Math.max(0, Number(visualLen) - Number(len)) / 2 : 0;
    return base + extra;
  }

  /** 出口车心停止 z（three）：优先 planner.exitStopZ(len)，否则“停止线漆 - 半车长”。 */
  _exitStopZ(len, visualLen = null) {
    const base = (() => {
      if (this.planner && typeof this.planner.exitStopZ === 'function') {
        try { const z = this.planner.exitStopZ(len); if (isFinite(z)) return z; } catch (_) { /* fall */ }
      }
      return this.exit.paintZ - Math.max(2.0, len) / 2;
    })();
    const extra = visualLen ? Math.max(0, Number(visualLen) - Number(len)) / 2 : 0;
    return base - extra;
  }

  // ---------------- 冲突区 ----------------
  _zone(zoneId) { return this.conflictZones?.zones?.get?.(zoneId) || null; }

  /** 冲突区 three 矩形：优先 ConflictZones.rectThree；Z7 动态区用 stallZone；最后 manifest/Z7 兜底。 */
  _zoneRect(zoneId, spaceNo = null) {
    if (!zoneId) return null;
    if(/^W_[A-D]$/.test(zoneId)) {
      const zone=zoneId.slice(2), west=zone==='A'||zone==='B', north=zone==='A'||zone==='C';
      const z=-(this.manifest?.aisles?.[north?'n_y':'s_y'] ?? (north?19.5:-13.5));
      return {xmin:west?-35.8:5.5,xmax:west?-5.5:35.8,zmin:z-3.25,zmax:z+3.25};
    }
    if(zoneId==='JT_N'||zoneId==='JT_S') {
      const z=-(this.manifest?.aisles?.[zoneId==='JT_N'?'n_y':'s_y'] ?? (zoneId==='JT_N'?19.5:-13.5));
      return {xmin:-9,xmax:9,zmin:z-3.25,zmax:z+3.25};
    }
    const key = `${zoneId}|${spaceNo || ''}`;
    if (this._zoneCache.has(key)) return this._zoneCache.get(key);
    let rect = null;
    try {
      if (this.conflictZones && typeof this.conflictZones.rectThree === 'function') {
        rect = Collider.rectThree(this.conflictZones.rectThree(zoneId) || null);
      }
    } catch (_) { rect = null; }
    // Z7 动态区：用 planner 的 Blender 口径车位 + ConflictZones.stallZone 生成
    if (!rect && String(zoneId).startsWith('Z7')) {
      const blSpace = this._spaceBlender(spaceNo);
      if (blSpace && this.conflictZones && typeof this.conflictZones.stallZone === 'function') {
        try {
          const zz = this.conflictZones.stallZone(blSpace);
          if (zz && Array.isArray(zz.rect)) rect = Collider.rectThree(zz.rect);
        } catch (_) { rect = null; }
      }
    }
    if (!rect && this.manifest?.conflict_zones) {
      const z = this.manifest.conflict_zones.find((c) => c.id === zoneId);
      if (z && Array.isArray(z.rect) && z.rect.length >= 4) rect = Collider.rectThree(z.rect);
    }
    if (!rect && /^Z7/.test(String(zoneId)) && spaceNo) rect = this._stallRect(spaceNo);
    if (rect) this._zoneCache.set(key, rect);
    return rect;
  }

  /** 车位 Blender 口径（供 ConflictZones.stallZone / planner 使用）：{space_no,x,y,zone}。 */
  _spaceBlender(spaceNo) {
    if (!spaceNo) return null;
    try {
      if (this.planner && typeof this.planner.space === 'function') {
        const p = this.planner.space(spaceNo);
        if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) {
          return { space_no: p.no, x: p.x, y: p.y, zone: p.zone };
        }
      }
    } catch (_) { /* fallthrough */ }
    const m = this.manifest?.space_grid;
    const mm = /^([A-D])-(\d+)$/i.exec(spaceNo);
    if (mm && m) {
      const zone = mm[1].toUpperCase();
      const idx = Math.max(0, parseInt(mm[2], 10) - 1);
      const grid = m[zone];
      if (grid) {
        const columns = grid.columns || 10;
        const rg = grid.rows[Math.floor(idx / columns)] || grid.rows[0];
        return {
          space_no: spaceNo, zone, x: grid.x_start + (grid.pitch || 3.0) * ((idx % columns) + 0.5),
          y: rg.y, rotation: rg.rotation || 0,
        };
      }
    }
    return null;
  }

  /** Z7 兜底矩形：车位矩形 + 支路侧机动带 + 邻位各 1.5 m（three 坐标）。 */
  _stallRect(spaceNo) {
    const sp = this._spaceInfo(spaceNo);
    if (!sp) return null;
    const zone = sp.zone;
    const aisleKey = (zone === 'A' || zone === 'C') ? 'n' : 's';
    const az = this.aisleZ[aisleKey];
    const towardAisle = Math.sign(az - sp.z) || 1;         // 车位 → 支路方向
    const zNear = sp.z + towardAisle * 7.0;                // 机动带伸入支路 ≤7m
    const zmin = Math.min(sp.z - 3.0, zNear);
    const zmax = Math.max(sp.z + 3.0, zNear);
    return {
      xmin: sp.x - 1.5 - 1.5, xmax: sp.x + 1.5 + 1.5,      // 邻位 ±1.5 m
      zmin, zmax,
    };
  }

  _zoneHold(zoneId) {
    const z = this._zone(zoneId);
    if (z && Number(z.hold_s) > 0) return Number(z.hold_s);
    if (zoneId.startsWith('Z7')) return 4.5;
    if (zoneId === 'Z1') return 4.0;
    if (zoneId === 'Z2') return 5.0;
    return 6.0;
  }

  // =====================================================================
  // 路径规划门面（planner / legacy 兼容）
  // =====================================================================
  _normalizePlan(plan, spaceNo) {
    if (!plan) return null;
    if (Array.isArray(plan)) return { spaceNo, legs: plan, zones: [] };
    const legs = plan.legs || plan.path || [];
    const zones = Array.isArray(plan.zones) ? plan.zones.slice() : [];
    if (!legs.length) return null;
    return { spaceNo: plan.spaceNo || spaceNo, legs, zones };
  }

  _planEntry(spaceNo, len = null, visualLen = null) {
    if (this.planner && typeof this.planner.planEntry === 'function') {
      try {
        // 精确停止点：from='stop' + startZ(车长) → 路径起点 = 当前车心停止点（避免 snap 0.1~0.3m）
        const opts = { from: 'stop', safeTurns:true };
        if (len != null) opts.startZ = this._entryStopZ(len, visualLen);
        // 邻位已有停放车：倒车弧线取更保守控制点，消除车尾扫掠邻车（R3 支持单次覆盖）
        if (this._neighborParked(spaceNo)) opts.stallCurveM = this.options.neighborStallCurveM ?? 1.0;
        return this._normalizePlan(this.planner.planEntry(spaceNo, opts), spaceNo);
      } catch (e) { this._warnGlobal(`planner.planEntry(${spaceNo}) 失败: ${e}`); return null; }
    }
    if (this.paths && typeof this.paths.entryToSpace === 'function') {
      const sp = this._spaceInfo(spaceNo);
      return sp ? { spaceNo, legs: this.paths.entryToSpace(sp), zones: [] } : null;
    }
    return null;
  }

  _planExit(spaceNo, len = null, visualLen = null) {
    if (this.planner && typeof this.planner.planExit === 'function') {
      try {
        // 精确停止点：to='stop' + endZ(车长) → 路径终点 = 当前车心停止点
        const opts = { to: 'stop', safeTurns:true };
        if (len != null) opts.endZ = this._exitStopZ(len, visualLen);
        if (this._neighborParked(spaceNo, { excludeSelf: true })) opts.stallCurveM = this.options.neighborStallCurveM ?? 1.0;
        return this._normalizePlan(this.planner.planExit(spaceNo, opts), spaceNo);
      } catch (e) { this._warnGlobal(`planner.planExit(${spaceNo}) 失败: ${e}`); return null; }
    }
    if (this.paths && typeof this.paths.spaceToExitStop === 'function') {
      const sp = this._spaceInfo(spaceNo);
      return sp ? { spaceNo, legs: this.paths.spaceToExitStop(sp), zones: [] } : null;
    }
    return null;
  }

  /** 目标车位同排左右邻位（±1 列）是否已有停放车辆（用于收紧倒车弧线）。 */
  _neighborParked(spaceNo, { excludeSelf = false } = {}) {
    const me = this._spaceBlender(spaceNo);
    if (!me) return false;
    const pitch = Number(this.manifest?.space_grid?.[me.zone]?.pitch) || 3.0;
    for (const v of this.vehicles.values()) {
      if (excludeSelf && v.meta?.spaceNo === spaceNo) continue;
      if (v.state !== 'parked') continue;
      const other = this._spaceBlender(v.meta?.spaceNo);
      if (!other) continue;
      if (other.zone !== me.zone) continue;
      const dy = Math.abs(Number(other.y) - Number(me.y));
      const dx = Math.abs(Number(other.x) - Number(me.x));
      if (dy < 0.5 && Math.abs(dx - pitch) < 0.6) return true;
    }
    return false;
  }

  /** 入口外场→停止线的直线段（排队段；不依赖 planner 图）。 */
  _entryApproachLegs(startZ, len, visualLen = null) {
    return [{
      kind: 'line', ax: this.entry.x, az: startZ, bx: this.entry.x,
      bz: this._entryStopZ(len, visualLen), reverse: false,
    }];
  }

  /** 出口停止线→越闸→外场（含 Z2，由预约门控）。 */
  _exitContinueLegs(v) {
    const z0 = v.pos.z;
    const z1 = this.outerZ + 4.0;
    return [{ kind: 'line', ax: this.exit.x, az: z0, bx: this.exit.x, bz: z1, reverse: false }];
  }

  /** 把 plan 赋给车辆；若 plan 起点与当前位姿有偏差，先补一段短线避免位置跳变。 */
  _assignPlan(v, plan, { prependToStart = true } = {}) {
    const legs = plan.legs.slice();
    if (prependToStart) {
      const start = legStartPoint(legs[0]);
      if (start) {
        const d = hyp(start.x - v.pos.x, start.z - v.pos.z);
        if (d > 0.05 && d < 3.0) {
          legs.unshift({ kind: 'line', ax: v.pos.x, az: v.pos.z, bx: start.x, bz: start.z, reverse: false });
        }
      }
    }
    v.zones = Array.isArray(plan.zones) ? plan.zones.slice() : [];
    // 若路径起点已在某冲突区内，该区应视为“已进入”，直接尝试预约
    v.zoneCursor = 0;
    v.zoneReleased = new Set();
    v.zoneEntered = new Set();
    v.zoneBlocked = null;
    v.zoneBlockedSince = null;
    v.nextZoneId = null;
    v.nextZoneDist = Infinity;
    v.setLegs(legs, { snapFirst: false });
    // 过滤掉起点已在车后/已经过的冲突区（防守：planner 若给了入口区之外的区）
    v.done = false;
    return v;
  }

  // =====================================================================
  // 派车 / 排队
  // =====================================================================
  _pick(values) { return values[Math.floor(this._rng() * values.length)]; }

  async spawnEntry({ kind = 'blue', model = null, imagePath = null, vehicleId = null,
               plate = null, expectedPlate = null, vid = null, plateType = null,
               duration = 0, imageUrl = null, plateImage = null, plateColor = null,
               vehicleKind = null } = {}) {
    if (this.options.claimDispatcher) {
      this._claimPromise ||= this.options.claimDispatcher().catch(error => {this._claimPromise=null;throw error;});
      await this._claimPromise;
    }
    const id = String(vid || vehicleId || `sim_${this.seed}_${this._seq++}`);
    if (this.vehicles.has(id) || this._queuedIds.has(id)) return Promise.reject(new Error(`重复车辆ID ${id}`));
    const plateKey = String(plate || expectedPlate || '').replace(/[·.\-\s]/g, '').toUpperCase();
    if (plateKey) {
      const dup = [...this.vehicles.values()].find((v) => !v.remote
        && String(v.meta?.plate || '').replace(/[·.\-\s]/g, '').toUpperCase() === plateKey);
      const queuedDup = this._spawnRequests.some((r) => String(r.plate || r.expectedPlate || '')
        .replace(/[·.\-\s]/g, '').toUpperCase() === plateKey);
      if (dup || queuedDup) {
        const error = new Error(`车牌 ${plateKey} 已在场/排队，不能重复派车`);
        error.code = 'duplicate_plate'; error.status = 409; error.plate = plateKey;
        return Promise.reject(error);
      }
    }
    if (this._spawnRequests.length >= 100) return Promise.reject(new Error('等待派车请求已达100辆上限'));
    const spec = model ? this._resolveModel(model, kind) : this._pick(CAR_MODELS[kind] || CAR_MODELS.blue);
    this._queuedIds.add(id);
    return new Promise((resolve, reject) => {
      this._spawnRequests.push({id,kind,spec,imagePath:imagePath || this._pick(PLATE_IMAGES[kind] || PLATE_IMAGES.blue),
        plate:plate || expectedPlate,expectedPlate:expectedPlate || plate,epoch:this._runtimeEpoch,
        plateType,duration:Number(duration)||0,imageUrl:imageUrl||null,
        plateImage,plateColor,vehicleKind,resolve,reject});
      this._emit({type:'queue_hold',id,reason:'等待道路内安全生成位置',pending:this._spawnRequests.length});
      this._drainSpawn();
    });
  }

  async _drainSpawn() {
    if (this._spawning || !this._spawnRequests.length) return;
    const request = this._spawnRequests[0];
    const dims = Collider.dimsOf(request.spec.family);
    const visual = Collider.visualDimsOf(request.spec.family);
    const startZ = this._queueSpawnZ(dims.len, QK('minFollowGap') || 1, visual.len);
    if (this._entryQueueCount() >= this.options.queueMax || startZ + visual.len/2 > this.outerSouthZ) return;
    const candidate = {x:this.entry.x,z:startZ,theta:0,...dims};
    if ([...this.vehicles.values()].some(v => Collider.poseOverlap(candidate,
      {x:v.pos.x,z:v.pos.z,theta:v.theta,...v.dims}))) return;
    this._spawning = true;
    let vehicle;
    try {
      vehicle = new Vehicle(request.id,this.assets,request.spec);
      await vehicle.load(this.scene.scene);
      if (request.epoch !== this._runtimeEpoch) {
        vehicle.dispose(this.scene.scene);request.resolve(null);return;
      }
      // The queue may have moved while assets loaded; recompute the safe position.
      const vLen = Math.max(Number(vehicle.visualLen) || 0, visual.len);
      const z = this._queueSpawnZ(dims.len,QK('minFollowGap') || 1, vLen);
      const loadedPose={x:this.entry.x,z,theta:0,...dims};
      if (this._entryQueueCount() >= this.options.queueMax || z + vLen/2 > this.outerSouthZ ||
          [...this.vehicles.values()].some(v => Collider.poseOverlap(loadedPose,
            {x:v.pos.x,z:v.pos.z,theta:v.theta,...v.dims}))) {vehicle.dispose(this.scene.scene);return;}
      vehicle.vid = request.id;
      vehicle.meta = {kind:request.kind,imagePath:request.imagePath,plate:request.plate || null,
        color:request.kind,spaceNo:null,recordId:null,amount:null,
        expectedPlate:request.expectedPlate || null,vid:request.id,
        plateType:request.plateType || null,vehicleKind:request.vehicleKind || null,
        duration:Number(request.duration)||0,imageUrl:request.imageUrl || null};
      if (request.plate) vehicle.setPlate(request.plate, request.plateColor || (request.plateType === 'yellow' ? 'yellow' : request.kind));
      // 新剧本/手动派车：后端渲染车牌图必须在车辆进入相机视角前贴好（setPlateImage 只换材质）。
      if (request.plateImage) {
        try { vehicle.setPlateImage(request.plateImage, request.plate, request.plateColor || request.plateType || request.kind); }
        catch (e) { this._warnGlobal(`车牌图应用失败 ${request.plate}: ${e?.message || e}`); }
      }
      vehicle.setLegs(this._entryApproachLegs(z,dims.len,vLen),{snapFirst:true});
      vehicle.state='approach';vehicle.cruise=QK('straightSpeed');vehicle.meta._arrivedIn=false;
      this.vehicles.set(vehicle.id,vehicle);
      this._spawnRequests.shift();this._queuedIds.delete(vehicle.id);
      this._emit({type:'spawn',id:vehicle.id,vid:vehicle.vid,kind:request.kind});
      request.resolve(vehicle);
    } catch (error) {
      if(vehicle) vehicle.dispose(this.scene.scene);
      if(this._spawnRequests[0]===request) this._spawnRequests.shift();
      this._queuedIds.delete(request.id);request.reject(error);
    } finally {
      this._spawning=false;
      if(this._spawnRequests[0]!==request) queueMicrotask(()=>this._drainSpawn());
    }
  }

  /** 场内排队新车的车心起点 z：保证与前车（视觉车尾+间距）不重叠（混合车型安全）。 */
  _queueSpawnZ(len, gap, visualLen = null) {
    const vLen = visualLen || len;
    const stopZ = this._entryStopZ(len, vLen);
    let tail = stopZ + vLen + gap;
    for (const o of this.vehicles.values()) {
      if (o.state !== 'approach' && o.state !== 'wait_entry') continue;
      const oLen = this._visualLen(o);
      tail = Math.max(tail, o.pos.z + oLen / 2 + gap + vLen / 2);
    }
    return tail;
  }

  /** 外场等待新车的车心 z（同样按视觉车长递推，避免重叠）。 */
  _holdSpawnZ(len, gap, visualLen = null) {
    const vLen = visualLen || len;
    let z = this.holdZ + vLen / 2 + gap;
    for (const o of this.vehicles.values()) {
      if (o.state !== 'hold_entry') continue;
      const oLen = this._visualLen(o);
      z = Math.max(z, o.pos.z + oLen / 2 + gap + vLen / 2);
    }
    return z;
  }

  _tooCloseToOthersAtSpawn(v) {
    for (const o of this.vehicles.values()) {
      if (o === v) continue;
      if (hyp(o.pos.x - v.pos.x, o.pos.z - v.pos.z) < 2.0) {
        this._warn(v, `派车位置与前车重叠风险：${o.id}`);
        return true;
      }
    }
    return false;
  }

  /** 场内入口排队数（approach + wait_entry）。 */
  _entryQueueCount() {
    let n = 0;
    for (const v of this.vehicles.values()) {
      if (v.state === 'approach' || v.state === 'wait_entry') n++;
    }
    return n;
  }

  _heldCount() {
    let n = 0;
    for (const v of this.vehicles.values()) if (v.state === 'hold_entry') n++;
    return n;
  }

  _resolveModel(model, kind) {
    if (typeof model === 'object' && model.file) return model;
    // `kind` 现在既可能是颜色桶（blue/green），也可能是新剧本的 vehicle_kind（sedan/hatch/van）；
    // 指定了模型文件时在所有车型桶里按文件名查找，避免被 kind 桶限制。
    const all = Object.values(CAR_MODELS).flat();
    const hit = all.find((m) => m.file === model);
    if (hit) return hit;
    return this._pick(CAR_MODELS[kind] || CAR_MODELS.blue);
  }

  /** 让一辆已入库车开始出场（驶向出口停止线等待缴费/抬杆）。 */
  startExit(idOrPlate) {
    const v = this._find(idOrPlate);
    if (!v || v.state !== 'parked') {
      this._emit({ type: 'warn', msg: `出场目标未就绪: ${idOrPlate}` });
      return null;
    }
    const spaceNo = v.meta.spaceNo;
    if (!spaceNo) return null;
    // 出场前先恢复真实车位视觉中心（R-04 视觉偏移只用于停放展示）。
    v.setVisualOffset?.(0, 0);
    v.meta.autoExitAt = null;
    const plan = this._planExit(spaceNo, v.dims.len, this._visualLen(v));
    if (!plan || !plan.legs.length) {
      this._emit({ type: 'warn', msg: `出场路径规划失败: ${v.vid} @ ${spaceNo}` });
      return null;
    }
    // 出场zone序列：planner 给 Z7:<spaceNo> 在最前；缺省时补上
    if (!plan.zones.length || !String(plan.zones[0]).startsWith('Z7')) {
      plan.zones.unshift(`Z7:${spaceNo}`);
    }
    this._assignPlan(v, plan, { prependToStart: false });
    v.state = 'to_exit';
    v.cruise = QK('straightSpeed');
    v.meta.recordId = null; v.meta.amount = null; v.meta.exitAuthorized = false;
    v.meta._arrivedOut = false; v.meta._zoneOut = false; v.meta._passedOut = false;
    // 先在车位内申请 Z7（出库机动区），BLOCKED 则在车位里等待
    this._requestZone(v, `Z7:${spaceNo}`, this.simTime + 0.3);
    this.onEvent({ type: 'exit_start', id: v.id, vid: v.vid, plate: v.meta.plate });
    return v;
  }

  _find(idOrPlate) {
    if (this.vehicles.has(idOrPlate)) return this.vehicles.get(idOrPlate);
    const matches=[...this.vehicles.values()].filter(v=>!v.remote && (v.meta.plate===idOrPlate || v.vid===idOrPlate));
    return matches.length===1 ? matches[0] : null;
  }

  _eventVehicle(message, states) {
    let vehicle;
    if (message.vehicle_id) vehicle=this.vehicles.get(message.vehicle_id);
    else if(message.plate) {
      const matches=[...this.vehicles.values()].filter(v=>!v.remote && v.meta.plate===message.plate && states.includes(v.state));
      if(matches.length===1) vehicle=matches[0];
    }
    if(!vehicle || vehicle.remote || !states.includes(vehicle.state)) {
      this._emit({type:'warn',msg:`事件无法精确绑定 ${message.vehicle_id || message.plate || '缺少标识'}`});
      return null;
    }
    return vehicle;
  }

  /**
   * 到达停止线上报：优先“3D 闸口相机截图 → POST /sim/events/arrived-frame”（qa-backend 提供），
   * 端点未就绪/截图失败时回退旧图库 image_path 模式 Rest.arrived，保证演示不中断。
   */
  async _reportArrival(v, gate) {
    const expected = v.meta?.expectedPlate || v.meta?.plate || null;
    const capture = this.options?.cameraFrame;
    const useFrame = capture && this.options?.cameraRecognition !== false && !v.remote;
    if (useFrame) {
      try {
        const frame = await capture(gate, v);
        if (frame?.blob) {
          // 先发“本机截图”事件让识别输入卡片立即可见（不必等后端返回）；
          // 识别结果随后由 WS plate_recognized 更新为最终卡片（服务端 image_url/裁剪图/置信度）。
          let objectUrl = null;
          try { objectUrl = (typeof URL !== 'undefined' && URL.createObjectURL) ? URL.createObjectURL(frame.blob) : null; }
          catch (_) { objectUrl = null; }
          this._emit({ type: 'camera_frame', gate, id: v.id, vid: v.vid,
            image_url: objectUrl, image_path: null, pending: true,
            expected_plate: expected, capture_ms: frame.capture_ms,
            width: frame.width, height: frame.height });
          const res = await Rest.arrivedFrame({
            vehicle_id: v.id, gate, expected_plate: expected,
            sim_ts: this.simTime, image: frame.blob,
          });
          v.meta.lastCameraFrame = {
            gate, capture_ms: frame.capture_ms, image_path: res?.image_path || null,
            image_url: res?.image_url || null, expected_plate: expected, sim_ts: this.simTime,
            width: frame.width, height: frame.height,
          };
          this._emit({ type: 'camera_frame_sent', gate, id: v.id, vid: v.vid,
            image_path: res?.image_path || null, image_url: res?.image_url || null,
            expected_plate: expected, capture_ms: frame.capture_ms,
            width: frame.width, height: frame.height });
          return res;
        }
      } catch (e) {
        this._emit({ type: 'warn',
          msg: `相机截图识别不可用（${e?.status || ''}${e?.message || e}），回退旧图库模式` });
      }
    }
    return Rest.arrived(v.id, gate, v.meta.imagePath, this.simTime);
  }

  onRecognized(message) {
    const states=message.gate==='out' ? ['to_exit','wait_exit'] : ['approach','wait_entry'];
    const v=this._eventVehicle(message,states);
    if(!v) return null;
    if(message.plate) v.setPlate(message.plate,message.color || v.meta.kind);
    v.meta.needManual=!!message.need_manual;v.meta.recognizedPlate=message.plate || null;
    if (message.gate === 'in' && ['blacklist', 'duplicate_entry', 'duplicate_plate'].includes(message.reason)) {
      this.onEntryDenied({ ...message, type: 'entry_denied', reason: message.reason });
    }
    return v;
  }

  _deniedVehicle(message) {
    const id = message.vehicle_id || message.vehicleId || null;
    if (id && this.vehicles.has(String(id))) return this.vehicles.get(String(id));
    const plate = String(message.plate || message.source_plate || '').replace(/[·.\-\s]/g, '').toUpperCase();
    if (!plate) return null;
    return [...this.vehicles.values()].find((v) => !v.remote &&
      String(v.meta?.plate || '').replace(/[·.\-\s]/g, '').toUpperCase() === plate) || null;
  }

  _hasRearAtEntry(v) {
    return [...this.vehicles.values()].some((o) => o !== v && !o.remote
      && ['approach', 'wait_entry', 'hold_entry'].includes(o.state)
      && Math.abs(o.pos.x - this.entry.x) < 3.2 && o.pos.z > v.pos.z + 1.0);
  }

  _releaseDeniedQueue(v) {
    try { this.reservations?.releaseAll?.(v.id); } catch (_) { /* ignore */ }
    for (const rid of [...(v.reservedZones || [])]) this._releaseZone(v, rid);
    v.reservedZones?.clear?.();
    v.zoneWindows?.clear?.();
    this._queuedIds?.delete?.(v.id);
  }

  /**
   * 入口拒绝（黑名单/重复车牌）：无后车→倒车离场；有后车→先向前进场再直接出场；
   * 不创建普通停车记录，释放预约/排队资源。
   */
  onEntryDenied(message = {}) {
    const v = this._deniedVehicle(message);
    if (!v) {
      this._emit({ type: 'warn', msg: `入口拒绝事件未找到车辆 ${message.plate || message.vehicle_id || ''}` });
      return null;
    }
    if (v.meta?.denied) return v;
    this._releaseDeniedQueue(v);
    v.meta.denied = true;
    v.meta.deniedReason = message.reason || message.code || 'entry_denied';
    v.meta.noRecord = true;
    v.meta.recordId = null;
    v.meta.spaceNo = null;
    v.meta.needManual = false;
    const hasRear = this._hasRearAtEntry(v);
    v.meta.deniedMode = hasRear ? 'forward_then_exit' : 'reverse_out';
    v.meta.deniedAt = this.simTime;
    v.hardStop = false;
    v.cruise = 0;
    if (hasRear) {
      // 有后车：等入口闸杆物理放行，再走正常入口/场内车道绕到出口等待区。
      v.state = 'DENIED_BYPASS_WAIT_ENTRY_GATE';
      this._requestDeniedGate('in', 'open', v);
    } else {
      // 无后车：等入口闸杆处于落杆/不可通行，再沿入口车道倒车离场。
      v.state = 'DENIED_REVERSE_WAIT_ENTRY_GATE';
      this._requestDeniedGate('in', 'close', v);
    }
    this._emit({ type: 'entry_denied', id: v.id, vid: v.vid, plate: v.meta.plate,
      reason: v.meta.deniedReason, mode: v.meta.deniedMode, no_record: true,
      queue_released: true });
    return v;
  }

  /**
   * 黑名单处置完成后，按后端新契约处理同入口后车自动重试结果：
   * - 只清 `retried_vehicle_ids` 中、且 `authorized>=1` 的后车 needManual；
   * - 后端已授权时不再补发 arrival（后端幂等，重复 arrival 会被忽略）；
   * - 未在 retried 列表/仍失败的车辆保留 pending 显示，由后端新 pending/后续正常 arrival 处理。
   */
  _applyBlacklistRetryResult(denied, response) {
    if (!denied) return;
    const retry = response?.retry || response || {};
    const ids = (Array.isArray(retry.retried_vehicle_ids) ? retry.retried_vehicle_ids : []).map(String);
    const authorized = Number(retry.authorized) || 0;
    const expectedAuthorized = Number(retry.expected_authorized ?? response?.expected_authorized) || 0;
    const failed = Number(retry.failed) || 0;
    const retried = Number(retry.retried) || ids.length;
    if ((authorized > 0 || expectedAuthorized > 0) && ids.length) {
      for (const id of ids) {
        const f = this.vehicles.get(id);
        if (!f || f.remote) continue;
        f.meta.needManual = false;
        f.meta.pending = false;
        f.meta.backendBlacklistRetry = true;
        this._emit({ type: 'blacklist_retry_authorized', id: f.id, vid: f.vid,
          plate: f.meta?.plate || null });
      }
    }
    this._emit({ type: 'blacklist_retry', id: denied.id, vid: denied.vid,
      plate: denied.meta?.plate || null, retried, authorized, expected_authorized: expectedAuthorized,
      failed, pending_ids: retry.pending_ids || [],
      retried_vehicle_ids: ids });
  }

  /**
   * 黑名单前车清场后：对没进入后端 pending 重试列表、仍停在 wait_entry 的后车，
   * 延迟 300–800ms 重新抓拍/上报 arrival（生成新相机帧），让它走正常入场授权；
   * 已被后端 retry 覆盖/已授权/仍有 pending 的后车不重复上报，避免重复 pending。
   */
  _scheduleFollowerArrivalRetry(denied, response) {
    if (!denied) return;
    const retry = response?.retry || response || {};
    const retriedIds = new Set((Array.isArray(retry.retried_vehicle_ids) ? retry.retried_vehicle_ids : []).map(String));
    const retrySucceeded = (Number(retry.authorized) || 0) > 0;
    const candidates = [...this.vehicles.values()].filter((f) => f !== denied && !f.remote
      && f.state === 'wait_entry'
      // 后端重试成功（authorized>0）的车辆不再补发；重试失败/未覆盖的仍要前端新帧重报，
      // 否则后端 retry 失败时后车会永远停在 wait_entry（实测 dbg-rear）。
      && !(retrySucceeded && retriedIds.has(String(f.id)))
      && !f.meta?.needManual
      && !f.meta?._arrivalRetryAfterBlacklist
      && !f.meta?._arrivalRetryScheduled);
    for (const f of candidates) {
      f.meta._arrivalRetryScheduled = true;
      const waitMs = 350 + Math.floor(Math.random() * 400);
      setTimeout(() => {
        const cur = this.vehicles.get(f.id);
        if (!cur || cur.remote || cur.meta?._arrivalRetryAfterBlacklist) return;
        // 期间如果已拿到车位/已离开/变成 pending，交给对应事件，不重复上报。
        if (cur.state !== 'wait_entry' || cur.meta?.needManual || cur.meta?.spaceNo) return;
        cur.meta._arrivalRetryAfterBlacklist = true;
        cur.meta._arrivalRetryScheduled = false;
        this._emit({ type: 'scn', msg: `黑名单后车重新抓拍 ${cur.vid}` });
        this._reportArrival(cur, 'in').catch((e) => this._emit({ type: 'warn',
          msg: `黑名单后车重新上报失败 ${cur.vid}: ${e?.message || e}` }));
      }, waitMs);
    }
  }

  /** 后端请求 3D 对指定车辆重新抓拍并上报 arrival（新帧走正常识别/授权）。 */
  onArrivalRetryRequested(message = {}) {
    const gate = message.gate === 'out' ? 'out' : 'in';
    const id = message.vehicle_id || message.vehicleId || null;
    let v = id ? this.vehicles.get(String(id)) : null;
    if (!v && message.plate) {
      const key = String(message.plate).replace(/[·.\-\s]/g, '').toUpperCase();
      v = [...this.vehicles.values()].find((x) => !x.remote &&
        String(x.meta?.plate || '').replace(/[·.\-\s]/g, '').toUpperCase() === key) || null;
    }
    if (!v || v.remote) return null;
    this._emit({ type: 'arrival_retry_requested', id: v.id, vid: v.vid,
      plate: v.meta?.plate || null, gate, reason: message.reason || 'retry_failed' });
    if (v.state !== 'wait_entry' && v.state !== 'approach') return v;
    if (v.meta?._arrivalRetryRequestedDone || v.meta?.spaceNo) return v;
    v.meta._arrivalRetryRequestedDone = true;
    if (v.state === 'approach') {
      // 到停止线时 _advance 原本会抓拍上报，不提前截空帧。
      v.meta._arrivalRetryOnStop = true;
      return v;
    }
    setTimeout(() => {
      const cur = this.vehicles.get(v.id);
      if (!cur || cur.remote || cur.state !== 'wait_entry' || cur.meta?.spaceNo) return;
      this._reportArrival(cur, gate).catch((e) => this._emit({ type: 'warn',
        msg: `arrival_retry_requested 重新上报失败 ${cur.vid}: ${e?.message || e}` }));
    }, 350);
    return v;
  }

  /** 黑名单 3D 实际路径回报（只写事件/审计，不建记录）。 */
  _reportBlacklistResolution(v, resolution) {
    if (!v || !v.meta?.denied || v.meta.blacklistReported) return;
    v.meta.blacklistReported = true;
    Promise.resolve()
      .then(() => Rest.blacklistResolution({
        vehicle_id: v.id, plate: v.meta.plate || null, resolution,
      }))
      .then((res) => {
        v.meta.blacklistResolution = resolution;
        this._emit({ type: 'blacklist_resolution', id: v.id, vid: v.vid,
          plate: v.meta.plate, resolution: res?.resolution || resolution,
          ok: res?.ok !== false, no_record: true,
          retried: res?.retried, authorized: res?.authorized, failed: res?.failed,
          expected_authorized: res?.expected_authorized,
          pending_ids: res?.pending_ids || [],
          retried_vehicle_ids: res?.retried_vehicle_ids || [] });
        this._applyBlacklistRetryResult(v, res);
        this._scheduleFollowerArrivalRetry(v, res);
      })
      .catch((e) => {
        this._emit({ type: 'warn',
          msg: `黑名单路径回报失败 ${v.meta.plate || v.vid}: ${e?.message || e}` });
        // 回报接口失败时也不能让后车永远等；仍按未进入后端重试列表处理。
        this._scheduleFollowerArrivalRetry(v, { ok: false });
      });
  }

  /** 入口人工/后端授权：撤销拒绝并恢复进场等待（后端随后分配车位/抬杆）。 */
  onEntryAuthorized(message = {}) {
    const v = this._deniedVehicle(message)
      || this._eventVehicle(message, ['approach', 'wait_entry', 'denied_reverse', 'denied_forward',
        'denied_exit', 'DENIED_REVERSE_WAIT_ENTRY_GATE', 'DENIED_REVERSE_CLOSE_GATE',
        'DENIED_BYPASS_WAIT_ENTRY_GATE', 'DENIED_BYPASS_ROUTE',
        'DENIED_BYPASS_WAIT_EXIT_GATE', 'DENIED_BYPASS_EXIT', 'DENIED_BYPASS_CLOSE_GATE']);
    if (!v) return null;
    v.meta.denied = false;
    v.meta.manualAuthorized = true;
    if (message.space_no) v.meta.spaceNo = message.space_no;
    if (message.record_id != null) v.meta.recordId = message.record_id;
    if (message.action) v.meta.entryAction = message.action;
    if (message.reason) v.meta.authorizeReason = message.reason;
    if (['denied_reverse', 'denied_forward', 'denied_exit', 'DENIED_REVERSE_WAIT_ENTRY_GATE',
      'DENIED_REVERSE_CLOSE_GATE', 'DENIED_BYPASS_WAIT_ENTRY_GATE', 'DENIED_BYPASS_ROUTE',
      'DENIED_BYPASS_WAIT_EXIT_GATE', 'DENIED_BYPASS_EXIT', 'DENIED_BYPASS_CLOSE_GATE'].includes(v.state)) {
      const len = v.dims.len;
      v.setLegs(this._entryApproachLegs(v.pos.z, len, this._visualLen(v)), { snapFirst: false });
      v.state = 'approach';
      v.cruise = QK('straightSpeed');
      v.meta._arrivedIn = false;
    }
    this._emit({ type: 'entry_authorized', id: v.id, vid: v.vid, plate: v.meta.plate,
      gate: 'in', mode: message.mode || message.authorization || 'manual', space_no: v.meta.spaceNo || null });
    return v;
  }

  /** 出口授权（exit_authorized / manual_authorized）：解锁 wait_exit，不伪造缴费记录。 */
  onExitAuthorized(message = {}) {
    const v = this._deniedVehicle(message)
      || this._eventVehicle(message, ['to_exit', 'wait_exit', 'denied_exit']);
    if (!v) return null;
    if (message.record_id != null) v.meta.recordId = message.record_id;
    v.meta.exitAuthorized = true;
    v.meta.needManual = false;
    if (message.action) v.meta.exitAction = message.action;
    if (message.amount_paid != null) v.meta.amountPaid = message.amount_paid;
    if (message.space_no) v.meta.spaceNo = message.space_no;
    if (message.reason) v.meta.exitReason = message.reason;
    this._emit({ type: 'exit_authorized', id: v.id, vid: v.vid, plate: v.meta.plate,
      gate: message.gate || 'out', mode: message.mode || message.authorization || message.action || 'manual' });
    return v;
  }

  onPendingResolved(message = {}) {
    if (message.status !== 'confirmed') return null;
    return message.gate === 'in' ? this.onEntryAuthorized(message) : this.onExitAuthorized(message);
  }

  onSpaceAssigned(message) {
    const v=this._eventVehicle(message,['approach','wait_entry']);
    if(!v || (v.meta.spaceNo && v.meta.spaceNo!==message.space_no) ||
      (message.plate && v.meta.plate && message.plate!==v.meta.plate)) return null;
    v.meta.spaceNo=message.space_no;
    const sp=this._spaceInfo(message.space_no);
    if(sp) {sp.status='occupied';this.markers?.setOccupied?.(message.space_no,true);}
    this._emit({type:'assigned',id:v.id,vid:v.vid,space_no:message.space_no});
    return v;
  }

  onFeeQuote(message) {
    const v=this._eventVehicle(message,['to_exit','wait_exit']);
    if(!v || (v.meta.recordId && v.meta.recordId!==message.record_id) ||
      (message.plate && v.meta.plate && message.plate!==v.meta.plate)) return null;
    v.meta.recordId=message.record_id;v.meta.amount=message.amount;v.meta.minutes=message.minutes;
    return v;
  }

  onPaymentCompleted(message) {
    const v=this._eventVehicle(message,['to_exit','wait_exit']);
    if(!v || (v.meta.recordId && v.meta.recordId!==message.record_id) ||
      (message.plate && v.meta.plate && message.plate!==v.meta.plate)) return null;
    v.meta.recordId=message.record_id;v.meta.exitAuthorized=true;
    return v;
  }

  pendingExitVehicle() {
    for (const v of this.vehicles.values()) {
      if (v.state === 'wait_exit' && v.meta.recordId) return v;
    }
    return null;
  }

  // =====================================================================
  // 帧循环
  // =====================================================================
  update(dt) {
    if (!(dt > 0) || !Number.isFinite(dt)) return;
    let remaining=dt;
    while(remaining>1e-9) {
      const step=Math.min(.05,remaining);this._updateStep(step);remaining-=step;
    }
  }

  _updateStep(dt) {
    this.simTime += dt;
    const vs = [...this.vehicles.values()];
    const owned = vs.filter((v) => !v.remote);
    for (const v of owned) this._advance(v);
    for (const v of owned) this._refreshReservations(v);
    for (const v of owned) this._reserveNext(v);
    for (const v of owned) this._constrain(v, vs);
    for (const v of owned) this._safeStep(v,dt,vs);
    for (const v of owned) this._maybeAutoExit(v);
    for (const v of owned) this._syncBoundaryVisualOffset(v);
    for (const v of owned) this._releaseOnExit(v);
    this._syncGateZones(owned);
    this._drainSpawn();
    this._cleanup();
  }

  /**
   * 刷新/回收预约窗口：车还在区外等待、窗口即将过期的，先释放再重新申请（下一帧 _reserveNext 会重订）。
   * 已进入区内的车不主动释放（依赖一次性申请的足够长窗口覆盖穿越全程）。
   */
  _refreshReservations(v) {
    if (!v.reservedZones || !v.reservedZones.size) return;
    const now = this.simTime;
    for (const id of [...v.reservedZones]) {
      const win = v.zoneWindows?.get(id);
      if (!win) continue;
      if (win.t1 - now > 1.5) continue;
      const rect = this._zoneRect(id, v.meta.spaceNo);
      const inside = rect ? Collider.boxIntersectsRect(v.poseBox(), rect) : false;
      if (inside) continue;            // 已在区内：保持，避免释放瞬间被他人抢占
      if(v.zoneEntered?.has(id)) v.zoneReleased?.add(id);
      v.reservedZones.delete(id);
      v.zoneWindows.delete(id);
      try { this.reservations?.release?.(v.id, id); } catch (_) { /* ignore */ }
      this._emit({ type: 'scn', msg: `预约窗口到期回收 ${v.vid} ${id}` });
    }
  }

  _advance(v) {
    switch (v.state) {
      case 'hold_entry': {
        // 外场等待：场内排队有空位才放行（FIFO：只放行排在最前面的等待车）
        if (this._entryQueueCount() >= this.options.queueMax) break;
        if (!this._isFirstHeld(v)) break;
        const len = v.dims.len;
        v.hardStop = false;
        v.setLegs(this._entryApproachLegs(v.pos.z, len, this._visualLen(v)), { snapFirst: true });
        v.state = 'approach';
        v.cruise = QK('straightSpeed');
        this._emit({ type: 'scn', msg: `外场等待车辆放行 ${v.vid}` });
        break;
      }
      case 'approach':
        if (v.done && !v.meta._arrivedIn) {
          v.meta._arrivedIn = true;
          v.state = 'wait_entry';
          this.onEvent({ type: 'arrived', gate: 'in', id: v.id, vid: v.vid });
          this._reportArrival(v, 'in').catch((e) => this._emit({
            type: 'warn', msg: `入口到达上报失败 ${e?.message || e}` }));
        }
        break;
      case 'wait_entry': {
        if (!v.meta.spaceNo) break;               // 入口业务授权：必须有 spaceNo
        if (!this._gatePassable('in')) break;     // 物理条件：OPEN/KEEP_OPEN/MANUAL_OPEN/故障人工抬杆
        if (!this._leadClear(v)) break;           // 前车未拉开距离
        // Z1 双门控：预约 + 道闸
        if (!this._requestZone(v, 'Z1', this.simTime + 0.4)) break;
        const plan = this._planEntry(v.meta.spaceNo, v.dims.len, this._visualLen(v));
        if (!plan) { this._warn(v, `无入场路径 ${v.meta.spaceNo}`); break; }
        if (!plan.zones.length) plan.zones = ['Z1'];
        else if (!plan.zones.includes('Z1')) plan.zones.unshift('Z1');
        this._assignPlan(v, plan);
        v.state = 'to_space';
        v.cruise = QK('straightSpeed');
        v.meta._zoneIn = false; v.meta._passedIn = false;
        this._emit({ type: 'scn', msg: `入场 ${v.vid} → ${v.meta.spaceNo}（${plan.zones.join('→')}）` });
        break;
      }
      case 'to_space':
        this._detectGate(v, 'in');
        if (v.done) {
          v.state = 'parked';
          v.cruise = 0;
          v.hardStop = false;
          const spaceNo = v.meta.spaceNo;
          const sp = this._spaceInfo(spaceNo);
          if (sp) sp.status = 'occupied';
          const stay = Number(v.meta?.duration) || 0;
          v.meta.parkedAtSim = this.simTime;
          const schedule = this._computeExitSchedule(v, this.simTime);
          v.meta.entryTravel = schedule.entryTravel;
          v.meta.exitTravelEst = schedule.exitTravelEst;
          v.meta.scheduledExitAt = schedule.scheduledExitAt;
          v.meta.autoExitAt = stay > 0 ? schedule.autoExitAt : null;
          v.meta.exitGateAtSim = null;
          v.meta.gateToGateActual = null;
          this._applyParkedVisualOffset(v);
          this.markers?.setOccupied?.(spaceNo, true);
          this._releaseZone(v, 'Z1');
          this._releaseRemainingZones(v);
          this.onEvent({ type: 'parked', id: v.id, vid: v.vid, plate: v.meta.plate, space: spaceNo,
            entryGateAtSim: v.meta.entryGateAtSim ?? null, parkedAtSim: v.meta.parkedAtSim,
            entryTravel: v.meta.entryTravel, exitTravelEst: v.meta.exitTravelEst,
            scheduledExitAt: v.meta.scheduledExitAt, autoExitAt: v.meta.autoExitAt });
          this._emit({ type: 'scn', msg: `已入库 ${v.meta.plate || v.vid} @ ${spaceNo}` });
        }
        break;
      case 'to_exit':
        this._detectGate(v, 'out');
        if (v.done && !v.meta._arrivedOut) {
          v.meta._arrivedOut = true;
          v.state = 'wait_exit';
          v.cruise = 0;
          // 保留 Z2（出口杆区）预约：planExit 的 zones 以 Z2 收尾，等待缴费后直接使用
          for (const rid of [...v.reservedZones]) if (rid !== 'Z2') this._releaseZone(v, rid);
          this.onEvent({ type: 'arrived', gate: 'out', id: v.id, vid: v.vid, plate: v.meta.plate });
          this._reportArrival(v, 'out').catch((e) => this._emit({
            type: 'warn', msg: `出口到达上报失败 ${e?.message || e}` }));
        }
        break;
      case 'wait_exit': {
        if (!v.meta.exitAuthorized) break;        // 出口业务授权：正常计费/免费/作废完成
        if (!this._gatePassable('out')) break;    // 物理条件（人工抬杆但未授权也不放行）
        if (!this._leadClear(v)) break;
        if (!this._requestZone(v, 'Z2', this.simTime + 0.4)) break;
        const legs = this._exitContinueLegs(v);
        v.setLegs(legs, { snapFirst: false });
        v.zones = ['Z2']; v.zoneCursor = 0; v.zoneReleased = new Set();
        v.state = 'leaving';
        v.cruise = QK('straightSpeed');
        v.meta._zoneOut = false; v.meta._passedOut = false;
        this._emit({ type: 'scn', msg: `驶离出场道闸 ${v.vid}` });
        break;
      }
      case 'DENIED_REVERSE_WAIT_ENTRY_GATE': {
        // 无后车：先确认入口杆已落杆，再沿入口车道倒车离场。
        if (!this._deniedGateReady('in', 'close', v)) break;
        const targetZ = this.outerSouthZ + 8;
        v.setLegs([{ kind: 'line', ax: v.pos.x, az: v.pos.z, bx: v.pos.x,
          bz: Math.max(v.pos.z + 2, targetZ), reverse: true }], { snapFirst: false });
        v.state = 'denied_reverse';
        v.cruise = QK('straightSpeed') * 0.8;
        this._emit({ type: 'scn', msg: `黑名单车倒车离场 ${v.vid}` });
        break;
      }
      case 'denied_reverse':
        if (v.done) {
          v.state = 'DENIED_REVERSE_CLOSE_GATE';
          v.cruise = 0;
          this._requestDeniedGate('in', 'close', v);
        }
        break;
      case 'DENIED_REVERSE_CLOSE_GATE':
        if (!this._deniedGateReady('in', 'close', v)) break;
        v.state = 'gone';
        this._releaseDeniedQueue(v);
        this._emit({ type: 'entry_denied_departed', id: v.id, vid: v.vid, plate: v.meta.plate,
          reason: v.meta.deniedReason, mode: 'reverse_out', no_record: true });
        this._reportBlacklistResolution(v, 'reversed');
        break;
      case 'DENIED_BYPASS_WAIT_ENTRY_GATE':
        // 有后车：等入口杆物理可通行，再走正常入口/场内车道绕过。
        if (!this._deniedGateReady('in', 'open', v)) break;
        v.setLegs(this._deniedBypassLegs(v), { snapFirst: false });
        v.state = 'DENIED_BYPASS_ROUTE';
        v.cruise = QK('straightSpeed');
        this._emit({ type: 'scn', msg: `黑名单车经入口/场内车道绕行 ${v.vid}` });
        break;
      case 'DENIED_BYPASS_ROUTE':
        // 后车仍在入口等待时不能落入口杆，否则后车拿到 space_assigned 也过不去；
        // 等后车队列清空/后端正常收杆，再补 close。
        if (!v.meta._deniedEntryClosed && this._entryQueueCount() === 0
            && v.tailPos && v.tailPos().z < this.entry.gateZ - .5) {
          v.meta._deniedEntryClosed = true;
          this._requestDeniedGate('in', 'close', v);
        }
        if (v.done) {
          v.state = 'DENIED_BYPASS_WAIT_EXIT_GATE';
          v.cruise = 0;
          this._requestDeniedGate('out', 'open', v);
          this._emit({ type: 'scn', msg: `黑名单车到达出口等待区 ${v.vid}` });
        }
        break;
      case 'DENIED_BYPASS_WAIT_EXIT_GATE':
        if (!this._deniedGateReady('out', 'open', v)) break;
        v.setLegs(this._exitContinueLegs(v), { snapFirst: false });
        v.state = 'DENIED_BYPASS_EXIT';
        v.cruise = QK('straightSpeed');
        break;
      case 'DENIED_BYPASS_EXIT':
        if (v.done) {
          v.state = 'DENIED_BYPASS_CLOSE_GATE';
          v.cruise = 0;
          this._requestDeniedGate('out', 'close', v);
        }
        break;
      case 'DENIED_BYPASS_CLOSE_GATE':
        if (!this._deniedGateReady('out', 'close', v)) break;
        v.state = 'gone';
        this._releaseDeniedQueue(v);
        this._emit({ type: 'entry_denied_departed', id: v.id, vid: v.vid, plate: v.meta.plate,
          reason: v.meta.deniedReason, mode: 'forward_then_exit', no_record: true });
        this._reportBlacklistResolution(v, 'entered_then_left');
        break;
      case 'leaving':
        this._detectGate(v, 'out');
        if (v.done) {
          v.state = 'gone';
          this._releaseZone(v, 'Z2');
          this._releaseRemainingZones(v);
          this.markers?.setOccupied?.(v.meta.spaceNo, false);
          const sp = this._spaceInfo(v.meta.spaceNo);
          if (sp) sp.status = 'free';
          Rest.left(v.id, 'out').catch(() => {});
          this.onEvent({ type: 'left', id: v.id, vid: v.vid, plate: v.meta.plate });
        }
        break;
      default:
        break;
    }
  }

  _isFirstHeld(v) {
    let first = null;
    for (const o of this.vehicles.values()) {
      if (o.state !== 'hold_entry') continue;
      if (!first || o.pos.z > first.pos.z) first = o;  // 越靠南（z 大）越晚到
    }
    // 队列中最早到达者 = z 最小（最靠近场前开口）
    let best = null;
    for (const o of this.vehicles.values()) {
      if (o.state !== 'hold_entry') continue;
      if (!best || o.pos.z < best.pos.z) best = o;
    }
    return best === v;
  }

  /** 前车是否已拉开足够距离（启动下一段前用，避免起步顶前车；P2-17 用视觉车长）。 */
  _leadClear(v) {
    const d = v.dirVec();
    const vLen = this._visualLen(v);
    const vWid = this._visualWid(v);
    const stopDist = (QK('minFollowGap') || 1.0) + (vLen / 2);
    for (const o of this.vehicles.values()) {
      if (o === v || !o.model) continue;
      const rx = o.pos.x - v.pos.x, rz = o.pos.z - v.pos.z;
      const along = rx * d.x + rz * d.z;
      const perp = Math.abs(rx * d.z - rz * d.x);
      if (along <= 0 || perp > Math.max(2.0, (vWid + this._visualWid(o)) / 2 + 0.6)) continue;
      const gap = along - (vLen / 2 + this._visualLen(o) / 2);
      if (gap < stopDist) return false;
    }
    return true;
  }

  // =====================================================================
  // 冲突区预约（R1/R2/R4）
  // =====================================================================
  _zonesTouch(a,b,spaceNo) {
    const A=this._zoneRect(a,spaceNo),B=this._zoneRect(b,spaceNo);
    return A && B && A.xmin<=B.xmax && A.xmax>=B.xmin && A.zmin<=B.zmax && A.zmax>=B.zmin;
  }

  _requestZone(v, zoneId, tEnter) {
    if (!zoneId) return true;
    if (v.reservedZones.has(zoneId)) return true;
    const rect=this._zoneRect(zoneId,v.meta.spaceNo);
    for(const other of this.vehicles.values()) {
      if(other===v || other.state==='parked') continue;
      for(const held of other.reservedZones || []) {
        const otherRect=this._zoneRect(held,other.meta.spaceNo);
        const overlapping=rect && otherRect && rect.xmin<otherRect.xmax && rect.xmax>otherRect.xmin && rect.zmin<otherRect.zmax && rect.zmax>otherRect.zmin;
        if((held===zoneId || (zoneId.startsWith('Z7')&&held.startsWith('Z7')&&overlapping)) && otherRect && Collider.boxIntersectsRect(other.poseBox(),otherRect)) return false;
      }
    }
    const t0 = Math.max(this.simTime, tEnter ?? this.simTime);
    const eta = Math.max(0, t0 - this.simTime);
    // 窗口需覆盖“等待 + 出发 + 穿越”全过程：基值 + ETA + 缓冲，避免车到达时旧窗口已过期
    // （R3 ReservationTable 对同车重叠窗口幂等、不会续期，因此必须一次申请足够长）
    const hold = this._zoneHold(zoneId) + Math.min(45, eta) + 6;
    let r = null;
    try {
      r = this.reservations?.request?.(v.id, zoneId, t0, hold, {
        direction: v.state === 'to_exit' || v.state === 'leaving' ? 'out' : 'in',
        noPreempt:true,
      });
    } catch (e) {
      this._warn(v, `预约表异常 ${zoneId}: ${e}`);
      return false;
    }
    const granted = !!(r && (r.granted === true || r.ok === true
      || (r.granted === undefined && r.blocked === false)));
    if (granted) {
      v.reservedZones.add(zoneId);
      if (v.restoreMissingZones?.delete(zoneId) && !v.restoreMissingZones.size) {
        v.hardStop = false;
      }
      if (!v.zoneWindows) v.zoneWindows = new Map();
      v.zoneWindows.set(zoneId, { t0, t1: Number(r.until) || (t0 + hold * 1.3) });
      v.zoneBlocked = null;
      v.zoneBlockedSince = null;
      this._emit({ type: 'scn', msg: `预约授予 ${v.vid} ${zoneId}` });
    } else {
      if (!v.zoneBlocked) v.zoneBlockedSince = this.simTime;
      v.zoneBlocked = zoneId;
      v.until = r?.until ?? null;
    }
    return granted;
  }

  _releaseZone(v, zoneId) {
    if (!zoneId || !v.reservedZones.has(zoneId)) return;
    try { this.reservations?.release?.(v.id, zoneId); } catch (_) { /* ignore */ }
    v.reservedZones.delete(zoneId);
    v.zoneWindows?.delete(zoneId);
    this._emit({ type: 'scn', msg: `预约释放 ${v.vid} ${zoneId}` });
  }

  _releaseRemainingZones(v) {
    for (const id of [...v.reservedZones]) this._releaseZone(v, id);
    // 取消尚未进入的预约窗口（停车/驶离后不再占用）
    try {
      if (typeof this.reservations?.releaseAll === 'function') this.reservations.releaseAll(v.id);
    } catch (_) { /* ignore */ }
    v.reservedZones.clear();
    if (v.zones) v.zoneCursor = v.zones.length;
  }

  /** 每帧：按 planner.zones 顺序预约下一冲突区；BLOCKED 记录以便 _constrain 停车。 */
  _reserveNext(v) {
    v.bundleBlocked=false;
    if(!v.zones?.length || ['parked','wait_entry','wait_exit','gone','idle','hold_entry'].includes(v.state)) {
      v.nextZoneId=null;v.nextZoneDist=Infinity;return;
    }
    v.zoneReleased ||= new Set();v.zoneEntered ||= new Set();
    let cursor=v.zoneCursor || 0;
    while(cursor<v.zones.length && v.zoneReleased.has(v.zones[cursor])) cursor++;
    v.zoneCursor=cursor;
    if(cursor>=v.zones.length){v.nextZoneId=null;v.nextZoneDist=Infinity;return;}
    const current=v.zones[cursor],rect=this._zoneRect(current,v.meta.spaceNo);
    const distance=rect ? Collider.boxRectDistance(v.poseBox(),rect) : 0;
    v.nextZoneId=current;v.nextZoneDist=distance;
    const direction=v.dirVec();
    const preceding=[...this.vehicles.values()].some(other=>{
      if(other===v || !['approach','to_space','to_exit','leaving'].includes(other.state) || other.zoneReleased?.has(current) || !other.zones?.includes(current)) return false;
      const dx=other.pos.x-v.pos.x,dz=other.pos.z-v.pos.z;
      const ahead=dx*direction.x+dz*direction.z;
      const lateral=Math.abs(dx*direction.z-dz*direction.x);
      return ahead>0 && lateral<2.4 && Math.cos(v.theta-other.theta)>.8 &&
        rect && Collider.boxRectDistance(other.poseBox(),rect)<distance-.1;
    });
    if(preceding && !v.zoneEntered.has(current)) {
      if(v.reservedZones.has(current)) this._releaseZone(v,current);
      v.bundleBlocked=true;return;
    }
    const eta=distance/Math.max(.8,v.speed || v.cruise || 1);
    if(eta>this.options.zoneLookaheadS && !v.reservedZones.has(current)) return;
    // Acquire touching areas as a single bundle before any motion into them.
    // This includes stall -> wing -> junction: stopping between overlapping areas is unsafe.
    const group=[current];
    for(let i=cursor+1;i<v.zones.length;i++) {
      if(!this._zonesTouch(group.at(-1),v.zones[i],v.meta.spaceNo)) break;
      group.push(v.zones[i]);
    }
    const acquired=[];
    for(const zone of group) {
      const already=v.reservedZones.has(zone);
      if(!this._requestZone(v,zone,this.simTime)) {
        for(const fresh of acquired) this._releaseZone(v,fresh);
        v.bundleBlocked=true;
        return;
      }
      if(!already) acquired.push(zone);
    }
    v.zoneBlocked=null;v.zoneBlockedSince=null;
  }

  /** 每帧：车盒完全离开冲突区（含余量）→ 释放；同时推进 zoneCursor。 */
  _releaseOnExit(v) {
    if (!v.zones || !v.zones.length) return;
    if (!v.zoneEntered) v.zoneEntered = new Set();
    if (!v.zoneReleased) v.zoneReleased = new Set();
    for (const id of [...v.reservedZones]) {
      const rect = this._zoneRect(id, v.meta.spaceNo);
      if (!rect) { this._releaseZone(v, id); continue; }
      const padded = Collider.expandRect(rect, ZONE_RELEASE_PAD);
      const inside = Collider.boxIntersectsRect(v.poseBox(), padded);
      // 只有“进入过该区”之后离开才释放，避免提前预约的区被误判为已通过
      if (inside) v.zoneEntered.add(id);
      else if (v.zoneEntered.has(id)) {
        v.zoneReleased.add(id);
        this._releaseZone(v, id);
      }
    }
  }

  // =====================================================================
  // 速度约束
  // =====================================================================
  _constrain(v, vs) {
    if (['parked', 'wait_entry', 'wait_exit', 'hold_entry', 'gone', 'idle',
      'DENIED_REVERSE_WAIT_ENTRY_GATE', 'DENIED_REVERSE_CLOSE_GATE',
      'DENIED_BYPASS_WAIT_ENTRY_GATE', 'DENIED_BYPASS_WAIT_EXIT_GATE',
      'DENIED_BYPASS_CLOSE_GATE'].includes(v.state)) {
      v.cruise = 0;
      return;
    }
    let cruise = QK('straightSpeed');

    // 1) 闸杆硬约束：杆不可通行且车头逼近杆平面 → 停车（越过车头后不再拦，避免停在杆下）
    let gateId = v.state === 'leaving' ? 'out' : (v.state === 'to_space' ? 'in' : null);
    if (v.state === 'DENIED_BYPASS_ROUTE') gateId = 'in';
    if (v.state === 'DENIED_BYPASS_EXIT') gateId = 'out';
    if (gateId) {
      const g = this._gateInfo(gateId);
      const nose = v.nosePos().z;
      const nx = -Math.sin(v.theta), nz = -Math.cos(v.theta); // 车头方向
      const movingToward = gateId === 'in' ? nz < -0.15 : nz > 0.15;
      const before = (gateId === 'in' ? nose > g.gateZ : nose < g.gateZ) && movingToward;
      const distNose = Math.abs(nose - g.gateZ);
      if (!this._gatePassable(gateId) && before && distNose < 2.2) cruise = 0;
    }

    // 2) 冲突区预约门控：未授予且在区外 → 停车（“BLOCKED 停在区外”）
    if (v.nextZoneId && !v.reservedZones.has(v.nextZoneId)) {
      if (v.nextZoneDist < this.options.zoneStopMargin) cruise = 0;
    }
    if(v.bundleBlocked && v.nextZoneDist < this.options.zoneStopMargin) cruise=0;
    // 3) R1 两段式：即将进入当前区（<1.2m）时，再下一区也必须已预约，否则停在区外
    if (v.nextZoneId && v.reservedZones.has(v.nextZoneId) && v.nextZoneDist < TWO_PHASE_DIST) {
      const cursor = v.zoneCursor ?? 0;
      const k2 = cursor + 1;
      if (k2 < (v.zones?.length || 0) && !v.reservedZones.has(v.zones[k2]) && this._zonesTouch(v.nextZoneId,v.zones[k2],v.meta.spaceNo)) cruise = 0;
    }
    // 4) 静态碰撞体安全网
    const hit = this._staticHit(v);
    if (hit) {
      cruise = 0;
      this._warn(v, `静态碰撞体接近：${hit.id || hit.kind || 'collider'}`);
      this._emit({ type: 'static_hit', id: v.id, vid: v.vid, collider: hit.id || hit.kind || null });
    }

    // 5) 跟车 + 车-车碰撞最后防线（新路网无对向共线，仅同向/交叉预约后的安全余量）
    const nx = -Math.sin(v.theta), nz = -Math.cos(v.theta);   // 车头方向
    const stopDist = (QK('minFollowGap') || 1.0) + v.speed * 0.8;
    const MOVING = ['approach', 'to_space', 'to_exit', 'leaving'];
    // P2-17：停在停止线/出口等待的车也是“前车”，必须纳入视觉跟车间距；
    // 否则后车会一直开到自己的停止线，OBB 刚好接触但视觉包围盒可能已重叠（旧问题）。
    const FOLLOW_TARGETS = [...MOVING, 'wait_entry', 'wait_exit'];
    for (const o of vs) {
      if (o === v || !o.model) continue;
      // 车-车碰撞（AABB 粗筛 + OBB 精判）：仅“后车”停车，前车继续驶离以解开互锁
      if (this._pairCollide(v, o)) {
        const ahead = ((o.pos.x - v.pos.x) * nx + (o.pos.z - v.pos.z) * nz) > 0;
        if (ahead) {
          cruise = 0;
          this._warn(v, `碰撞安全网：${o.id}`);
          this._emit({ type: 'collision_blocked', id: v.id, vid: v.vid, other: o.vid || o.id });
        }
        continue;
      }
      // 同向跟车间距只对“队列/运动中”的车生效；相邻车位已停好的车不应成为跟车目标
      if (!FOLLOW_TARGETS.includes(o.state) || Math.cos(v.theta-o.theta)<.8) continue;
      const rx = o.pos.x - v.pos.x, rz = o.pos.z - v.pos.z;
      const along = rx * nx + rz * nz;
      const perp = Math.abs(rx * nz - rz * nx);
      const latLimit = (this._visualWid(v) + this._visualWid(o)) / 2 + 0.6;
      if (along <= 0 || perp > latLimit) continue;
      const gap = along - (this._visualLen(v) / 2 + this._visualLen(o) / 2);
      if (gap < stopDist) cruise = 0;
      else if (gap < stopDist * 2.2) cruise = Math.min(cruise, QK('turnSpeed'));
    }
    v.cruise = Math.max(0, cruise);
  }

  _staticColliderList() {
    const sc = this.scene?.staticColliders || this.scene?.colliders || this._staticColliders;
    return Array.isArray(sc) ? sc : null;
  }

  /**
   * 车-车碰撞判定：AABB 粗筛（快速排除）+ OBB/SAT 精判（消除相邻车位旋转时的角点误报）。
   * @param {import('../entities/Vehicle.js').Vehicle} a
   * @param {import('../entities/Vehicle.js').Vehicle} b
   */
  _pairCollide(a, b) {
    if (!a || !b) return false;
    if(Math.hypot(a.pos.x-b.pos.x,a.pos.z-b.pos.z)>Math.hypot(a.dims.len,a.dims.wid)/2+Math.hypot(b.dims.len,b.dims.wid)/2) return false;
    return Collider.poseOverlap(
      { x: a.pos.x, z: a.pos.z, theta: a.theta, len: a.dims.len, wid: a.dims.wid },
      { x: b.pos.x, z: b.pos.z, theta: b.theta, len: b.dims.len, wid: b.dims.wid },
    );
  }

  _staticHit(v) {
    const pose={x:v.pos.x,z:v.pos.z,theta:v.theta,...v.dims};
    for(const item of this._staticColliderList() || []) {
      const box=Collider.toBox3(item);
      if(box && Collider.poseOverlap(pose,{x:(box.min.x+box.max.x)/2,
        z:(box.min.z+box.max.z)/2,theta:0,wid:box.max.x-box.min.x,len:box.max.z-box.min.z})) return item;
    }
    return null;
  }

  _safeStep(v,dt,vehicles) {
    const before={pos:{...v.pos},theta:v.theta,dist:v.dist,legIndex:v.legIndex,
      legStart:v.legStart,done:v.done,reverseLeg:v.reverseLeg,_spin:v._spin,_steer:v._steer};
    const wheelAngles=v.wheels.map(w=>w.rotation.clone());
    v.update(dt);
    // 黑名单“有后车”绕行出场是抽象离场路径：不参与碰撞（避免与排队后车/闸杆互相卡死）。
    // 黑名单车辆也必须遵守碰撞/闸杆硬约束，禁止用 ghost 方式穿模。
    const ghostExit = false;
    const hit = ghostExit ? null
      : (this._staticHit(v) || vehicles.find(other=>other!==v && this._pairCollide(v,other)));
    if(hit) {
      Object.assign(v,before);v.speed=0;v.cruise=0;
      v.wheels.forEach((w,i)=>w.rotation.copy(wheelAngles[i]));v._applyTransform();
      this._warn(v,`安全停车：下一步与 ${hit.id || hit.kind || '障碍'} 相交`);
      this._emit({type:'motion_blocked',id:v.id,other:hit.id || hit.kind});
    }
  }

  // =====================================================================
  // 闸区/越闸判定（车头语义，审查报告 §8.8）
  // =====================================================================
  _detectGate(v, gateId) {
    const g = this._gateInfo(gateId);
    if (!g) return;
    const tail = v.tailPos().z;
    const keyP = gateId === 'in' ? '_passedIn' : '_passedOut';
    // 越闸：整车越过杆平面（入口 z 递减 → 车尾 < 杆-0.5；出口 z 递增 → 车尾 > 杆+0.5）
    const passed = gateId === 'in' ? tail < g.gateZ - 0.5 : tail > g.gateZ + 0.5;
    if (passed && !v.meta[keyP]) {
      v.meta[keyP] = true;
      if (gateId === 'in') {
        v.meta.entryGateAtSim = this.simTime;
      } else {
        v.meta.exitGateAtSim = this.simTime;
        const entryAt = Number(v.meta.entryGateAtSim);
        if (Number.isFinite(entryAt)) v.meta.gateToGateActual = this.simTime - entryAt;
      }
      Rest.passed(v.id, gateId).catch(() => {});
      this.onEvent({ type: 'passed', gate: gateId, id: v.id, vid: v.vid,
        gateAtSim: this.simTime,
        entryGateAtSim: v.meta.entryGateAtSim ?? null,
        gateToGateActual: v.meta.gateToGateActual ?? null });
      this._releaseZone(v, gateId === 'in' ? 'Z1' : 'Z2');
    }
  }

  _syncGateZones(vehicles) {
    for (const gate of ['in','out']) {
      const g=this._gateInfo(gate), half=ZONE_HALF.along || .9;
      const occupied=vehicles.some(v=>v.state!=='gone' && Collider.poseOverlap(
        {x:v.pos.x,z:v.pos.z,theta:v.theta,...v.dims},
        {x:g.x,z:g.gateZ,theta:0,len:half*2,wid:3.5}));
      this._gateOccupied[gate]=occupied;
      const report=this._zoneReports[gate];
      if(report.busy || report.ack===occupied || this.simTime<report.retryAt) continue;
      report.busy=true;
      Rest.zone(`${gate}_barrier`,occupied,null,this.ownerId,this.sessionId).then(()=>{
        report.ack=occupied;report.retryAt=0;
      }).catch(error=>{
        report.retryAt=this.simTime+1;
        this._emit({type:'warn',msg:`地感上报失败，等待重试 ${error}`});
      }).finally(()=>{report.busy=false;});
    }
  }

  // =====================================================================
  // 清理 / 统计
  // =====================================================================
  _cleanup() {
    for (const [id, v] of [...this.vehicles.entries()]) {
      if (v.state === 'gone') {
        try { this.reservations?.releaseAll?.(v.id); } catch (_) { /* ignore */ }
        v.reservedZones.clear();
        v.dispose(this.scene.scene);
        this.vehicles.delete(id);
      }
    }
  }

  counts() {
    let onLot = 0, moving = 0, queued = 0;
    for (const v of this.vehicles.values()) {
      if (['to_space', 'parked', 'to_exit', 'wait_exit', 'leaving'].includes(v.state)) onLot++;
      if (['approach', 'to_space', 'to_exit', 'leaving'].includes(v.state)) moving++;
      if (['approach', 'wait_entry', 'hold_entry'].includes(v.state)) queued++;
    }
    return { total: this.vehicles.size, onLot, moving, queued:queued+this._spawnRequests.length,
      pendingSpawn:this._spawnRequests.length };
  }

  followCandidate() {
    for (const v of this.vehicles.values()) {
      if (['approach', 'to_space', 'to_exit', 'leaving'].includes(v.state)) return v;
    }
    return [...this.vehicles.values()][0] || null;
  }

  // =====================================================================
  // 控制 / 快照占位接口
  // =====================================================================
  /** 清场（仅本地）：移除全部车辆、释放预约、复位排队计数。 */
  clearAll() {
    this._runtimeEpoch++;
    for(const request of this._spawnRequests) request.resolve(null);
    this._spawnRequests=[];this._queuedIds.clear();this._rng=seeded(this.seed);
    for (const [, v] of this.vehicles) {
      try { this.reservations?.releaseAll?.(v.id); } catch (_) { /* ignore */ }
      v.dispose(this.scene.scene);
    }
    this.vehicles.clear();
    this._remoteLoading.clear();
    this._seq = 0;
    this._queueHoldCount = 0;
    this._claimPromise=null;
    this._gateOccupied={in:false,out:false};
    this._zoneReports={in:{ack:false,busy:false,retryAt:0},out:{ack:false,busy:false,retryAt:0}};
    this._entryRetries=[];
  }

  /**
   * 复位接口（占位）：Route-v2 暂未接入后端快照，reset() = 本地清场。
   * 后续接入 /api/state 快照时在此同步 markers/space/barrier。
   */
  reset() { this.clearAll(); }

  /** Restore only vehicles reported by this browser owner. Other clients remain backend-owned. */
  async hydrate(snapshot, ownerId) {
    this.sessionId=snapshot?.session?.id || null;
    this.ownerId=ownerId;
    const saved=(snapshot?.scheduler_runtime || []).find(row=>row.owner_id===ownerId);
    if(saved?.seed != null && !this.vehicles.size && !this._spawnRequests.length) {
      this.seed=Number(saved.seed)>>>0;this._seq=Number(saved.seq)||0;
      this._rng=seeded(this.seed,saved.rng_state ?? this.seed);
      for(const request of saved.pending || []) {
        if(!request.id || this._queuedIds.has(request.id)) continue;
        this._queuedIds.add(request.id);
        this._spawnRequests.push({...request,epoch:this._runtimeEpoch,resolve:()=>{},reject:error=>this._warnGlobal(String(error))});
      }
    }
    const runtime = (snapshot?.vehicle_runtime || []).filter((r) => r.owner_id === ownerId);
    const epoch=this._runtimeEpoch;
    for (const row of runtime) {
      if (this.vehicles.has(row.vehicle_id)) continue;
      const kind = row.kind || 'blue';
      const spec = this._resolveModel(row.model, kind);
      const v = new Vehicle(row.vehicle_id, this.assets, spec);
      await v.load(this.scene.scene);
      if(epoch!==this._runtimeEpoch){v.dispose(this.scene.scene);return;}
      v.vid = row.vid || row.vehicle_id;
      v.meta = { ...(row.meta || {}), kind, plate: row.plate || row.meta?.plate || null,
        spaceNo: row.space_no || row.meta?.spaceNo || null };
      if (v.meta.plate) v.setPlate(v.meta.plate, kind);
      const pose = row.pose || { x: 0, z: 0, theta: 0 };
      if (Array.isArray(row.legs) && row.legs.length && !row.done) {
        v.setLegs(row.legs, { snapFirst: false });
        const dist = Math.max(0, Math.min(Number(row.dist) || 0, v.routeLength));
        let start = 0, index = 0;
        while (index < v.prims.length - 1 && dist >= start + v.prims[index].length) {
          start += v.prims[index++].length;
        }
        v.dist = dist;
        v.legIndex = index;
        v.legStart = start;
        v.pos = { x: pose.x, z: pose.z };
        v.theta = pose.theta;
        v._applyTransform();
      } else {
        v.placeAt(pose.x, pose.z, pose.theta);
        v.done = !!row.done;
      }
      v.state = row.state || 'parked';
      v.speed = 0;
      v.cruise = Number(row.cruise) || 0;
      this._applyParkedVisualOffset(v);
      v.zones = row.zones || [];
      v.zoneCursor = Number(row.zone_cursor) || 0;
      v.zoneEntered = new Set(row.zone_entered || []);
      v.zoneReleased = new Set(row.zone_released || []);
      v.restoreMissingZones = new Set();
      for (const zoneId of row.reserved_zones || []) {
        if (!this._requestZone(v, zoneId, this.simTime)) {
          v.hardStop = true;
          v.restoreMissingZones.add(zoneId);
          this._warn(v, `恢复预约失败 ${zoneId}，车辆保持停止`);
        }
      }
      this.vehicles.set(v.id, v);
    }
    await this.syncRemoteRuntime(snapshot?.vehicle_runtime || [], ownerId);
  }

  /** Other simulator owners are rendered as read-only traffic obstacles. */
  async syncRemoteRuntime(runtime, ownerId) {
    const epoch = this._runtimeEpoch;
    const remoteRows = (runtime || []).filter((r) => r.owner_id && r.owner_id !== ownerId);
    const wanted = new Set(remoteRows.map((r) => `remote:${r.owner_id}:${r.vehicle_id}`));
    for (const [id, v] of this.vehicles) {
      if (!v.remote || wanted.has(id)) continue;
      v.dispose(this.scene.scene);
      this.vehicles.delete(id);
    }
    for (const row of remoteRows) {
      const id = `remote:${row.owner_id}:${row.vehicle_id}`;
      const pose = row.pose;
      if (!pose) continue;
      let v = this.vehicles.get(id);
      if (!v) {
        if (this._remoteLoading.has(id)) continue;
        this._remoteLoading.add(id);
        try {
          const kind = row.kind || 'blue';
          v = new Vehicle(id, this.assets, this._resolveModel(row.model, kind));
          await v.load(this.scene.scene);
          if (epoch !== this._runtimeEpoch) {
            v.dispose(this.scene.scene);
            continue;
          }
          v.remote = true;
          v.vid = row.vid || row.vehicle_id;
          v.meta = { ...(row.meta || {}), kind, spaceNo: row.space_no || null };
          if (row.plate) v.setPlate(row.plate, kind);
          this.vehicles.set(id, v);
        } finally {
          this._remoteLoading.delete(id);
        }
      }
      v.placeAt(pose.x, pose.z, pose.theta);
      v.state = row.state || 'idle';
      v.meta.spaceNo = row.space_no || row.meta?.spaceNo || null;
    }
  }

  schedulerSnapshot() {
    return {seed:this.seed,seq:this._seq,rng_state:this._rng.state(),
      pending:this._spawnRequests.map(({id,kind,spec,imagePath,plate,expectedPlate})=>
        ({id,kind,spec,imagePath,plate,expectedPlate}))};
  }

  runtimeSnapshot() {
    return [...this.vehicles.values()].filter((v) => !v.remote).map((v) => ({
      vehicle_id: v.id, vid: v.vid, state: v.state,
      kind: v.meta?.kind || 'blue', model: v.spec?.file || null,
      plate: v.meta?.plate || null, space_no: v.meta?.spaceNo || null,
      pose: { x: v.pos.x, z: v.pos.z, theta: v.theta },
      legs: v.legs, dist: v.dist, done: v.done, cruise: v.cruise,
      zones: v.zones || [], zone_cursor: v.zoneCursor || 0,
      reserved_zones: [...(v.reservedZones || [])],
      zone_entered: [...(v.zoneEntered || [])],
      zone_released: [...(v.zoneReleased || [])],
      meta: v.meta || {},
    }));
  }

  /** 调试快照（供 window.__SIM__.dump().zones 等使用；可 JSON 序列化）。 */
  debugSnapshot() {
    const vehicles = [...this.vehicles.values()].map((v) => ({
      id: v.id, vid: v.vid, state: v.state, plate: v.meta?.plate || null,
      spaceNo: v.meta?.spaceNo || null, pos: { x: +v.pos.x.toFixed(3), z: +v.pos.z.toFixed(3) },
      theta: +v.theta.toFixed(4), speed: +v.speed.toFixed(3), legKind: v.legKind(),
      reservedZones: [...(v.reservedZones || [])], zoneCursor: v.zoneCursor ?? 0,
      zones: v.zones || [], blocked: v.zoneBlocked || null,
    }));
    return {
      simTime: +this.simTime.toFixed(3), legacy: this._legacy,
      counts: this.counts(), queue: { entry: this._entryQueueCount(), hold: this._heldCount(), pending:this._spawnRequests.length },
      vehicles, reservations: (() => { try { return this.reservations?.snapshot?.() ?? {}; } catch (_) { return {}; } })(),
    };
  }
}
