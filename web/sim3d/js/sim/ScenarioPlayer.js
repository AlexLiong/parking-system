// 剧本播放器（sim/ScenarioPlayer.js）：按时间轴驱动演示脚本。
//
// Route-v2 扩展（向后兼容）：
//   - spawn 步新增可选 `vid`（稳定演示 id，同时作为上报后端的 vehicle_id）与
//     `plate`/`expected_plate`（演示车牌：识别返回前即显示，识别后以后端为准）；
//     未给 vid 时按“第 N 辆入场车”生成确定性 `E<N>`，保证重播/断言可复现；
//   - exit 支持 `vid`/`vehicle`/`plate`/`space` 精确定位，或 `target:'any'|'last'|'first'|'oldest'`；
//   - 允许同一 `t` 的 spawn 与 exit 同时触发（同时进出用例）；可选 `order` 显式排序；
//   - 保留 spawn/exit/pay/inject 语义与原有重试机制（pay 未就绪自动重试）。
// 业务真理在后端：播放器只触发"物理动作"与"管理动作"，不自行开闸。
import { Rest } from '../api.js';
import { normalizePlateType, plateTypeToKind, vehicleModelFor } from '../ui/DispatchPolicy.js';

export class ScenarioPlayer {
  constructor({ scheduler, onEvent = () => {}, plateLoader = null }) {
    this.scheduler = scheduler;
    this.onEvent = onEvent;
    this.plateLoader = plateLoader;
    this.name = 'default';
    this.description = '';
    this.steps = [];
    this.loaded = false;
    this._raw = null;
    this.clock = 0;
    this.idx = 0;
    this.playing = false;
    this._retries = [];
    this._spawnSeq = 0;
    this._epoch = 0; this.pendingActions = 0; this.failures = [];
    this.duration = 0;
    this.totalVehicles = 0;
    this.completedVehicles = 0;
    this.spawnedVehicles = 0;
    this.enteredVehicles = 0;
    this.leftVehicles = 0;
    this.forecast = [];
    this._vehicleVids = new Set();
    this._leftVids = new Set();
    this._enteredVids = new Set();
    this.loaded = false;
    this.ready = true;
    this.preloadError = null;
    this.preloadFailures = [];
    this.preloadTotal = 0;
    this.preloadLoaded = 0;
  }

