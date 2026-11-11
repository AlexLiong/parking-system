// 车辆实体（entities/Vehicle.js）：按"路段(leg)序列"做运动学跟随。
//
// Route-v2 改造要点：
//   1) 支持 planner 的三种 leg：line / arc / bezier（三维坐标均为 three {x,z}，z=-blender_y）；
//   2) **连续朝向**：朝向由路径切线直接给出（无“原地旋转”），仅用有限角速度做平滑；
//      转弯限速由曲率反解（v ≤ ω_max·R，且 v ≤ sqrt(a_lat_max·R)），不再先停车再转向；
//   3) 倒车入库：leg.reverse=true 时车头 = 切线反方向（nose 与运动方向相反），
//      速度符号/车轮反转/车牌朝向随之处理；
//   4) 保留旧接口与节点契约：Wheel_FL/FR/RL/RR 滚动、Plate_F/Plate_R 贴图、dimsOf 碰撞尺寸、
//      AABB（model.setFromObject）每帧刷新；旧折线 leg（ax/az/bx/bz）仍被接受（兼容分阶段集成）。
//
// 车辆只负责"怎么走"，"何时走/走哪条路"由 TrafficScheduler 决定（业务真理在后端）。
import * as THREE from 'three';
import { KIN } from '../config.js';
import { Collider } from './Collider.js';

// 车辆动力学缺省（config.KIN 覆盖同名字段；route-v2 新增 turnRate/maxLatAcc 可缺省）
const DEFAULTS = {
  straightSpeed: 2.5, turnSpeed: 1.2, accelTime: 1.2, brakeTime: 0.8,
  wheelRadius: 0.33, turnRate: 1.9, maxLatAcc: 1.15,
};
const ALIGN_GO = 0.30;      // rad：低于此按目标速度行驶
const ALIGN_STOP = 0.62;    // rad：高于此先减速/停车对齐（只应发生在路径切向接缝处）
const BEZIER_SAMPLES = 24;  // 三次贝塞尔折线化采样数（8~12 点亦可，24 点切线更平滑）

function kinOf(name) {
  const v = KIN && KIN[name];
  return (typeof v === 'number' && isFinite(v)) ? v : DEFAULTS[name];
}

const headingTo = (dx, dz) => Math.atan2(-dx, -dz);

function normAngle(a) {
  let x = a % (Math.PI * 2);
  if (x > Math.PI) x -= Math.PI * 2;
  if (x < -Math.PI) x += Math.PI * 2;
  return x;
}

function angleDiff(a, b) { return normAngle(b - a); }

function hyp(dx, dz) { return Math.hypot(dx, dz); }

// ----------------------------- leg 原语 -----------------------------

/** 直线段：a→b，切线恒定。 */
class LinePrim {
  constructor(leg) {
    this.kind = 'line';
    this.length = hyp(leg.bx - leg.ax, leg.bz - leg.az);
    this.dx = leg.bx - leg.ax;
    this.dz = leg.bz - leg.az;
    this.reverse = !!leg.reverse;
    const n = this.length || 1;
    this.tx = this.dx / n;
    this.tz = this.dz / n;
    this.speedLimit = Number(leg.speed) > 0 ? Number(leg.speed) : kinOf('straightSpeed');
    this.ax = leg.ax; this.az = leg.az;
  }
  pointAt(s) {
    const u = this.length > 1e-9 ? Math.max(0, Math.min(1, s / this.length)) : 1;
    return { x: this.ax + this.dx * u, z: this.az + this.dz * u };
  }
  tangentAt() { return { x: this.tx, z: this.tz }; }
}

