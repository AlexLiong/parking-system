// 时间窗预约表（sim/ReservationTable.js）。
//
// 纯逻辑模块：**不 import three**，可在 node --test 下直接单测。
// 对应设计报告 §8.7：冲突区（Z1..Z7）按"时间窗 + 1.3 安全系数"预约，保证同一互斥区
// 同一时刻只有一辆车；幂等申请；优先级规则；为调度器（three-scheduler）提供查询接口。
//
// 契约（contract §3.2）：
//   new ReservationTable(conflictZones)
//   request(vehicleId, zoneId, tEnter, holdSec) -> {granted, until}
//   release(vehicleId, zoneId)
//   releaseAll(vehicleId)
//   isFree(zoneId, tEnter, holdSec) -> boolean
//   snapshot() -> object
//
// 优先级（报告 §8.7.3）：
//   已进入路口（窗口已开始）> 请求者；出口方向 > 入口方向；人工/异常车 > 普通车；同级 FIFO。
//   由于本表不知道"现在"，用"冲突窗口的 t0 是否已早于本车请求 tEnter"近似判定
//   "持有者是否已进入路口"：已开始的窗口**不可抢占**；尚未开始的窗口允许更高优先级抢占。
//
// 防死锁四规则（R1 无持有并等待 / R2 不堵门口 / R3 车位私有 / R4 30s 看门狗）
// 由调度器落实；本表只提供窗口判定 + 等待队列观测（waiters）。

const EPS = 1e-9;
export const RESERVATION_SAFETY = 1.3;   // 安全系数（契约/报告 §8.7.2）
export const Z7_PREFIX = 'Z7:';          // 动态倒车入库区 id 前缀
export const Z7_MARGIN = 0.1;            // Z7 矩形互斥的安全余量（m，重叠深度需 > 该值才算冲突）

/**
 * 两个轴对齐矩形（Blender [xmin,ymin,xmax,ymax]）是否重叠超过 margin。
 * 用"重叠深度 > margin"而非"膨胀矩形"，这样恰好相邻的矩形（如 A-01 与 A-03，
 * 边界仅在 x=-31.5 相切）不会被误判为冲突。
 */
export function rectsOverlap(a, b, margin = 0) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== 4 || b.length !== 4) return false;
  const ox = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
  const oy = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
  return ox > margin && oy > margin;
}

/** 请求被拒绝的稳定原因码（便于调度器/HUD 分类）。 */
export const REJECT = Object.freeze({
  CONFLICT: 'conflict',        // 与已有（同区 id）窗口冲突且不可抢占
  Z7_OVERLAP: 'z7-overlap',    // Z7 动态区与另一车位的 Z7 矩形重叠（邻位/对面排机动带）
  INVALID: 'invalid',          // 参数非法
});

export class ReservationTable {
  /**
   * @param {object|Map<string,object>|null} conflictZones buildConflictZones(manifest) 的返回值（可省略）
   */
  constructor(conflictZones = null) {
    this.cz = conflictZones || null;
    this._zones = new Map();     // zoneId -> Window[]
    this._byVehicle = new Map(); // vehicleId -> Map<zoneId, Window>
    this._waiters = new Map();   // zoneId -> Waiter[]
    this._seq = 0;               // 全局请求序号（同级 FIFO）
    this._stats = { requests: 0, granted: 0, rejected: 0, preempted: 0, released: 0 };
  }

  /** 某冲突区的默认持有时间（秒），来自 manifest.conflict_zones[].hold_s。 */
  defaultHold(zoneId) {
    if (this.cz && typeof this.cz.holdSec === 'function') return this.cz.holdSec(zoneId);
    return 5;
  }

  /** 实际占用的时间窗长度（含 1.3 安全系数）。 */
  static window(holdSec) {
    const h = Number(holdSec);
    if (!Number.isFinite(h) || h <= 0) throw new TypeError(`ReservationTable: holdSec 非法 (${holdSec})`);
    return h * RESERVATION_SAFETY;
  }

  /** 有效优先级数值：越大越优先。 */
  static priorityOf(opts = {}) {
    const emergency = opts.emergency ? 1000 : 0;
    const base = Number.isFinite(Number(opts.priority)) ? Number(opts.priority) : 0;
    const dir = opts.direction === 'out' ? 10 : 0;   // 出口方向优先于入口方向
    return emergency + base + dir;
  }