  /**
   * 加载剧本。新 vehicles 格式会在 load 阶段**逐车预取** `/api/sim/plate-image`
   * （plate+plate_type），保存 plate_image_url/vehicle_kind/已解码 ImageBitmap；
   * 任一辆失败 → ready=false / preloadError，HUD 与播放锁定明确报错，不静默回退 Canvas。
   */
  async load(scn) {
    this._raw = scn || {};
    this.name = this._raw.name || 'default';
    this.description = this._raw.description || '';
    const oldRaw = this._raw.steps || this._raw.events || null;
    const newVehicles = Array.isArray(this._raw.vehicles) ? this._raw.vehicles : null;
    let raw = oldRaw;
    this.preloadFailures = [];
    this.preloadTotal = 0;
    this.preloadLoaded = 0;
    if (newVehicles) {
      // 2026-10 后端格式：vehicles:[{vid,t,plate,plate_type,duration}]。
      // 每辆车是一个 spawn 时间点；duration 交给调度器在入库后自动 startExit。
      this.preloadTotal = newVehicles.length;
      raw = await Promise.all(newVehicles.map(async (v, i) => {
        const plateType = normalizePlateType(v.plate_type || v.color || 'blue');
        const step = {
          t: Number(v.t) || 0,
          action: 'spawn',
          vid: v.vid || v.vehicle_id || `V${i + 1}`,
          plate: v.plate || null,
          expected_plate: v.plate || null,
          plate_type: plateType,
          kind: plateTypeToKind(plateType),
          model: vehicleModelFor(plateType, null, i),
          vehicle_kind: null,
          duration: Number(v.duration) || 0,
          order: i,
        };
        if (!this.plateLoader) {
          step.plate_error = '缺少 /api/sim/plate-image 预取器，拒绝用旧 Canvas 车牌兜底';
        } else {
          try {
            const info = await this.plateLoader(step.plate, plateType);
            if (!info?.image || !info?.imageUrl) throw new Error('plate-image 返回空图/空 URL');
            step.plate_image = info.image;
            step.plate_image_url = info.imageUrl;
            step.vehicle_kind = info.vehicleKind || null;
            step.plate_color = info.color || null;
            step.model = info.model || vehicleModelFor(plateType, info.vehicleKind, i);
            // 不再把 kind 压成 green/blue：优先使用 vehicle_kind（sedan/hatch/van）。
            step.kind = info.vehicleKind || plateTypeToKind(plateType);
          } catch (e) {
            step.plate_error = `车牌图预取失败 ${step.plate || ''}(${plateType}): ${e?.message || e}`;
          }
        }
        if (step.plate_error) this.preloadFailures.push(step.plate_error);
        else this.preloadLoaded += 1;
        return step;
      }));
      // 后端默认剧本同时下发 steps（pay/safety_probe/inject/recover/wait）：
      // 新 vehicles 分支只生成 spawn；自动出场由 duration 触发，pay 等动作必须保留，
      // 否则车辆卡在 wait_exit。exit 动作由自动出场完成，重复执行会造成 pending 重试，
      // 因此这里过滤掉 spawn/exit，仅合并其余动作。
      if (Array.isArray(oldRaw) && oldRaw.length) {
        const extras = oldRaw
          .filter((s) => s && s.action && s.action !== 'spawn' && s.action !== 'exit')
          .map((s) => ({ ...s }));
        raw = raw.concat(extras);
      }
    }
    raw = Array.isArray(raw) ? raw : [];
    // t 是唯一时间轴；同一 t 才按数组出现顺序（不再使用后端 order 字段覆盖时间）。
    this.steps = raw
      .map((s, i) => ({ ...s, _order: i }))
      .sort((a, b) => ((Number(a.t) || 0) - (Number(b.t) || 0)) || (a._order - b._order));
    // duration 新语义：通过入口闸 → 通过出口闸；预告还需加 spawn 到入口过闸的估计行驶时间。
    this.entryTransitEstimate = (() => {
      const raw = Number(this._raw.entry_transit_est);
      return Number.isFinite(raw) && raw > 0 ? raw : 9;
    })();
    this.forecast = (newVehicles || [])
      .map((v, i) => {
        const t = Number(v.t) || 0;
        const stay = Number(v.duration) || 0;
        const exitT = t + this.entryTransitEstimate + stay;
        return stay > 0 ? { t: Math.round(exitT * 1000) / 1000,
          vid: v.vid || v.vehicle_id || `V${i + 1}`, plate: v.plate || null,
          entryTransitEstimate: this.entryTransitEstimate,
          text: `预计 ${Math.round(exitT * 1000) / 1000}s 出场/缴费 ${v.plate || ''}`
            + `（预计，不含排队/故障）` }
          : null;
      })
      .filter(Boolean)
      .sort((a, b) => a.t - b.t);
    this.totalVehicles = this.steps.filter((s) => s.action === 'spawn').length;
    let autoSeq = 0;
    this._vehicleVids = new Set(this.steps.filter((s) => s.action === 'spawn')
      .map((s) => String(s.vid || `E${++autoSeq}`)));
    const declared = Number(this._raw.duration);
    if (Number.isFinite(declared) && declared > 0) this.duration = declared;
    else if (newVehicles) {
      // 新格式总时长 = max(t + 入场估计行驶时间 + duration)，duration 为入口→出口总时长。
      this.duration = newVehicles.reduce((m, v) => Math.max(
        m, (Number(v.t) || 0) + this.entryTransitEstimate + (Number(v.duration) || 0)), 0);
    } else {
      // 旧 steps：最后动作时间（无车辆 duration 语义）。
      this.duration = this.steps.reduce((m, s) => Math.max(m, Number(s.t) || 0), 0);
    }
    this.ready = this.preloadFailures.length === 0;
    this.preloadError = this.preloadFailures.length
      ? `车牌图预取失败 ${this.preloadFailures.length}/${this.preloadTotal}：${this.preloadFailures[0]}`
      : null;
    if (this.preloadError) this.onEvent({ type: 'warn', msg: this.preloadError });
    this.loaded = true;
    this.reset();
    return this;
  }

  reset() {
    this.clock = 0; this.idx = 0; this.playing = false; this._retries = [];
    this._spawnSeq = 0;
    this._epoch++; this.pendingActions = 0; this.failures = [];
    this.completedVehicles = 0;
    this.spawnedVehicles = 0;
    this.enteredVehicles = 0;
    this.leftVehicles = 0;
    this._leftVids = new Set();
    this._enteredVids = new Set();
  }
  play() {
    if (!this.ready || this.preloadError) {
      this.onEvent({ type: 'warn', msg: this.preloadError || '剧本车牌图未就绪，禁止播放' });
      return false;
    }
    this.playing = true;
    this.onEvent({ type: 'scn', msg: `▶ ${this.name}` });
    return true;
  }
  pause() { this.playing = false; this.onEvent({ type: 'scn', msg: '⏸ 暂停' }); }
  get finished() {
    const timelineDone = this.clock >= this.duration - 1e-6;
    const actionsDone = this.idx >= this.steps.length && !this._retries.length && !this.pendingActions;
    return actionsDone && (this.duration > 0 ? timelineDone : true);
  }

