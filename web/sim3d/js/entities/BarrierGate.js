// 道闸实体（entities/BarrierGate.js）：执行后端下发的 barrier_cmd 动画，完成后回执
// barrier_ack（§6.2 前后端职责：后端裁决，前端执行动画）。
// Arm.rotation.z: 0（落杆）→ +82°（抬杆），easeOutCubic 抬 / easeInQuad 落，打断即反向。
import * as THREE from 'three';
import { ARM_OPEN_RAD, GATE_PLACEMENT, deg2rad } from '../config.js';

const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
const easeInQuad = (t) => t * t;

export class BarrierGate {
  /**
   * @param {'in'|'out'} gate
   * @param {import('../scene/AssetFactory.js').AssetFactory} assets
   * @param {(gate:string,state:'open'|'closed')=>void} ackCb 动画完成回执
   * @param {{gateX:number, gateY:number, duration?:number}} opts gateY=Blender Y(北)
   */
  constructor(gate, assets, ackCb, opts) {
    this.gate = gate;
    this.assets = assets;
    this.ackCb = ackCb;
    this.duration = opts.duration ?? 1.5;
    this.openDuration = this.duration; this.closeDuration = this.duration;
    this.lastCmdId = null; this._motionDuration = this.duration;
    this.gateX = opts.gateX;
    this.gateY = opts.gateY;      // Blender Y（北）；世界 z = -gateY
    this.state = 'closed';        // closed | opening | open | closing
    this._from = 0; this._to = 0; this._t = 0; this._animating = false;
    // 后端权威物理可通行语义（barrier_cmd/barrier_state.passable）优先于本地动画状态。
    this._passableOverride = null;
    this.manual = false;
    this.authorization = null;
    this.keepOpen = false;
    this.root = new THREE.Group();
    this.root.name = `Barrier_${gate}`;
    this.arm = null;
    this.screen = null;
    this._ready = this._build();
  }

  async _build() {
    const model = await this.assets.cloneModel('barrier_gate.glb');
    const place = GATE_PLACEMENT[this.gate];
    this.root.position.set(this.gateX + place.dx, 0, -this.gateY);
    this.root.rotation.y = deg2rad(place.rotDeg);
    this.root.add(model);
    this.arm = model.getObjectByName('Arm');
    if (this.arm) this.arm.rotation.z = 0;   // 初始落杆
    // 识别显示屏：换成运行时 CanvasTexture（欢迎/费用）
    const screenMesh = model.getObjectByName('DisplayScreen');
    if (screenMesh) {
      // The GLB uses atlas UVs. An owned runtime plane presents the complete
      // canvas on the original physical face without changing source geometry.
      screenMesh.geometry.computeBoundingBox();
      const box = screenMesh.geometry.boundingBox, size = box.getSize(new THREE.Vector3());
      this._screenHeight = Math.round(384 * size.y / size.x);
      this.screen = new THREE.Mesh(new THREE.PlaneGeometry(size.x, size.y));
      this.screen.name = 'GateScreenCanvas';
      box.getCenter(this.screen.position); this.screen.position.z = box.min.z - .001;
      this.screen.rotation.y = Math.PI;
      screenMesh.add(this.screen);
      this.setScreen([this.gate === 'in' ? '入口' : '出口', '请刷卡/识别']);
    }
    return model;
  }

  get ready() { return this._ready; }

  /** 世界坐标下的杆区中心（供 ZoneOccupied 检测）。 */
  zoneCenter() { return { x: this.gateX, z: -this.gateY }; }

  /**
   * 更新道闸屏。opts.photo 为已解码抓拍图片（P2-02）：有照片时走照片屏，
   * 否则保持原多行文字屏；两者复用同一 CanvasTexture/材质。
   */
  setScreen(lines, opts = {}) {
    if (!this.screen) return;
    this.screenLines = lines.slice();
    this.screenPhoto = opts.photo || null;
    const sizeOpts = { texture: this._screenMaterial?.map, w: 384, h: this._screenHeight || 552, fg: '#39ff88' };
    const tex = opts.photo
      ? this.assets.photoScreenTexture(lines, opts.photo, {
        ...sizeOpts, fg: '#e9fbff', label: opts.photoLabel || '出口抓拍' })
      : this.assets.screenTexture(lines, sizeOpts);
    if (!this._screenMaterial) this._screenMaterial = new THREE.MeshBasicMaterial({ map: tex, toneMapped: false });
    this.screen.material = this._screenMaterial;
  }

  dispose() {
    this._screenMaterial?.map?.dispose(); this._screenMaterial?.dispose();
    this._screenMaterial = null;
    this.screen?.geometry?.dispose();
  }

  /** 处理后端指令。 */
  command(cmd, reason = '', cmdId = null, duration = null, target = null) {
    this._passableOverride = null;
    if (cmdId && cmdId === this.lastCmdId) {
      if (!this._animating && ['open', 'closed', 'stopped'].includes(this.state))
        this.ackCb(this.gate, this.state, cmdId, this.armAngleDeg() / 82);
      return;
    }
    this.lastCmdId = cmdId;
    this.duration = Number(duration) > 0 ? Number(duration) :
      (cmd === 'close' ? this.closeDuration : this.openDuration);
    switch (cmd) {
      case 'open': this._animateTo('opening', 'open', ARM_OPEN_RAD); break;
      case 'close': this._animateTo('closing', 'closed', 0); break;
      case 'interrupt': this._animateTo('opening', 'open', ARM_OPEN_RAD); break; // 落杆中复抬
      case 'stop':
        this._animating = false; this.state = 'stopped';
        this.ackCb(this.gate, 'stopped', cmdId, Math.max(0, Math.min(1, this.armAngleDeg() / 82)));
        break;
      case 'fault':
        this._animating = false; this.state = 'fault';
        this.faultRaised = target === 'open';
        if (this.arm) this.arm.rotation.z = target === 'open' ? ARM_OPEN_RAD : 0;
        break;
      default: console.warn('[Barrier] 未知指令', cmd, reason);
    }
  }