/** 圆弧段：{cx,cz,r,a0,a1,reverse}，角度按 (cx+r·cos a, cz+r·sin a) 参数化，a0→a1 为行进方向。 */
class ArcPrim {
  constructor(leg) {
    this.kind = 'arc';
    this.cx = leg.cx; this.cz = leg.cz;
    this.r = Math.max(0.2, Math.abs(Number(leg.r) || 0));
    this.a0 = Number(leg.a0) || 0;
    this.a1 = Number(leg.a1) || 0;
    this.dir = Math.sign(this.a1 - this.a0) || 1;
    this.length = this.r * Math.abs(this.a1 - this.a0);
    this.reverse = !!leg.reverse;
    const maxLat = kinOf('maxLatAcc');
    const turnRate = kinOf('turnRate');
    // 转弯限速：向心加速度 + 角速度上限（朝向按切线跟随，需要 ω=v/R ≤ turnRate）
    const vLat = Math.sqrt(maxLat * this.r);
    const vRate = turnRate * this.r * 0.85;
    const cfgTurn = kinOf('turnSpeed');
    this.speedLimit = Math.max(0.4, Math.min(this.length > 0 ? cfgTurn : 0.4, vLat, vRate));
    if (Number(leg.speed) > 0) this.speedLimit = Math.min(this.speedLimit, Number(leg.speed));
  }
  _angle(s) { return this.a0 + this.dir * (this.length > 1e-9 ? s / this.r : 0); }
  pointAt(s) {
    const a = this._angle(Math.max(0, Math.min(this.length, s)));
    return { x: this.cx + this.r * Math.cos(a), z: this.cz + this.r * Math.sin(a) };
  }
  tangentAt(s) {
    const a = this._angle(Math.max(0, Math.min(this.length, s)));
    return { x: -Math.sin(a) * this.dir, z: Math.cos(a) * this.dir };
  }
}

/** 三次贝塞尔段：{p0,p1,p2,p3,reverse}；折线化后按弧长参数化，切线取采样折线方向。 */
class BezierPrim {
  constructor(leg) {
    this.kind = 'bezier';
    const p = (q) => (q && typeof q.x === 'number' ? { x: q.x, z: q.z } : { x: 0, z: 0 });
    this.p0 = p(leg.p0); this.p1 = p(leg.p1); this.p2 = p(leg.p2); this.p3 = p(leg.p3);
    this.reverse = !!leg.reverse;
    const n = Math.max(6, Math.min(64, Number(leg.samples) || BEZIER_SAMPLES));
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n;
      const mt = 1 - t;
      pts.push({
        x: mt * mt * mt * this.p0.x + 3 * mt * mt * t * this.p1.x
          + 3 * mt * t * t * this.p2.x + t * t * t * this.p3.x,
        z: mt * mt * mt * this.p0.z + 3 * mt * mt * t * this.p1.z
          + 3 * mt * t * t * this.p2.z + t * t * t * this.p3.z,
      });
    }
    // 累积弧长
    const cum = [0];
    for (let i = 1; i < pts.length; i++) {
      cum.push(cum[i - 1] + hyp(pts[i].x - pts[i - 1].x, pts[i].z - pts[i - 1].z));
    }
    this.pts = pts; this.cum = cum;
    this.length = cum[cum.length - 1];
    // 曲率估计（三点圆）→ 限速
    let kMax = 0;
    for (let i = 1; i < pts.length - 1; i++) {
      const a = pts[i - 1], b = pts[i], c = pts[i + 1];
      const ab = hyp(b.x - a.x, b.z - a.z), bc = hyp(c.x - b.x, c.z - b.z);
      const ca = hyp(a.x - c.x, a.z - c.z);
      const area2 = Math.abs((b.x - a.x) * (c.z - a.z) - (b.z - a.z) * (c.x - a.x));
      if (ab * bc * ca > 1e-9) kMax = Math.max(kMax, 2 * area2 / (ab * bc * ca));
    }
    const rMin = kMax > 1e-6 ? 1 / kMax : 1e6;
    const vLat = Math.sqrt(kinOf('maxLatAcc') * Math.max(0.4, rMin));
    const vRate = kinOf('turnRate') * Math.max(0.4, rMin) * 0.85;
    this.speedLimit = Math.max(0.4, Math.min(kinOf('turnSpeed'), vLat, vRate));
    if (Number(leg.speed) > 0) this.speedLimit = Math.min(this.speedLimit, Number(leg.speed));
  }
  _segment(s) {
    const L = this.length;
    const u = L > 1e-9 ? Math.max(0, Math.min(L, s)) : L;
    // 二分定位折线段
    let lo = 0, hi = this.cum.length - 1;
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (this.cum[mid] <= u) lo = mid; else hi = mid;
    }
    const segLen = this.cum[hi] - this.cum[lo] || 1e-9;
    const t = Math.max(0, Math.min(1, (u - this.cum[lo]) / segLen));
    return { i: lo, t, segLen };
  }
  pointAt(s) {
    const { i, t } = this._segment(s);
    const a = this.pts[i], b = this.pts[Math.min(i + 1, this.pts.length - 1)];
    return { x: a.x + (b.x - a.x) * t, z: a.z + (b.z - a.z) * t };
  }
  tangentAt(s) {
    const { i } = this._segment(s);
    const a = this.pts[i], b = this.pts[Math.min(i + 1, this.pts.length - 1)];
    const dx = b.x - a.x, dz = b.z - a.z;
    const n = hyp(dx, dz) || 1;
    return { x: dx / n, z: dz / n };
  }
}

