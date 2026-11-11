// 车位标线构建器（scene/MarkingBuilder.js）—— route-v2 新增。
//
// 职责（契约要求）：只画“车位白线 + 车位编号 + C 区（新能源优先）淡绿底”。
// 道路铺装/路缘/停止线/箭头/绿岛等由 Blender 静态壳 lot_shell.glb 提供，避免与壳重复。
//
// 实现：把全部车位线/编号/C 区底色绘制到**一张 CanvasTexture 地面贴图**，再用一个
// 平面网格铺到车场地面上（单 draw call，80 车位不增加 draw call 负担；避免 z-fighting：
// y 略高于地面 + polygonOffset）。布局热更新时只重建这张贴图。
import * as THREE from 'three';
import { SCENE_DEFAULT, MARKING_DEFAULTS } from '../config.js';

const C_TINT = 'rgba(96, 200, 118, 0.20)';     // C 区新能源优先淡绿底
const LINE_COLOR = 'rgba(238, 236, 228, 0.92)'; // 车位白线（略暖白，贴近漆面）

export class MarkingBuilder {
  /**
   * @param {THREE.Scene} scene
   */
  constructor(scene) {
    this.scene = scene;
    this.group = new THREE.Group();
    this.group.name = 'Markings';
    scene.add(this.group);
    this.texture = null;
    this.mesh = null;
    this._ppm = 0;              // 贴图分辨率（px / m）
    this._bounds = null;
  }

