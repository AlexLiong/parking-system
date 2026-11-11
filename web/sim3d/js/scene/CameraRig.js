// 相机装置（scene/CameraRig.js）—— route-v2 升级。
// 透视相机 + OrbitControls + 预设机位（overview / entry / exit / lprIn / lprOut / plazaN / plazaS / follow / free）。
// 机位以“审查报告 §8.5.3 关键节点”为准：入口停止线车心 -28.4（three z=28.4）、
// 出口停止线车心 -31.1（three z=31.1）、北/南分岔区 y=19.5/-13.5（three z=-19.5/+13.5）。
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { SCENE_DEFAULT, deg2rad } from '../config.js';

export const CAMERA_PRESETS = [
  { id: 'overview', label: '全局俯视' },
  { id: 'entry', label: '入口特写' },
  { id: 'exit', label: '出口特写' },
  { id: 'lprIn', label: '入口相机' },
  { id: 'lprOut', label: '出口相机' },
  { id: 'plazaN', label: '北分岔区' },
  { id: 'plazaS', label: '南分岔区' },
  { id: 'follow', label: '跟随车辆' },
  { id: 'free', label: '自由视角' },
];

/** LPR 闸口相机兜底参数（manifest.props.lpr_cameras 未就绪时使用；来自 idea2 标定）。 */
const LPR_FALLBACK = {
  in: { view_three: { x: -5.6, y: 3.405, z: 21.0 }, look_at_three: { x: -3.5, y: 0.6, z: 25.8 }, fov_vertical_deg: 30 },
  out: { view_three: { x: 5.6, y: 3.405, z: 39.0 }, look_at_three: { x: 3.5, y: 0.6, z: 33.7 }, fov_vertical_deg: 30 },
};