function buildPrim(leg) {
  if (!leg || typeof leg !== 'object') return null;
  if (leg.kind === 'arc') return new ArcPrim(leg);
  if (leg.kind === 'bezier') return new BezierPrim(leg);
  // line：新契约 {kind:'line',ax,az,bx,bz} 与旧折线 {ax,az,bx,bz} 均接受
  if (typeof leg.ax === 'number' && typeof leg.bx === 'number') return new LinePrim(leg);
  return null;
}

// ----------------------------- Vehicle -----------------------------

export class Vehicle {
  /**
   * @param {string} id 车辆唯一 id（与后端 vehicle_id 对应）
   * @param {import('../scene/AssetFactory.js').AssetFactory} assets
   * @param {{file:string,family:string,len:number,wid:number}} spec
   */
  constructor(id, assets, spec) {
    this.id = id;
    this.vid = id;                 // route-v2：稳定演示 id（剧本 vid），dump() 使用
    this.assets = assets;
    this.spec = spec;
    this.model = null;
    this.wheels = [];
    this.plateMeshes = [];
    this.box = new THREE.Box3();
    this.dims = Collider.dimsOf(spec.family);
    // P2-17：视觉尺寸（含后视镜/饰件）与碰撞 OBB 分离；载入后由 GLB 包围盒实测覆盖。
    const vd = Collider.visualDimsOf(spec.family);
    this.visualLen = vd.len;
    this.visualWid = vd.wid;
    this.visualHgt = vd.hgt;
    this.visualBox = null;
    // R-04：仅视觉偏移（边界车位停放时朝场内微移）；不改 pos/dims/poseBox/路线。
    this.visualOffset = { x: 0, z: 0 };

    // 路径
    this.legs = [];
    this.prims = [];
    this.legIndex = 0;             // 当前 leg 序号（prims 与 legs 一一对应，跳过零长段）
    this.legStart = 0;             // 当前 prim 的全局弧长起点
    this.routeLength = 0;
    this.dist = 0;                 // 全局弧长进度（沿路径）
    this.pos = { x: 0, z: 0 };
    this.theta = 0;
    this.speed = 0;                // 沿路径速度（m/s，恒 >=0；方向由 reverse 解释）
    this.cruise = kinOf('straightSpeed');
    this.hardStop = false;
    this.done = false;
    this.reverseLeg = false;

    this.state = 'idle';           // 语义状态，由调度器维护
    this.meta = {};                // {plate,color,kind,imagePath,spaceNo,recordId,vid...}
    this.reservedZones = new Set(); // 由调度器维护（debug dump 使用）
    this._spin = 0;
    this._steer = 0;
    this._targetBox = new THREE.Box3();
  }

  async load(scene) {
    const model = await this.assets.cloneModel(this.spec.file);
    model.name = `Veh_${this.id}`;
    this.model = model;
    for (const w of ['Wheel_FL', 'Wheel_FR', 'Wheel_RL', 'Wheel_RR']) {
      const node = model.getObjectByName(w);
      if (node) {
        node.rotation.order = 'YXZ'; // yaw about the vertical steering axis, then axle roll
        this.wheels.push(node);
      }
    }
    for (const p of ['Plate_F', 'Plate_R']) {
      const node = model.getObjectByName(p);
      if (node) {
        // Source box meshes use atlas UVs. Keep the original mesh and add a
        // complete UV face in its own coordinates, at the same physical size.
        node.geometry.computeBoundingBox();
        const box = node.geometry.boundingBox, size = box.getSize(new THREE.Vector3());
        const face = new THREE.Mesh(new THREE.PlaneGeometry(size.x, size.y));
        face.name = `PlateCanvas_${p.endsWith('F') ? 'F' : 'R'}`;
        // 3D 前/后牌面在原模型坐标系中纹理纵向为倒置（实测相机帧 OCR 可复现）；
        // 翻转 UV 的 V 轴（不改资产/几何大小/位置），让车牌文字在闸口相机中正向可读。
        const uv = face.geometry.attributes.uv;
        for (let i = 0; i < uv.count; i++) uv.setY(i, 1 - uv.getY(i));
        uv.needsUpdate = true;
        box.getCenter(face.position);
        face.position.z = p.endsWith('F') ? box.min.z - .001 : box.max.z + .001;
        if (p.endsWith('F')) face.rotation.y = Math.PI;
        node.add(face);
        this.plateMeshes.push(face);
      }
    }
    // P2-17：用 GLB 实际包围盒计算视觉尺寸（单位米）。只用于排队/跟车/停止线安全间距，
    // this.dims 仍保持 Collider.dimsOf(family) 的 OBB 碰撞语义，不改变既有碰撞/路线断言。
    try {
      model.updateMatrixWorld(true);
      const vbox = new THREE.Box3().setFromObject(model);
      const vsize = vbox.getSize(new THREE.Vector3());
      const fallback = Collider.visualDimsOf(this.spec.family);
      this.visualBox = vbox;
      this.visualLen = Math.max(fallback.len, vsize.z || 0);
      this.visualWid = Math.max(fallback.wid, vsize.x || 0);
      this.visualHgt = Math.max(fallback.hgt, vsize.y || 0);
    } catch (e) {
      console.warn('[Vehicle] 视觉包围盒计算失败，使用 per-family 兜底', e?.message || e);
    }
    scene.add(model);
    this._applyTransform();
    return model;
  }