  /** 车辆入口识别/授权成功：用于“已入场/总车次”进度。 */
  markVehicleEntered(idOrVidOrPlate) {
    const key = String(idOrVidOrPlate || '');
    if (!key) return false;
    const hit = [...this._vehicleVids].find((v) => v === key)
      || (this.scheduler?.vehicles?.get?.(key)?.vid)
      || (this.scheduler?.vehicles?.values?.() ?
        [...this.scheduler.vehicles.values()].find((x) => x?.meta?.plate === key)?.vid : null);
    if (!hit) return false;
    if (this._enteredVids.has(hit)) return false;
    this._enteredVids.add(hit);
    this.enteredVehicles = Math.min(this.totalVehicles, this._enteredVids.size);
    return true;
  }

  _nextForecastLabel() {
    if (!this.forecast.length) return '—';
    const hit = this.forecast.find((f) => f.t >= this.clock - 1e-6);
    return hit ? hit.text : '—';
  }

  /** 车辆 left 事件到达：用于“已完成/总车次”进度。 */
  markVehicleLeft(idOrVidOrPlate) {
    const key = String(idOrVidOrPlate || '');
    if (!key) return false;
    const hit = [...this._vehicleVids].find((v) => v === key)
      || (this.scheduler?.vehicles?.get?.(key)?.vid);
    if (!hit) return false;
    if (this._leftVids.has(hit)) return false;
    this._leftVids.add(hit);
    this.leftVehicles = Math.min(this.totalVehicles, this._leftVids.size);
    // 进度“已完成”以已派车（剧本车次已处理）为准；leftVehicles 单独保留供详情/排障。
    this.completedVehicles = Math.max(this.spawnedVehicles, this.leftVehicles);
    return true;
  }

  _nextEventLabel() {
    const s = this.steps[this.idx];
    if (!s) return '';
    const action = { spawn: '派车', exit: '出场', pay: '缴费', inject: '故障注入', recover: '恢复', safety_probe: '防砸验证', wait: '等待' }[s.action] || s.action;
    const who = s.plate || s.vid || s.target || '';
    return `t=${Number(s.t || 0).toFixed(0)}s ${action}${who ? ' ' + who : ''}`;
  }

  progress() {
    return {
      name: this.name,
      description: this.description,
      clock: this.clock,
      totalDuration: this.duration,
      totalVehicles: this.totalVehicles,
      completedVehicles: this.completedVehicles,
      spawnedVehicles: this.spawnedVehicles,
      enteredVehicles: this.enteredVehicles,
      leftVehicles: this.leftVehicles,
      exitedVehicles: this.leftVehicles,
      nextEvent: this._nextEventLabel(),
      nextForecast: this._nextForecastLabel(),
      forecast: this.forecast.slice(0, 8),
      entryTransitEstimate: this.entryTransitEstimate,
      forecastNote: '预计，不含排队/故障',
      playing: this.playing,
      finished: this.finished,
      ready: this.ready,
      preloadError: this.preloadError,
      preloadLoaded: this.preloadLoaded,
      preloadTotal: this.preloadTotal,
      idx: this.idx,
      steps: this.steps.length,
    };
  }

  /** sdt：已按速度缩放的仿真增量秒。 */
  update(sdt) {
    if (!this.playing) return;
    this.clock += sdt;
    while (this.idx < this.steps.length && (this.steps[this.idx].t || 0) <= this.clock) {
      this._fire(this.steps[this.idx++]);
    }
    this._runRetries();
    // 时间轴+动作完成后停止播放，让“重播剧本”按钮出现（不依赖下一帧点击）。
    if (this.playing && this.finished) {
      this.playing = false;
      this.onEvent({ type: 'scn', msg: '✅ 剧本播放完成' });
    }
  }

  /** 稳定 vehicle_id：显式 vid 优先；否则按“入场车序号”生成 E1/E2/...（同剧本可复现）。 */
  _stableVid(step) {
    if (step && step.vid) return String(step.vid);
    this._spawnSeq += 1;
    return `E${this._spawnSeq}`;
  }