  /**
   * 申请一个冲突区时间窗。
   * @param {string} vehicleId
   * @param {string} zoneId 形如 'Z1' 或 'Z7:A-01'
   * @param {number} tEnter 计划进入时刻（秒，调度器 simTime）
   * @param {number} holdSec 计划持有时间（秒，未乘安全系数）
   * @param {{priority?:number, direction?:'in'|'out', emergency?:boolean}} [opts]
   * @returns {{granted:boolean, until:number, reason?:string, idempotent?:boolean,
   *            blockedBy?:string, preempted?:string|null}}
   */
  request(vehicleId, zoneId, tEnter, holdSec, opts = {}) {
    const vid = String(vehicleId || '');
    const zid = String(zoneId || '');
    const t0 = Number(tEnter);
    if (!vid || !zid || !Number.isFinite(t0)) {
      this._stats.rejected++;
      return { granted: false, until: t0, reason: REJECT.INVALID };
    }
    const win = ReservationTable.window(holdSec == null ? this.defaultHold(zid) : holdSec);
    const t1 = t0 + win;
    const prio = ReservationTable.priorityOf(opts);
    this._stats.requests++;

    const list = this._zones.get(zid) || [];
    // 幂等：同车同区已有重叠窗口 → 直接返回原窗口
    const mine = list.find((w) => w.vehicleId === vid && overlaps(w.t0, w.t1, t0, t1));
    if (mine) {
      return { granted: true, until: mine.t1, idempotent: true };
    }
    // 冲突判定（同区 id 时间窗）
    const conflicts = list.filter((w) => w.vehicleId !== vid && overlaps(w.t0, w.t1, t0, t1));
    // Z7 动态区：除同 id 外，还要与"其他车位的 Z7 矩形"做重叠互斥。
    // 原因：Z7 矩形 = 车位 3×6 + 支路侧机动带 + 邻位 ±1.5 m ⇒ 相邻车位（x±3）、
    // 同列对面排（支路机动带）的矩形必然重叠；仅按 zone id 字符串互斥会漏（P1 演示阻断）。
    const z7Conflict = this._findZ7Overlap(zid, vid, t0, t1);
    if (conflicts.length === 0 && !z7Conflict) {
      this._insert(zid, vid, t0, t1, prio, opts);
      return { granted: true, until: round6(t1), preempted: null };
    }
    // 抢占：仅对"同 id 冲突"生效；Z7 矩形重叠不抢占（对方可能正在入库/出库，不可打断）
    if (!z7Conflict && !opts.noPreempt) {
      const canPreempt = conflicts.every((w) => prio > w.priority && t0 <= w.t0 + EPS);
      if (canPreempt) {
        const preempted = [];
        for (const w of conflicts) {
          this._removeWindow(zid, w);
          this._stats.preempted++;
          preempted.push(w.vehicleId);
        }
        this._insert(zid, vid, t0, t1, prio, opts);
        return { granted: true, until: round6(t1), preempted: preempted.join(',') };
      }
    }
    // 拒绝：登记等待队列（同级 FIFO 观测用）
    const conf = z7Conflict || conflicts[0];
    this._enqueue(zid, { vehicleId: vid, tEnter: t0, holdSec: holdSec == null ? this.defaultHold(zid) : holdSec, priority: prio, seq: this._seq++ });
    this._stats.rejected++;
    if (z7Conflict) {
      return {
        granted: false, until: round6(z7Conflict.w.t1), reason: REJECT.Z7_OVERLAP,
        blockedBy: z7Conflict.w.vehicleId, zoneConflict: z7Conflict.zoneId,
      };
    }
    return { granted: false, until: round6(conf.t1), reason: REJECT.CONFLICT, blockedBy: conf.vehicleId };
  }

  /**
   * 释放（幂等）。
   * @returns {boolean} 是否真的释放了窗口
   */
  release(vehicleId, zoneId) {
    const vid = String(vehicleId || '');
    const zid = String(zoneId || '');
    const list = this._zones.get(zid) || [];
    const hit = list.find((w) => w.vehicleId === vid);
    if (!hit) return false;
    this._removeWindow(zid, hit);
    this._stats.released++;
    return true;
  }

  /** 释放某车的全部窗口，返回释放数量。 */
  releaseAll(vehicleId) {
    const vid = String(vehicleId || '');
    const held = this._byVehicle.get(vid);
    if (!held) return 0;
    let n = 0;
    for (const [zid, w] of [...held.entries()]) {
      if (this._removeWindow(zid, w)) n++;
    }
    return n;
  }

  /** 指定时间窗是否空闲（同车的窗口不算冲突）。 */
  isFree(zoneId, tEnter, holdSec, opts = {}) {
    const zid = String(zoneId || '');
    const t0 = Number(tEnter);
    if (!Number.isFinite(t0)) return false;
    const win = ReservationTable.window(holdSec == null ? this.defaultHold(zid) : holdSec);
    const t1 = t0 + win;
    const vid = opts.vehicleId == null ? null : String(opts.vehicleId);
    const list = this._zones.get(zid) || [];
    for (const w of list) {
      if (vid && w.vehicleId === vid) continue;
      if (overlaps(w.t0, w.t1, t0, t1)) return false;
    }
    // Z7：额外检查与其他车位 Z7 矩形的重叠
    if (this._findZ7Overlap(zid, vid, t0, t1)) return false;
    return true;
  }

  /** 该动态区 id 的 Blender 矩形（不可得则 null）。 */
  _zoneRect(zid) {
    const cz = this.cz;
    if (!cz) return null;
    if (typeof cz.stallRectById === 'function') return cz.stallRectById(zid);
    if (typeof cz.rect === 'function') return cz.rect(zid);
    return null;
  }

