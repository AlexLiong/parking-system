// 碰撞代理（entities/Collider.js）：视觉模型与碰撞体分离（§8.1）。
// Route-v2 扩展：
//   - 保留旧接口（dimsOf / worldBox / intersects / gapAlong），供既有调用方兼容；
//   - 新增 OBB→AABB 位姿盒、静态碰撞体归一化/相交、冲突区矩形（three 坐标）距离/相交工具，
//     供 TrafficScheduler 的“静态壳碰撞安全网”和“预约区/闸区判定”使用。
// 坐标约定：three 世界坐标，x 东、z 南（three_z = -blender_y）；矩形统一 {xmin,zmin,xmax,zmax}。
import * as THREE from 'three';

const _box = new THREE.Box3();
const _min = new THREE.Vector3();
const _max = new THREE.Vector3();

const DIMS = {
  suv: { len: 4.8, wid: 1.9, hgt: 1.7 },
  van: { len: 5.2, wid: 2.0, hgt: 2.0 },
  hatch: { len: 4.2, wid: 1.8, hgt: 1.5 },
  sedan: { len: 4.6, wid: 1.8, hgt: 1.5 },
};

// 视觉尺寸保守兜底（P2-17）：GLB 实际包围盒含后视镜/饰件，通常大于碰撞 OBB。
// 车辆载入后会用 Box3 实测覆盖；未载入/单测 stub 模型时用本表保守值做排队/跟车间距。
// 注意：这些数值只用于“视觉安全间隙”，不参与 OBB 碰撞判定（dimsOf 仍为碰撞语义）。
const VISUAL_DIMS = {
  sedan: { len: 4.82, wid: 2.06, hgt: 1.58 },
  hatch: { len: 4.42, wid: 2.02, hgt: 1.62 },
  suv: { len: 5.02, wid: 2.16, hgt: 1.86 },
  van: { len: 5.42, wid: 2.22, hgt: 2.10 },
};

export class Collider {
  /** 依据车型族返回标准碰撞尺寸（米）。 */
  static dimsOf(family) {
    return DIMS[family] || DIMS.sedan;
  }

  /** 依据车型族返回**视觉**尺寸兜底（米）：仅用于排队/跟车/停止线视觉安全间隙。 */
  static visualDimsOf(family) {
    const o = DIMS[family] || DIMS.sedan;
    const v = VISUAL_DIMS[family] || VISUAL_DIMS.sedan;
    return { len: Math.max(o.len, v.len), wid: Math.max(o.wid, v.wid),
      hgt: Math.max(o.hgt, v.hgt) };
  }

  /** 从模型计算世界 AABB（写入 target，避免每帧分配）。 */
  static worldBox(model, target) {
    return (target || new THREE.Box3()).setFromObject(model);
  }

  static intersects(a, b) { return a.intersectsBox(b); }

  /**
   * 后车相对前车的沿路间距（用于跟车约束）：取两盒中心在给定方向上的投影差，
   * 减去两车半长。返回负值表示已重叠（异常，安全网会处理）。
   */
  static gapAlong(frontBox, rearBox, dirX, dirZ) {
    const fc = frontBox.getCenter(new THREE.Vector3());
    const rc = rearBox.getCenter(new THREE.Vector3());
    const len = Math.hypot(dirX, dirZ) || 1;
    const ux = dirX / len, uz = dirZ / len;
    const d = (fc.x - rc.x) * ux + (fc.z - rc.z) * uz;
    const fs = frontBox.getSize(new THREE.Vector3());
    const rs = rearBox.getSize(new THREE.Vector3());
    const halfF = Math.abs(ux) * fs.x / 2 + Math.abs(uz) * fs.z / 2;
    const halfR = Math.abs(ux) * rs.x / 2 + Math.abs(uz) * rs.z / 2;
    return d - halfF - halfR;
  }

  static scratch() { return _box; }

  // ===================== Route-v2 新增 =====================

  /**
   * 由车辆位姿（车心 x/z + rotation.y + 长宽）生成世界轴对齐包围盒。
   * 车体 OBB 尺寸 len(沿车头 -Z 轴)×wid(沿局部 X)，旋转后取 AABB。
   * 该盒比 model.setFromObject 略小（不含后视镜外扩），用于逻辑预测/静态碰撞预检。
   */
  static boxFromPose({ x, z, theta = 0, len = 4.6, wid = 1.8, hgt = 1.5 }, target) {
    const c = Math.abs(Math.cos(theta));
    const s = Math.abs(Math.sin(theta));
    const hx = c * wid / 2 + s * len / 2;
    const hz = s * wid / 2 + c * len / 2;
    return (target || new THREE.Box3()).set(_min.set(x - hx, 0, z - hz),
                                           _max.set(x + hx, hgt, z + hz));
  }

  /** 把静态碰撞体归一化为 Box3：支持 Box3 / [xmin,zmin,xmax,zmax] / {xmin,zmin,xmax,zmax} / {minX,minZ,maxX,maxZ} / {box:Box3}。 */
  static toBox3(item) {
    if (!item) return null;
    if (item.isBox3) return item;
    if (item.box && item.box.isBox3) return item.box;
    if (Array.isArray(item) && item.length >= 4) {
      return new THREE.Box3(new THREE.Vector3(item[0], -1, item[1]),
                            new THREE.Vector3(item[2], 5, item[3]));
    }
    if (typeof item.minX === 'number' && typeof item.maxX === 'number') {
      const h = typeof item.height === 'number' ? item.height : 3;
      return new THREE.Box3(new THREE.Vector3(item.minX, -1, item.minZ),
                            new THREE.Vector3(item.maxX, h, item.maxZ));
    }
    if (typeof item.xmin === 'number' && typeof item.xmax === 'number') {
      return new THREE.Box3(new THREE.Vector3(item.xmin, -1, item.zmin ?? item.ymin),
                            new THREE.Vector3(item.xmax, 8, item.zmax ?? item.ymax));
    }
    return null;
  }