  _fire(step) {
    const s = step || {};
    switch (s.action) {
      case 'spawn': {
        const vid = this._stableVid(s), epoch = this._epoch;
        // 新 vehicles 车牌图预取失败：明确失败并跳过 spawn，绝不用旧 Canvas 车牌冒充。
        if (s.plate_error) {
          this._fail(s.plate_error);
          this.pendingActions++;
          this.pendingActions--;
          break;
        }
        this.pendingActions++;
        const expectedPlate = s.plate || s.expected_plate || null;
        this.scheduler.spawnEntry({
          kind: s.kind || (String(s.plate_type || '').includes('green') ? 'green' : 'blue'),
          model: s.model || null,
          // 新格式 plate_image 是已解码 ImageBitmap；旧 steps 的 plate_image 可能是路径字符串。
          imagePath: typeof s.plate_image === 'string' ? s.plate_image : (s.image || null),
          imageUrl: s.plate_image_url || s.image_url || null,
          plateImage: s.plate_image && typeof s.plate_image === 'object' ? s.plate_image : null,
          vehicleKind: s.vehicle_kind || null,
          plateColor: s.plate_color || null,
          vehicleId: vid,
          vid,
          plate: s.plate || null,
          expectedPlate,
          plateType: s.plate_type || s.color || null,
          duration: Number(s.duration) || 0,
        }).then((v) => {
          if (epoch !== this._epoch) return;
          if (v) {
            v.vid = vid;
            this.spawnedVehicles = Math.min(this.totalVehicles, this.spawnedVehicles + 1);
            this.completedVehicles = Math.max(this.spawnedVehicles, this.leftVehicles);
            if (v.meta) {
              v.meta.vid = vid;
              if (s.plate_type) v.meta.plateType = s.plate_type;
              if (s.vehicle_kind) v.meta.vehicleKind = s.vehicle_kind;
              if (s.plate_image_url) v.meta.plateImageUrl = s.plate_image_url;
            }
            if (s.plate_image && typeof s.plate_image === 'object') {
              try { v.setPlateImage(s.plate_image, s.plate, s.plate_color || s.plate_type || 'blue'); }
              catch (e) { this._fail(`车牌图应用失败 ${s.plate}: ${e?.message || e}`); }
            } else if (expectedPlate && !v.meta.plate) {
              v.setPlate(expectedPlate, String(s.plate_type || '').includes('green') ? 'green' : 'blue');
            }
          }
          this.onEvent({ type: 'scn', msg: `派车 ${vid} ${s.plate_type || s.kind || 'blue'}${s.vehicle_kind ? '/' + s.vehicle_kind : ''}${expectedPlate ? ' · ' + expectedPlate : ''}`,
            plate: expectedPlate, plate_type: s.plate_type, vehicle_kind: s.vehicle_kind, image_url: s.plate_image_url || null });
        }).catch(e => { if (epoch === this._epoch) this._fail(`派车失败 ${e}`); })
          .finally(() => { if (epoch === this._epoch) this.pendingActions--; });
        break;
      }
      case 'exit':
        this._retry(() => {
          const id = this._resolveExitTarget(s);
          const v = this.scheduler.vehicles.get(id);
          if (!v || v.state !== 'parked') return false;
          const started = this.scheduler.startExit(id);
          if (!started) return false;
          this.onEvent({ type:'scn', msg:`出场 ${v.vid || id}` });
          return true;
        }, s.timeout || 240, `出场 ${s.vid || s.target || ''}`);
        break;
      case 'pay':
        this._retry(() => this._pay(s), s.timeout || 240, `缴费 ${s.vid || ''}`);
        break;
      case 'recover':
        if (s.kind === 'barrier') this._retry(() => Rest.barrierFault(s.gate || 'in', 'clear').then(()=>true),30,'道闸恢复');
        else Rest.cameraRecover(s.gate || 'in').catch(() => {});
        break;
      case 'inject':
        if (s.kind === 'barrier') {
          this._retry(() => Rest.barrierFault(s.gate || 'in', 'inject', s.mode || 'ack_timeout').then(()=>true),30,'道闸故障注入');
          break;
        }
        if (s.kind === 'recover') Rest.cameraRecover(s.gate || 'in').catch(() => {});
        else Rest.cameraInject(s.gate || 'in', s.reason || 'scenario').catch(() => {});
        this.onEvent({ type: 'scn', msg: `摄像头${s.kind === 'recover' ? '恢复' : '故障'}(${s.gate || 'in'})` });
        break;
      case 'safety_probe':
        this._retry(async () => {
          const gate=s.gate || 'in';
          if (!this.scheduler._gateOccupied?.[gate] || !this.scheduler._zoneReports?.[gate]?.ack) return false;
          try { await Rest.closeBarrier(gate); }
          catch (error) {
            if (error.status === 409) { this.onEvent({type:'scn',msg:'防砸验证：杆下有车，安全拒绝落杆'}); return true; }
            throw error;
          }
          throw new Error('杆下有车却接受了落杆');
        },s.timeout || 60,'杆下防砸验证');
        break;
      case 'wait':
        break;   // 纯时间锚点（便于“同时进出”分组文档化）
      default:
        this.onEvent({ type: 'warn', msg: `未知剧本动作: ${s.action}` });
    }
  }