  /**
   * 查找与本次 Z7 请求在**时间窗重叠**、且**矩形重叠**的其他车辆 Z7 窗口。
   * @returns {{w:object, zoneId:string}|null}
   */
  _findZ7Overlap(zid, vid, t0, t1) {
    if (!String(zid).startsWith(Z7_PREFIX)) return null;
    const rectA = this._zoneRect(zid);
    if (!rectA) return null;                       // 无几何信息 ⇒ 退化为仅同 id 互斥
    for (const [otherId, list] of this._zones) {
      if (otherId === zid || !otherId.startsWith(Z7_PREFIX)) continue;
      const rectB = this._zoneRect(otherId);
      if (!rectB) continue;
      if (!rectsOverlap(rectA, rectB, Z7_MARGIN)) continue;
      for (const w of list) {
        if (w.vehicleId === vid) continue;
        if (overlaps(w.t0, w.t1, t0, t1)) return { w, zoneId: otherId };
      }
    }
    return null;
  }

  /** 某冲突区当前占用窗口数。 */
  size(zoneId) { return (this._zones.get(zoneId) || []).length; }

  /** 某冲突区的全部窗口（按 t0 升序）。 */
  windows(zoneId) {
    return (this._zones.get(zoneId) || []).map((w) => ({ ...w, t0: round6(w.t0), t1: round6(w.t1) }));
  }

  /** 某车持有的全部窗口 [{zoneId, t0, t1}]。 */
  heldBy(vehicleId) {
    const held = this._byVehicle.get(String(vehicleId || ''));
    if (!held) return [];
    return [...held.entries()].map(([zoneId, w]) => ({ zoneId, t0: round6(w.t0), t1: round6(w.t1) }));
  }

  /** 某冲突区的等待队列（同级 FIFO 观测）。 */
  waiters(zoneId) {
    return (this._waiters.get(String(zoneId || '')) || []).map((w) => ({ ...w }));
  }

  /** 调试快照。 */
  snapshot() {
    const zones = {};
    for (const [zid, list] of this._zones) {
      zones[zid] = list
        .slice()
        .sort((a, b) => a.t0 - b.t0 || (a.vehicleId < b.vehicleId ? -1 : 1))
        .map((w) => ({
          vehicleId: w.vehicleId, t0: round6(w.t0), t1: round6(w.t1),
          priority: w.priority, direction: w.direction || 'in', emergency: !!w.emergency,
        }));
    }
    const vehicles = {};
    for (const [vid, held] of this._byVehicle) {
      vehicles[vid] = [...held.keys()];
    }
    return {
      safety: RESERVATION_SAFETY,
      zones,
      vehicles,
      stats: { ...this._stats },
      activeWindows: [...this._zones.values()].reduce((s, l) => s + l.length, 0),
    };
  }

  /** 清空（演示复位用）。 */
  reset() {
    this._zones.clear();
    this._byVehicle.clear();
    this._waiters.clear();
    this._seq = 0;
    this._stats = { requests: 0, granted: 0, rejected: 0, preempted: 0, released: 0 };
    return this;
  }

  // ------------------------------ 内部 ------------------------------
  _insert(zid, vid, t0, t1, prio, opts) {
    const w = {
      vehicleId: vid, t0, t1, priority: prio,
      direction: opts.direction || 'in', emergency: !!opts.emergency,
    };
    if (!this._zones.has(zid)) this._zones.set(zid, []);
    const list = this._zones.get(zid);
    list.push(w);
    list.sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1 || (a.vehicleId < b.vehicleId ? -1 : 1));
    if (!this._byVehicle.has(vid)) this._byVehicle.set(vid, new Map());
    this._byVehicle.get(vid).set(zid, w);
    this._dropWaiter(zid, vid);
    this._stats.granted++;
  }

  _removeWindow(zid, w) {
    const list = this._zones.get(zid);
    if (!list) return false;
    const i = list.indexOf(w);
    if (i < 0) return false;
    list.splice(i, 1);
    if (list.length === 0) this._zones.delete(zid);
    const held = this._byVehicle.get(w.vehicleId);
    if (held) {
      held.delete(zid);
      if (held.size === 0) this._byVehicle.delete(w.vehicleId);
    }
    return true;
  }

  _enqueue(zid, waiter) {
    if (!this._waiters.has(zid)) this._waiters.set(zid, []);
    const q = this._waiters.get(zid);
    const idx = q.findIndex((x) => x.vehicleId === waiter.vehicleId);
    if (idx >= 0) q[idx] = waiter; else q.push(waiter);
    q.sort((a, b) => b.priority - a.priority || a.tEnter - b.tEnter || a.seq - b.seq);
  }

  _dropWaiter(zid, vid) {
    const q = this._waiters.get(zid);
    if (!q) return;
    const i = q.findIndex((x) => x.vehicleId === vid);
    if (i >= 0) q.splice(i, 1);
    if (q.length === 0) this._waiters.delete(zid);
  }
}

/** 时间窗是否重叠（半开区间 [t0, t1)）。 */
function overlaps(a0, a1, b0, b1) {
  return a0 < b1 - EPS && b0 < a1 - EPS;
}

const round6 = (v) => Math.round(Number(v) * 1e6) / 1e6;

export default ReservationTable;