export class CameraRig {
  constructor(renderer, sceneCfg = {}, opts = {}) {
    this.renderer = renderer;
    this.sceneCfg = { ...SCENE_DEFAULT, ...(sceneCfg || {}) };
    this.lprCameras = Array.isArray(opts.lprCameras) ? opts.lprCameras.slice() : [];
    this.baseFov = 46;
    this.camera = new THREE.PerspectiveCamera(this.baseFov, 1, 0.3, 900);
    this.controls = new OrbitControls(this.camera, renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI * 0.495;   // 不钻到地面以下
    this.controls.minDistance = 5;
    this.controls.maxDistance = 320;
    this.controls.target.set(0, 0, 0);

    this.mode = 'overview';
    this.followTarget = null;      // Vehicle（有 .model / worldPos()）
    this._desiredPos = new THREE.Vector3();
    this._desiredTgt = new THREE.Vector3();
    this.setPreset('overview');
  }

  /** sceneCfg 热更新（layout_changed / manifest 加载后）；保持当前机位不跳。 */
  updateSceneCfg(sceneCfg) {
    this.sceneCfg = { ...this.sceneCfg, ...(sceneCfg || {}) };
  }

  /** three xz 辅助：Blender (x, y) → three (x, -y)。 */
  static three(x, blenderY = 0) { return { x, z: -blenderY }; }

  presets() {
    const s = this.sceneCfg;
    const E1 = Number(s.entry_gate_x ?? -3.5);
    const E2 = Number(s.exit_gate_x ?? 3.5);
    const inGateZ = -Number(s.gate_y ?? -25.5);              // 入口闸 three z（+25.5）
    const outGateZ = -Number(s.gate_out_y ?? s.gate_y ?? -34); // 出口闸 three z（+34）
    const inStopZ = -Number(s.stop_entry_center_y ?? -28.4);   // 入口车心停止 z（+28.4）
    const outStopZ = -Number(s.stop_exit_center_y ?? -31.1);   // 出口车心停止 z（+31.1）
    const pn = s.plaza_n || SCENE_DEFAULT.plaza_n;
    const ps = s.plaza_s || SCENE_DEFAULT.plaza_s;
    const pnZ = -Number(pn.y ?? 19.5);                        // 北分岔区 three z（-19.5）
    const psZ = -Number(ps.y ?? -13.5);                       // 南分岔区 three z（+13.5）
    return {
      // 全局俯视：从东南上方看全场（含外场排队区）
      overview: { pos: [66, 58, 92], tgt: [0, 0, 3] },
      // 入口特写：站在入口排队区西侧，看向新停止线（车头/岗亭同框）
      entry: { pos: [E1 - 13, 7.2, inStopZ + 15.5], tgt: [E1, 1.15, inStopZ - 1.0] },
      // 出口特写：站在出口外场东侧，看向新出口停止线
      exit: { pos: [E2 + 13, 7.4, outStopZ + 14.5], tgt: [E2, 1.15, outStopZ + 0.6] },
      // 北分岔区：从东南上空俯看北支路与 A/C 端口
      plazaN: { pos: [27, 24, pnZ + 22], tgt: [0, 0.4, pnZ] },
      // 南分岔区：从东南上空俯看南支路与 B/D 端口
      plazaS: { pos: [27, 24, psZ + 22], tgt: [0, 0.4, psZ] },
      // LPR 闸口相机视角（镜头原点位；manifest.props.lpr_cameras 可覆盖）
      lprIn: this._lprPreset('in'),
      lprOut: this._lprPreset('out'),
      follow: null,
      free: null,
    };
  }

  /** 取 manifest 提供的 LPR 相机视角；缺失时用 idea2 标定兜底（入口/出口）。 */
  _lprPreset(gate) {
    const cam = (this.lprCameras || []).find((c) => c && c.gate === gate) || {};
    const fallback = LPR_FALLBACK[gate];
    const view = cam.view_three || fallback.view_three;
    const look = cam.look_at_three || fallback.look_at_three;
    const fov = Number(cam.fov_vertical_deg ?? fallback.fov_vertical_deg) || 30;
    return { pos: [view.x, view.y, view.z], tgt: [look.x, look.y, look.z], fov, lpr: true };
  }

  /** manifest 热更新后刷新 LPR 相机位。 */
  setLprCameras(list) {
    this.lprCameras = Array.isArray(list) ? list.slice() : [];
  }

  /** 预设机位列表（供 UI 动态补齐选项）。 */
  static presetList() { return CAMERA_PRESETS.slice(); }

  setPreset(name, followTarget = null) {
    this.mode = name;
    this._followHold = false;
    if (name === 'follow') {
      this.followTarget = followTarget;
      this._setFov(this.baseFov);
      if (followTarget) this._snapToFollow(followTarget);
      return;
    }
    if (name === 'free') return;
    const p = this.presets()[name];
    if (!p) return;
    this._desiredPos.set(...p.pos);
    this._desiredTgt.set(...p.tgt);
    this.camera.position.copy(this._desiredPos);
    this.controls.target.copy(this._desiredTgt);
    this._setFov(p.fov || this.baseFov);
    this.controls.update();
  }

  /** 视角 FOV：LPR 相机按 manifest 30°，其余恢复 46°。 */
  _setFov(fov) {
    const next = Number(fov) || this.baseFov;
    if (Math.abs(this.camera.fov - next) < 1e-6) return;
    this.camera.fov = next;
    this.camera.updateProjectionMatrix();
  }

  setFollowTarget(vehicle) {
    this._followHold = false;
    this.followTarget = vehicle;
    if (vehicle) { this.mode = 'follow'; this._snapToFollow(vehicle); }
  }

  /** 目标离场：停止追踪已删除对象，相机保持最后位置。 */
  holdFollowPosition() {
    this.followTarget = null;
    this._followHold = true;
    this.mode = 'follow';
  }

  get currentPreset() { return this.mode; }

  _snapToFollow(vehicle) {
    try {
      const p = vehicle.worldPos ? vehicle.worldPos() : vehicle.pos;
      const h = vehicle.heading ? vehicle.heading() : 0;
      this.controls.target.set(p.x, (p.y || 0) + 0.8, p.z);
      this.camera.position.set(
        p.x + Math.sin(h) * 11, (p.y || 0) + 6.5, p.z + Math.cos(h) * 11);
      this.controls.update();
    } catch (_) { /* 车辆模型未就绪时忽略 */ }
  }

  update(dt) {
    if (this.mode === 'follow' && this.followTarget && !this.followTarget.model) {
      // 目标对象已 dispose/删除：立即停止追踪并保持最后机位，不访问已删除对象。
      this.followTarget = null;
      this._followHold = true;
    }
    if (this.mode === 'follow' && !this.followTarget && this._followHold) {
      // 目标已离场：保持最后机位，不追已删除对象、不自动切下一辆。
      this.controls.update();
      return;
    }
    if (this.mode === 'follow' && this.followTarget) {
      let p = null, h = 0;
      try {
        p = this.followTarget.worldPos ? this.followTarget.worldPos() : this.followTarget.pos;
        h = this.followTarget.heading ? this.followTarget.heading() : 0;
      } catch (_) { p = null; }
      if (p) {
        this._desiredTgt.set(p.x, (p.y || 0) + 0.8, p.z);
        this._desiredPos.set(p.x + Math.sin(h) * 11, (p.y || 0) + 6.5, p.z + Math.cos(h) * 11);
        this.camera.position.lerp(this._desiredPos, Math.min(1, dt * 3));
        this.controls.target.lerp(this._desiredTgt, Math.min(1, dt * 5));
      }
    }
    this.controls.update();
  }

  resize(w, h) {
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }
}

export { deg2rad };