  /** 与任静态碰撞体相交（colliders 允许 null/空）。返回命中的碰撞体或 null。 */
  static overlapsAny(box, colliders) {
    if (!box || !colliders || !colliders.length) return null;
    for (const item of colliders) {
      const b = Collider.toBox3(item);
      if (b && box.intersectsBox(b)) return item;
    }
    return null;
  }

  /** 冲突区矩形归一化：rectThree 已是 {xmin,zmin,xmax,zmax}；rect(Blender) 为 [xmin,ymin,xmax,ymax]。 */
  static rectThree(rect) {
    if (!rect) return null;
    if (Array.isArray(rect) && rect.length >= 4) {
      // Blender 坐标 [xmin,ymin,xmax,ymax] → three {xmin,zmin,xmax,zmax}（z=-y）
      return { xmin: rect[0], zmin: -rect[3], xmax: rect[2], zmax: -rect[1] };
    }
    if (typeof rect.xmin === 'number') return rect;
    return null;
  }

  /** 盒子与矩形（XZ 平面）是否相交。 */
  static boxIntersectsRect(box, rect) {
    if (!box || !rect) return false;
    return box.max.x > rect.xmin && box.min.x < rect.xmax
        && box.max.z > rect.zmin && box.min.z < rect.zmax;
  }

  /** 点是否在矩形内。 */
  static pointInRect(px, pz, rect) {
    return !!rect && px >= rect.xmin && px <= rect.xmax && pz >= rect.zmin && pz <= rect.zmax;
  }

  /** 点到矩形的最短距离（在矩形内为 0）。 */
  static pointRectDistance(px, pz, rect) {
    if (!rect) return Infinity;
    const dx = Math.max(rect.xmin - px, 0, px - rect.xmax);
    const dz = Math.max(rect.zmin - pz, 0, pz - rect.zmax);
    return Math.hypot(dx, dz);
  }

  /** 盒子到矩形的最短距离（相交为 0）。 */
  static boxRectDistance(box, rect) {
    if (!box || !rect) return Infinity;
    const dx = Math.max(rect.xmin - box.max.x, 0, box.min.x - rect.xmax);
    const dz = Math.max(rect.zmin - box.max.z, 0, box.min.z - rect.zmax);
    return Math.hypot(dx, dz);
  }

  /** 矩形外扩（margin 为负时内缩）。 */
  static expandRect(rect, margin) {
    if (!rect) return rect;
    return {
      xmin: rect.xmin - margin, zmin: rect.zmin - margin,
      xmax: rect.xmax + margin, zmax: rect.zmax + margin,
    };
  }

  /** 由车心 + 朝向计算车头/车尾点（与 Vehicle.nosePos 同一约定：车头 = 局部 -Z）。 */
  static nosePoint(x, z, theta, len) {
    const half = len / 2;
    return { x: x - Math.sin(theta) * half, z: z - Math.cos(theta) * half };
  }

  static tailPoint(x, z, theta, len) {
    const half = len / 2;
    return { x: x + Math.sin(theta) * half, z: z + Math.cos(theta) * half };
  }

  // ---------------- OBB（车体矩形）相交：AABB 粗筛后的精判 ----------------

  /** 车体 OBB 四角（世界 XZ）：车头 -Z、车宽局部 X。 */
  static obbCorners({ x, z, theta = 0, len = 4.6, wid = 1.8 }) {
    const s = Math.sin(theta), c = Math.cos(theta);
    // 局部 +X（宽）世界方向 (c,-s)；局部 -Z（车头）世界方向 (-s,-c)
    const rx = { x: c * wid / 2, z: -s * wid / 2 };
    const rz = { x: -s * len / 2, z: -c * len / 2 };
    return [
      { x: x + rx.x + rz.x, z: z + rx.z + rz.z },
      { x: x - rx.x + rz.x, z: z - rx.z + rz.z },
      { x: x - rx.x - rz.x, z: z - rx.z - rz.z },
      { x: x + rx.x - rz.x, z: z + rx.z - rz.z },
    ];
  }

  /**
   * 两个有向矩形（OBB，2D SAT）是否重叠；margin>0 时视为扩张后重叠。
   * @returns {boolean}
   */
  static obbOverlap(a, b, margin = 0) {
    const A = Collider.obbCorners(a);
    const B = Collider.obbCorners(b);
    const axes = [];
    for (const poly of [A, B]) {
      for (let i = 0; i < 2; i++) {
        const p1 = poly[i], p2 = poly[i + 1];
        const dx = p2.x - p1.x, dz = p2.z - p1.z;
        const l = Math.hypot(dx, dz) || 1;
        axes.push({ x: -dz / l, z: dx / l });      // 边法线
      }
    }
    for (const ax of axes) {
      let amin = Infinity, amax = -Infinity, bmin = Infinity, bmax = -Infinity;
      for (const p of A) { const d = p.x * ax.x + p.z * ax.z; amin = Math.min(amin, d); amax = Math.max(amax, d); }
      for (const p of B) { const d = p.x * ax.x + p.z * ax.z; bmin = Math.min(bmin, d); bmax = Math.max(bmax, d); }
      if (amax + margin < bmin || bmax + margin < amin) return false;
    }
    return true;
  }

  /** 车体 OBB 之间的重叠（自动带 margin）。 */
  static poseOverlap(a, b, margin = 0) {
    return Collider.obbOverlap(a, b, margin);
  }
}