  _animateTo(animState, endState, targetAngle) {
    if (!this.arm) return;
    this._from = this.arm.rotation.z;
    this._to = targetAngle;
    this._motionDuration = Math.max(.001, this.duration * Math.abs(this._to-this._from) / ARM_OPEN_RAD);
    this._t = 0;
    this.state = animState;
    this._endState = endState;
    this._animating = true;
    if (Math.abs(this._to - this._from) < 1e-6) this.update(this._motionDuration);
  }

  update(dt) {
    if (!this._animating || !this.arm) return;
    this._t += dt;
    const p = Math.min(1, this._t / this._motionDuration);
    const eased = this._endState === 'open' ? easeOutCubic(p) : easeInQuad(p);
    this.arm.rotation.z = this._from + (this._to - this._from) * eased;
    if (p >= 1) {
      this._animating = false;
      this.arm.rotation.z = this._to;
      this.state = this._endState;
      // 手动抬杆是本地物理动作，不向后端伪造 open/closed 回执。
      if (this._endState !== 'manual_open') this.ackCb(this.gate, this._endState, this.lastCmdId);
    }
  }

  get isOpen() { return this.state === 'open'; }

  /** 物理可通行：后端 passable 优先；故障人工抬杆（杆已抬起）仍可通行。 */
  get isPassable() {
    if (this.state === 'fault' && this.faultRaised === true) return true;
    if (typeof this._passableOverride === 'boolean') return this._passableOverride;
    if (this.state === 'open' || this.state === 'keep_open' || this.state === 'manual_open') return true;
    if (this.state === 'fault' || this.state === 'stopped') {
      return this.faultRaised === true || this.armAngleDeg() >= 78;
    }
    return false;
  }

  /** 回灌后端 passable/manual/authorization；passable 不改变杆位动画。 */
  setPassable(passable, meta = {}) {
    if (typeof passable === 'boolean') this._passableOverride = passable;
    if (meta.manual != null) this.manual = !!meta.manual;
    if (meta.authorization != null) this.authorization = meta.authorization;
    if (meta.keep_open != null) this.keepOpen = !!meta.keep_open;
  }

  /** 人工/黑名单物理抬杆：只动杆，不伪造后端业务授权。 */
  manualOpen() {
    this.faultRaised = true;
    this._manualOpen = true;
    this._passableOverride = true;
    this._animateTo('opening', 'manual_open', ARM_OPEN_RAD);
  }

  /** 人工/黑名单物理落杆；下一次后端 barrier_state 仍可回灌校正。 */
  manualClose() {
    this.faultRaised = false;
    this._manualOpen = false;
    this._passableOverride = false;
    this.command('close', 'manual_close');
  }

  /** 当前杆角（度，供调试/自动化测试断言动画连续性）。 */
  armAngleDeg() {
    return this.arm ? (this.arm.rotation.z * 180 / Math.PI) : null;
  }

  /** 后端权威状态回灌（barrier_state）。后端 BarrierState 枚举值为大写：
   *  CLOSED/OPENING/KEEP_OPEN/CLOSING/FAULT。
   *  Route-v2 修复：OPENING/CLOSING 是“指令已下发、等待动画回执”的瞬态，
   *  前端**不得**用它直接改杆位（否则动画会被瞬移到终点而表现为“没有动画”）；
   *  只有终态 CLOSED / OPEN / KEEP_OPEN 才同步杆位；快照恢复时传 hydration=true 可强制置位。 */
  forceState(state, { hydration = false, arm_position = null, passable = null,
    manual = null, authorization = null, keep_open = null } = {}) {
    if (passable != null) this._passableOverride = !!passable;
    else this._passableOverride = null;
    if (manual != null) this.manual = !!manual;
    if (authorization != null) this.authorization = authorization;
    if (keep_open != null) this.keepOpen = !!keep_open;
    const terminal = String(state || '').toUpperCase();
    if (terminal === 'FAULT' || terminal === 'STOPPED') {
      this._animating = false; this.state = terminal.toLowerCase();
      if (this.arm && arm_position != null && Number.isFinite(Number(arm_position))) {
        const p = Math.max(0, Math.min(1, Number(arm_position)));
        this.arm.rotation.z = p * ARM_OPEN_RAD;
        this.faultRaised = p >= .5;
      }
      return;
    }
    if (terminal === 'MANUAL_OPEN' || terminal === 'FAULT_OPEN') {
      this._animating = false;
      this.state = 'manual_open';
      this.faultRaised = true;
      this._passableOverride = true;
      if (this.arm) this.arm.rotation.z = ARM_OPEN_RAD;
      return;
    }
    if (this._animating && !hydration) return;
    const s = String(state || '').toUpperCase();
    const isFinal = s === 'OPEN' || s === 'KEEP_OPEN' || s === 'CLOSED';
    if (!isFinal) return;                 // OPENING/CLOSING/FAULT：不直接改杆位
    if (s === 'CLOSED') {
      this.state = 'closed';
      this.faultRaised = false;
      if (this.arm) this.arm.rotation.z = 0;
    } else {
      this.state = 'open';
      this.faultRaised = false;
      if (this.arm) this.arm.rotation.z = ARM_OPEN_RAD;
    }
    this._t = 0;
    this._animating = false;
  }
}