  setPlate(plate, color = 'blue') {
    this.meta.plate = plate;
    this.meta.color = color;
    if (!this.plateMeshes.length) return;
    // 后端 plate-image 渲染图优先：入口识别后的 setPlate 不得把它重置成 Canvas/MIT 兜底图，
    // 否则出口相机看到的是另一套方向的牌面，出口 expected_mismatch。
    if (this._plateImage) return;
    const key = `${color}:${plate}`;
    if (key === this._plateKey && this._plateMaterial) return;
    const oldMap = this._plateMaterial?.map;
    // AssetFactory 的 MIT 真实车牌贴图跨车辆共享（userData.shared）：不能在此销毁，
    // 否则其他车辆共用贴图会被一并释放；仅释放 Canvas 回退贴图（本车独有）。
    if (oldMap && !oldMap.userData?.shared) oldMap.dispose();
    this._plateMaterial?.dispose();
    const tex = this.assets.plateTexture(plate, color);
    this._plateMaterial = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.4, metalness: 0.1 });
    this._plateKey = key;
    for (const m of this.plateMeshes) m.material = this._plateMaterial;
  }

  /**
   * 任意车牌图贴图（手动派车 dispatch-plan / plate-image 返回的图）。
   * image 可为 ImageBitmap / HTMLImageElement / HTMLCanvasElement / THREE.Texture。
   * 仅替换车牌面材质，不改变车辆/路径/OBB。
   */
  setPlateImage(image, plate = null, color = 'blue') {
    if (plate != null) this.meta.plate = plate;
    this.meta.color = color;
    if (!this.plateMeshes.length || !image) return false;
    let tex;
    if (image && image.isTexture) tex = image;
    else {
      tex = new THREE.Texture(image);
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.anisotropy = 4;
      tex.needsUpdate = true;
    }
    tex.userData = { ...(tex.userData || {}), plateImage: true };
    const oldMap = this._plateMaterial?.map;
    if (oldMap && !oldMap.userData?.shared && oldMap !== tex) oldMap.dispose();
    this._plateMaterial?.dispose();
    this._plateMaterial = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.4, metalness: 0.1 });
    this._plateKey = `image:${plate || ''}:${color}`;
    this._plateImage = tex;
    for (const m of this.plateMeshes) m.material = this._plateMaterial;
    return true;
  }

  // ----------------------------- 路径 -----------------------------

  /**
   * 设置行驶路径。legs: 支持
   *   {kind:'line', ax,az,bx,bz, reverse?}
   *   {kind:'arc', cx,cz,r,a0,a1, reverse?}
   *   {kind:'bezier', p0:{x,z},p1,p2,p3, reverse?}
   *   {ax,az,bx,bz}（旧折线，等价 line）
   * 坐标均为 three 世界坐标。
   * @param {{snapFirst?:boolean, keepProgress?:boolean}} opts
   */
  setLegs(legs, { snapFirst = true } = {}) {
    const raw = Array.isArray(legs) ? legs : [];
    this.prims = [];
    this.legs = [];
    for (const L of raw) {
      const p = buildPrim(L);
      if (p && p.length > 1e-6) { this.prims.push(p); this.legs.push(L); }
    }
    this.legIndex = 0;
    this.legStart = 0;
    this.dist = 0;
    this.done = this.prims.length === 0;
    this.speed = 0;
    this.reverseLeg = false;
    this.routeLength = this.prims.reduce((s, p) => s + p.length, 0);
    if (this.prims.length) {
      const p0 = this.prims[0];
      const s0 = p0.pointAt(0);
      this.pos = { x: s0.x, z: s0.z };
      this.reverseLeg = p0.reverse;
      const t0 = p0.tangentAt(0);
      const desired = normAngle(headingTo(t0.x, t0.z) + (p0.reverse ? Math.PI : 0));
      if (snapFirst) this.theta = desired;
    }
    this._applyTransform();
  }

  /** 直接放置（用于外场等待位/调试）；清空路径。 */
  placeAt(x, z, theta = 0) {
    this.prims = []; this.legs = []; this.dist = 0; this.routeLength = 0;
    this.legIndex = 0; this.done = false; this.speed = 0; this.hardStop = false;
    this.pos = { x, z };
    this.theta = normAngle(theta);
    this._applyTransform();
  }

  stop() { this.hardStop = true; }
  resume() { this.hardStop = false; }

  worldPos() { return { x: this.pos.x, y: 0, z: this.pos.z }; }
  heading() { return this.theta; }
  get length() { return this.dims.len; }

  /** 当前段的行进方向单位向量（世界，路径切线而非车头方向）。 */
  dirVec() {
    const p = this.prims[this.legIndex] || this.prims[this.prims.length - 1];
    if (!p) return { x: 0, z: 0 };
    const s = this.prims[this.legIndex] ? (this.dist - this.legStart) : p.length;
    return p.tangentAt(Math.max(0, Math.min(p.length, s)));
  }

  /** 车头世界坐标（用于杆区/停止线判定）：车头本地 -Z。 */
  nosePos() {
    const half = this.dims.len / 2;
    return { x: this.pos.x - Math.sin(this.theta) * half,
             z: this.pos.z - Math.cos(this.theta) * half };
  }

  /** 车尾世界坐标。 */
  tailPos() {
    const half = this.dims.len / 2;
    return { x: this.pos.x + Math.sin(this.theta) * half,
             z: this.pos.z + Math.cos(this.theta) * half };
  }

  /** 车身在给定位姿下的预测 AABB（不写回 box）。 */
  poseBox(theta = this.theta, x = this.pos.x, z = this.pos.z) {
    return Collider.boxFromPose(
      { x, z, theta, len: this.dims.len, wid: this.dims.wid, hgt: this.dims.hgt },
      this._targetBox);
  }

  /** 当前 leg 类型（line/arc/bezier），dump/测试用；done 时返回最后一段类型。 */
  legKind() {
    const p = this.prims[this.legIndex] || this.prims[this.prims.length - 1];
    return p ? p.kind : null;
  }

  /** 当前 leg 的进度 0..1 与剩余长度。 */
  legProgress() {
    const p = this.prims[this.legIndex];
    if (!p || p.length <= 1e-9) return 0;
    return Math.max(0, Math.min(1, (this.dist - this.legStart) / p.length));
  }

  remaining() {
    return Math.max(0, this.routeLength - this.dist);
  }

  /** 沿当前路径到某世界点的近似剩余距离（用于预约 ETA）；仅用欧氏距离估计。 */
  distanceTo(x, z) {
    return hyp(x - this.pos.x, z - this.pos.z);
  }

  // ----------------------------- 帧更新 -----------------------------

  update(dt) {
    if (!(dt > 0)) return;
    if (this.done || !this.prims.length) {
      this.speed = 0;
      this._applyTransform();
      return;
    }
    const p = this.prims[this.legIndex];
    if (!p) { this.done = true; this.speed = 0; this._applyTransform(); return; }

    const localS = this.dist - this.legStart;
    const tan = p.tangentAt(localS);
    const desired = normAngle(headingTo(tan.x, tan.z) + (p.reverse ? Math.PI : 0));
    const diff = angleDiff(this.theta, desired);
    const maxStep = kinOf('turnRate') * dt;
    this.theta = normAngle(this.theta + Math.max(-maxStep, Math.min(maxStep, diff)));
    const alignErr = Math.abs(angleDiff(this.theta, desired));

    // 目标速度：硬停/未对齐/曲率限速
    let target = this.hardStop ? 0 : this.cruise;
    if (alignErr > ALIGN_STOP) target = 0;
    else if (alignErr > ALIGN_GO) target = Math.min(target, kinOf('turnSpeed') * 0.6);
    target = Math.max(0, Math.min(target, p.speedLimit));

    const accel = kinOf('straightSpeed') / kinOf('accelTime');
    const brake = kinOf('straightSpeed') / kinOf('brakeTime');
    if (target > this.speed) this.speed = Math.min(target, this.speed + accel * dt);
    else this.speed = Math.max(target, this.speed - brake * dt);

    // 路径推进（沿路径恒为正向；reverse 只改变车头/轮向）
    const beforeDist = this.dist;
    this.dist += this.speed * dt;
    let guard = 0;
    while (this.legIndex < this.prims.length && this.dist >= this.legStart + this.prims[this.legIndex].length) {
      this.legStart += this.prims[this.legIndex].length;
      this.legIndex += 1;
      if (++guard > 512) break;
    }
    if (this.legIndex >= this.prims.length) {
      const last = this.prims[this.prims.length - 1];
      const end = last.pointAt(last.length);
      this.pos = { x: end.x, z: end.z };
      const endTan = last.tangentAt(last.length);
      this.theta = normAngle(headingTo(endTan.x, endTan.z) + (last.reverse ? Math.PI : 0));
      this.reverseLeg = last.reverse;
      this.done = true;
      this.speed = 0;
      this._spinWheels(this.routeLength - beforeDist, dt, last, last.length);
      this._applyTransform();
      return;
    }
    const cur = this.prims[this.legIndex];
    this.reverseLeg = cur.reverse;
    const pt = cur.pointAt(this.dist - this.legStart);
    this.pos.x = pt.x;
    this.pos.z = pt.z;

    // 车轮滚动：前进正转、倒车反转
    this._spinWheels(this.dist - beforeDist, dt, cur, this.dist - this.legStart);
    this._applyTransform();
  }

  _spinWheels(distance, dt, prim, s) {
    if (!this.wheels.length) return;
    // Nose is local -Z; forward rolling therefore rotates the X axle negatively.
    const signed = (this.reverseLeg ? 1 : -1) * distance / kinOf('wheelRadius');
    this._spin += signed;
    const a = Math.max(0, s - .15), b = Math.min(prim.length, s + .15);
    const t0 = prim.tangentAt(a), t1 = prim.tangentAt(b);
    const curvature = b > a ? angleDiff(headingTo(t0.x,t0.z), headingTo(t1.x,t1.z)) / (b-a) : 0;
    const front = this.wheels.find(w=>w.name==='Wheel_FL');
    const rear = this.wheels.find(w=>w.name==='Wheel_RL');
    const wheelbase = front && rear ? Math.abs(front.position.z-rear.position.z) : this.dims.len*.6;
    const target = Math.max(-.52, Math.min(.52, Math.atan(wheelbase * curvature * (this.reverseLeg ? -1 : 1))));
    this._steer += Math.max(-2*dt, Math.min(2*dt, target-this._steer));
    for (const w of this.wheels) {
      w.rotation.x = this._spin;
      w.rotation.y = w.name==='Wheel_FL' || w.name==='Wheel_FR' ? this._steer : 0;
    }
  }

  /**
   * R-04：只移动**视觉模型**（model.position）朝场内微移，不修改 pos/dims/路线/碰撞。
   * 用于贴墙边界车位停放后避免外饰/车角视觉穿墙。
   */
  setVisualOffset(x = 0, z = 0) {
    const nx = Number(x) || 0, nz = Number(z) || 0;
    if (this.visualOffset.x === nx && this.visualOffset.z === nz) return this.visualOffset;
    this.visualOffset = { x: nx, z: nz };
    if (this.model) this._applyTransform();
    return this.visualOffset;
  }

  _applyTransform() {
    if (!this.model) return;
    this.model.position.set(this.pos.x + this.visualOffset.x, 0,
      this.pos.z + this.visualOffset.z);
    this.model.rotation.y = this.theta;
    this.model.updateMatrixWorld(true);
    Collider.worldBox(this.model, this.box);
  }

  dispose(scene) {
    if (this.model) scene.remove(this.model);
    this.model = null;
    for (const mesh of this.plateMeshes) if (mesh.name.startsWith('PlateCanvas_')) mesh.geometry.dispose();
    const map = this._plateMaterial?.map;
    if (map && !map.userData?.shared) map.dispose();
    this._plateMaterial?.dispose();
    this._plateMaterial = null; this._plateKey = null; this.plateMeshes = [];
  }
}