  _resolveExitTarget(s) {
    if (s.vid) return this._byVid(s.vid) || s.vid;
    if (s.vehicle) return s.vehicle;
    if (s.plate) return s.plate;
    if (s.space) {
      const v = this._bySpace(s.space);
      if (v) return v.id;
    }
    return this._pickParked(s.target);
  }

  _byVid(vid) {
    if (!this.scheduler || !this.scheduler.vehicles) return null;
    for (const v of this.scheduler.vehicles.values()) {
      if (v.vid === vid || v.id === vid) return v.id;
    }
    return null;
  }

  _bySpace(spaceNo) {
    if (!this.scheduler || !this.scheduler.vehicles) return null;
    for (const v of this.scheduler.vehicles.values()) {
      if (v.meta && v.meta.spaceNo === spaceNo) return v;
    }
    return null;
  }

  _pickParked(how) {
    const parked = [...this.scheduler.vehicles.values()].filter((v) => v.state === 'parked');
    if (!parked.length) return null;
    if (how === 'last') return parked[parked.length - 1].id;
    if (how === 'first') return parked[0].id;
    if (how === 'oldest') return parked[0].id;
    return parked[0].id;   // 'any'（默认）
  }

  async _pay(step) {
    const id = step.vid ? this._byVid(step.vid) : null;
    const v = id ? this.scheduler.vehicles.get(id) : this.scheduler.pendingExitVehicle();
    // 2026-10-31 自动结算后，剧本 pay 是幂等兼容动作：车已自动缴费/已出场时直接视为成功，
    // 不能因“车已不在 wait_exit”反复重试到超时（实现 A 契约：/api/pay 幂等）。
    if (!v) return true;
    if (v.meta?.exitAuthorized || ['leaving', 'gone'].includes(v.state)) return true;
    if (v.state !== 'wait_exit' || !v.meta.recordId) return false;
    const epoch = this._epoch;
    await Rest.pay(v.meta.recordId, 'mock_scan');
    if (epoch !== this._epoch) return true;
    this.onEvent({ type:'scn', msg:`缴费 ¥${v.meta.amount ?? ''} (${v.meta.plate || v.vid || v.id})` });
    return true;
  }

  _fail(message) {
    this.failures.push(message); this.onEvent({type:'warn',msg:message});
  }

  _retry(fn, timeout, label) {
    this._retries.push({at:this.clock,deadline:this.clock+timeout,fn,label,busy:false,done:false});
  }

  _runRetries() {
    const epoch = this._epoch;
    for (const r of this._retries) {
      if (r.done || r.busy || this.clock < r.at) continue;
      if (this.clock > r.deadline) { r.done = true; this._fail(`${r.label}超时`); continue; }
      r.busy = true;
      Promise.resolve().then(() => epoch === this._epoch ? r.fn() : false).then(ok => { if (epoch === this._epoch && ok) r.done=true; })
        .catch(error => { if (epoch === this._epoch) r.lastError=String(error); })
        .finally(() => { if (epoch === this._epoch) { r.busy=false; r.at=this.clock+1.2; } });
    }
    this._retries = this._retries.filter(r=>!r.done);
  }

  /** 调试快照（可 JSON 序列化；兼容 __SIM__.dump 的可选扩展）。 */
  debugSnapshot() {
    return {
      name: this.name, playing: this.playing, clock: +this.clock.toFixed(2),
      idx: this.idx, steps: this.steps.length,
      nextSteps: this.steps.slice(this.idx, this.idx + 3)
        .map((s) => ({ t: s.t, action: s.action, vid: s.vid || null })),
      pendingRetries: this._retries.length, pendingActions:this.pendingActions, failures:this.failures.slice(), finished:this.finished,
    };
  }
}