  /** 依据车位表（Blender 坐标 z=y）与 manifest 重建地面标线贴图。 */
  build(spaces, manifest = null) {
    if (!spaces || !spaces.length) { this.clear(); return null; }
    const markings = { ...MARKING_DEFAULTS, ...(manifest ? manifest.markings() : {}) };
    const palette = manifest ? manifest.palette() : {};
    const lineColor = palette.line_white ? hexToRgba(palette.line_white, 0.92) : LINE_COLOR;

    // 1) 场地范围（含 3m 内边距；优先 manifest.lot，否则按车位外扩）
    const bounds = this._boundsOf(spaces, manifest);
    const widthM = Math.max(8, bounds.xMax - bounds.xMin);
    const depthM = Math.max(8, bounds.yMax - bounds.yMin);
    // 2) 画布分辨率：长边 ≤ 3072（显存可控），保证 0.15m 线宽 ≥ 3.5px
    const maxPx = 3072;
    const ppm = Math.min(maxPx / widthM, maxPx / depthM, 42);
    const cvW = Math.max(256, Math.round(widthM * ppm));
    const cvH = Math.max(256, Math.round(depthM * ppm));
    this._ppm = ppm;
    this._bounds = bounds;

    const cv = document.createElement('canvas');
    cv.width = cvW; cv.height = cvH;
    const g = cv.getContext('2d');
    g.clearRect(0, 0, cvW, cvH);

    // Blender (x, y) → 画布 (px, py)。画布上方 = 北（Blender +y），与 three_z=-y 一致。
    const px = (x) => (x - bounds.xMin) * ppm;
    const py = (y) => (bounds.yMax - y) * ppm;
    const linePx = Math.max(2, markings.line_width_m * ppm);

    // 3) C 区（新能源优先）淡绿底
    for (const sp of spaces) {
      if (sp.kind !== 'ne_prefer') continue;
      const w = markings.space_w, d = markings.space_d;
      g.fillStyle = C_TINT;
      g.fillRect(px(sp.x - w / 2), py(sp.z + d / 2), w * ppm, d * ppm);
    }

    // 4) 车位白线（两侧边线 + 背线，开口朝向支路）
    g.strokeStyle = lineColor;
    g.lineWidth = linePx;
    g.lineCap = 'butt';
    for (const sp of spaces) {
      drawSpaceOutlineV2(g, sp, markings, ppm, px, py, lineColor, linePx);
    }

    // 5) 车位编号（贴地文字；固定北向朝上，俯视/入口视角可读）
    const fontPx = Math.max(14, Math.round(0.62 * ppm));
    g.font = `bold ${fontPx}px "PingFang SC","Microsoft YaHei",Arial,sans-serif`;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillStyle = 'rgba(240, 240, 235, 0.95)';
    for (const sp of spaces) {
      // 编号放在车位中心略偏“背线”一侧，避免与线重叠
      const face = faceOfRotation(sp.rotation);          // -1: back 在北；+1: back 在南
      const labelY = sp.z - face * (markings.space_d / 2 - 0.9);
      g.fillText(String(sp.space_no || ''), px(sp.x), py(labelY));
    }

    // 6) 生成纹理 / 平面（替换旧网格）
    this.clear();
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 8;
    tex.needsUpdate = true;
    this.texture = tex;

    const geo = new THREE.PlaneGeometry(widthM, depthM);
    const mat = new THREE.MeshBasicMaterial({
      map: tex, transparent: true, depthWrite: false, toneMapped: false,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.name = 'SpaceMarkingOverlay';
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set((bounds.xMin + bounds.xMax) / 2, markings.line_y,
      -(bounds.yMin + bounds.yMax) / 2);
    mesh.renderOrder = 3;
    mesh.receiveShadow = false;
    this.mesh = mesh;
    this.group.add(mesh);
    return { widthM, depthM, canvas: [cvW, cvH], ppm, spaces: spaces.length };
  }

  _boundsOf(spaces, manifest) {
    let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
    for (const sp of spaces) {
      const w = 3, d = 6;
      xMin = Math.min(xMin, sp.x - w / 2); xMax = Math.max(xMax, sp.x + w / 2);
      yMin = Math.min(yMin, sp.z - d / 2); yMax = Math.max(yMax, sp.z + d / 2);
    }
    if (!Number.isFinite(xMin)) return { xMin: -36, xMax: 36, yMin: -30, yMax: 29.5 };
    const pad = 1.5;
    let b = { xMin: xMin - pad, xMax: xMax + pad, yMin: yMin - pad, yMax: yMax + pad };
    if (manifest) {
      const lot = manifest.lot();
      // 以 manifest 地块为准（能覆盖外圈），但不要超出太多（贴图分辨率有限）
      b = {
        xMin: Math.min(b.xMin, Number(lot.x_min ?? b.xMin)),
        xMax: Math.max(b.xMax, Number(lot.x_max ?? b.xMax)),
        yMin: Math.min(b.yMin, Number(lot.y_min ?? b.yMin)),
        yMax: Math.max(b.yMax, Number(lot.y_max ?? b.yMax)),
      };
    }
    return b;
  }

  clear() {
    if (this.mesh) {
      this.group.remove(this.mesh);
      this.mesh.geometry?.dispose();
      this.mesh.material?.dispose();
      this.mesh = null;
    }
    if (this.texture) { this.texture.dispose(); this.texture = null; }
  }

  dispose() { this.clear(); }
}

/**
 * rotation=0 → 车头/开口朝南（Blender -Y）；rotation=180 → 朝北。
 * 返回 +1 表示“背线在南”（近排/北侧排），-1 表示“背线在北”。
 * 与 GroundBuilder._faceOf / LayoutGenerator 的 HEAD_FAR/HEAD_NEAR 约定一致。
 */
export function faceOfRotation(rotation) {
  const r = ((Number(rotation) % 360) + 360) % 360;
  return r > 90 && r < 270 ? -1 : +1;
}

function drawSpaceOutlineV2(g, sp, markings, ppm, px, py, color, linePx) {
  const w = markings.space_w, d = markings.space_d;
  const face = faceOfRotation(sp.rotation);          // -1: back 在北；+1: back 在南
  const x0 = sp.x - w / 2, x1 = sp.x + w / 2;
  const yCenter = sp.z;
  const backY = yCenter - face * (d / 2);            // 背线所在 Blender y
  const openY = yCenter + face * (d / 2);            // 开口侧

  // 两条侧边线（画在 x0/x1 中心线上）
  line(g, px(x0), py(openY), px(x0), py(backY), linePx);
  line(g, px(x1), py(openY), px(x1), py(backY), linePx);
  // 背线
  line(g, px(x0), py(backY), px(x1), py(backY), linePx);
}

function line(g, x0, y0, x1, y1, lw) {
  g.lineWidth = lw;
  g.beginPath();
  g.moveTo(x0, y0);
  g.lineTo(x1, y1);
  g.stroke();
}

function hexToRgba(hex, alpha = 1) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
  if (!m) return `rgba(233,230,220,${alpha})`;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

export { SCENE_DEFAULT };
