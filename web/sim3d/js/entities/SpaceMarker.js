// 车位高亮标记（entities/SpaceMarker.js）—— route-v2 合批版（InstancedMesh）。
//
// 原实现为“每车位一个 Mesh”（80 个 draw call），改为 **1 个 InstancedMesh**：
//   - 每个 instance 的显隐用“缩放 0/1”表达（free 不可见，与原 opacity=0 等价）；
//   - 占用/禁用/推荐颜色用 `setColorAt` 逐实例上色（材质统一 opacity=0.42）；
//   - 推荐车位“呼吸”由实例缩放脉冲实现（原为 opacity 呼吸；视觉等价，无需自定义 shader）；
//   - 布局热更新 build(spaces) 重建实例并释放旧几何/材质。
//
// 对外 API 保持不变：build / setOccupied / resync / setRecommend / update / markers(Map)。
import * as THREE from 'three';

const COLOR = {
  free: 0x2f6df6, occupied: 0xff5a4d, recommend: 0x37e39a, disabled: 0x555a66,
};
const MARKER_W = 2.75;
const MARKER_D = 5.75;
const MARKER_Y = 0.035;
const BASE_OPACITY = 0.42;

export class SpaceMarker {
  constructor(scene) {
    this.group = new THREE.Group();
    this.group.name = 'SpaceMarkers';
    scene.add(this.group);
    this.markers = new Map();   // space_no -> {index, state, space}
    this.recommended = null;
    this._clock = 0;
    this._mesh = null;
    this._tmpMatrix = new THREE.Matrix4();
    this._tmpColor = new THREE.Color();
  }

  /** 依据 SpaceSpec[] 重建实例（布局热更新时调用）。 */
  build(spaces) {
    this._clear();
    const list = spaces || [];
    if (!list.length) return this;
    const geo = new THREE.PlaneGeometry(MARKER_W, MARKER_D);
    geo.rotateX(-Math.PI / 2);                    // 平铺在地面（局部 +Y 朝上）
    const mat = new THREE.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: BASE_OPACITY,
      depthWrite: false, side: THREE.DoubleSide, toneMapped: false,
    });
    const mesh = new THREE.InstancedMesh(geo, mat, list.length);
    mesh.name = 'SpaceMarkersInstanced';
    mesh.renderOrder = 2;
    mesh.frustumCulled = false;                   // 覆盖整场，避免实例包围盒裁剪误判
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.group.add(mesh);
    this._mesh = mesh;

    list.forEach((sp, i) => {
      this.markers.set(sp.space_no, { index: i, state: sp.status || 'free', space: sp });
      this._applyByIndex(i);
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    return this;
  }

  /** 写某实例的矩阵（缩放/位置）+ 颜色；free 用 scale=0 隐藏（等价旧 opacity=0）。 */
  _applyByIndex(i) {
    const mesh = this._mesh;
    const rec = this._byIndex(i);
    if (!mesh || !rec) return;
    const sp = rec.space || { x: 0, z: 0 };
    const isRec = this.recommended && sp.space_no === this.recommended;
    let scale = 0;
    if (rec.state === 'occupied' || rec.state === 'disabled') scale = 1;
    else if (isRec) scale = 1 + 0.06 * Math.sin(this._clock * 3.2);   // 推荐位呼吸
    this._tmpMatrix.identity();
    if (scale !== 1) this._tmpMatrix.makeScale(scale, 1, scale);
    this._tmpMatrix.setPosition(sp.x ?? 0, MARKER_Y, -(sp.z ?? 0));
    mesh.setMatrixAt(i, this._tmpMatrix);
    mesh.instanceMatrix.needsUpdate = true;

    let hex = COLOR.free;
    if (rec.state === 'occupied') hex = COLOR.occupied;
    else if (rec.state === 'disabled') hex = COLOR.disabled;
    else if (isRec) hex = COLOR.recommend;
    this._tmpColor.setHex(hex);
    mesh.setColorAt(i, this._tmpColor);
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }

  _byIndex(index) {
    for (const rec of this.markers.values()) if (rec.index === index) return rec;
    return null;
  }

  _byNo(spaceNo) { return this.markers.get(spaceNo) || null; }

  setOccupied(spaceNo, occupied = true) {
    const rec = this._byNo(spaceNo);
    if (!rec) return this;
    rec.state = occupied ? 'occupied' : 'free';
    this._applyByIndex(rec.index);
    return this;
  }

  /** 用 /api/spaces 的权威数据整体重同步。 */
  resync(spaceList) {
    for (const sp of (spaceList || [])) {
      const rec = this._byNo(sp.space_no);
      if (!rec) continue;
      rec.state = sp.status || 'free';
      if (rec.space) rec.space.status = sp.status;
      this._applyByIndex(rec.index);
    }
    return this;
  }

  setRecommend(spaceNo) {
    const prevNo = this.recommended;
    this.recommended = spaceNo || null;
    const prev = prevNo ? this._byNo(prevNo) : null;
    if (prev) this._applyByIndex(prev.index);     // 旧推荐位恢复自身状态
    if (spaceNo) {
      const rec = this._byNo(spaceNo);
      if (rec) this._applyByIndex(rec.index);
    }
    return this;
  }

  update(dt) {
    this._clock += dt;
    if (!this.recommended) return;
    const rec = this._byNo(this.recommended);
    if (!rec || rec.state !== 'free') return;
    this._applyByIndex(rec.index);                // 呼吸缩放
  }

  /** 供调试/统计：当前实例数（80）与 draw call（1）。 */
  stats() {
    return { instances: this.markers.size, meshes: this._mesh ? 1 : 0 };
  }

  _clear() {
    if (this._mesh) {
      this.group.remove(this._mesh);
      this._mesh.geometry?.dispose();
      this._mesh.material?.dispose();
      this._mesh.dispose?.();
      this._mesh = null;
    }
    this.markers.clear();
    this.recommended = null;
  }
}
