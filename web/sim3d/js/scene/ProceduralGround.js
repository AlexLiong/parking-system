// 程序化 V2 地面（scene/ProceduralGround.js）—— 仅用于“静态壳 lot_shell.glb 尚未就绪”的回退。
//
// 依据 manifest 的 ground / road_surfaces / green_areas / markings_v2 绘制：
//   - 草地底色（grass_extent）、车场地坪（lot_asphalt）、场前区（apron）、外场道路（outer_road）；
//   - 9 块铺装（中央双车道 ×2 / 南北分岔区 / 四条 6.5m 支路 / 场前咽喉带）；
//   - 绿化分区（绿岛两翼 / 种植带 / 南侧绿带 / 中央隔离带，按 raise 抬高 + 路缘色）；
//   - 停止线（markings_v2.stop_lines，2 条）。
// 车位白线/编号/C 区底色仍由 MarkingBuilder 负责（避免重复）。shell 就绪后本模块不再创建。
import * as THREE from 'three';

const COLOR = {
  grass: 0x4a6b35, asphalt: 0x2b2d31, road: 0x222529, plaza: 0x26292e,
  curb: 0xb8b3a6, median_grass: 0x557a3e, line_white: 0xe9e6dc,
};

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({
    color, roughness: opts.rough ?? 0.95, metalness: 0,
    polygonOffset: true, polygonOffsetFactor: opts.offset ?? -1, polygonOffsetUnits: opts.offset ?? -1,
  });
}

export class ProceduralGround {
  constructor(scene, manifest) {
    this.scene = scene;
    this.manifest = manifest;
    this.group = new THREE.Group();
    this.group.name = 'ProceduralGroundV2';
    scene.add(this.group);
    this._mats = {
      grass: mat(COLOR.grass, { rough: 1 }),
      asphalt: mat(COLOR.asphalt),
      road: mat(COLOR.road),
      plaza: mat(COLOR.plaza),
      curb: mat(COLOR.curb, { rough: 0.8 }),
      median: mat(COLOR.median_grass, { rough: 1 }),
      line: mat(COLOR.line_white, { rough: 0.6, offset: -2 }),
    };
  }

  /** Blender 矩形 [xmin, ymin, xmax, ymax] → 水平面板（three z = -y）。 */
  _slab(rect, y, material) {
    const [x0, y0, x1, y1] = rect.map(Number);
    const w = Math.abs(x1 - x0), d = Math.abs(y1 - y0);
    if (w <= 0 || d <= 0) return null;
    const geo = new THREE.PlaneGeometry(w, d);
    const m = new THREE.Mesh(geo, material);
    m.rotation.x = -Math.PI / 2;
    m.position.set((x0 + x1) / 2, y, -(y0 + y1) / 2);
    m.receiveShadow = true;
    this.group.add(m);
    return m;
  }

  build() {
    const m = this.manifest;
    const ground = m.ground ? m.ground() : null;
    const roadSurfaces = m.roadSurfaces ? m.roadSurfaces() : [];
    const green = m.greenAreas ? m.greenAreas() : null;
    const markings = m.markingsV2 ? m.markingsV2() : null;
    const lot = m.lot ? m.lot() : null;
    const outer = m.outerRoad ? m.outerRoad() : null;
    const apron = m.apron ? m.apron() : null;
    const P = this._mats;

    // 1) 草地底色（外扩到 grass_extent；缺省以地块外扩 16m）
    const grassRect = ground?.grass_extent || (lot ? [
      Number(lot.x_min) - 16, Number(lot.y_min) - 25, Number(lot.x_max) + 16, Number(lot.y_max) + 4.5,
    ] : [-52, -55, 52, 34]);
    this._slab(grassRect, -0.06, P.grass);

    // 2) 车场地坪 / 场前区 / 外场道路
    const lotRect = ground?.lot_asphalt || (lot ? [lot.x_min, lot.y_min, lot.x_max, lot.y_max] : [-36, -30, 36, 29.5]);
    this._slab(lotRect, -0.03, P.asphalt);
    const apronRect = ground?.apron || (apron ? [
      -9, Number(apron.y_min), 9, Number(apron.y_max)] : [-9, -45.5, 9, -30]);
    this._slab(apronRect, -0.02, P.asphalt);
    const outerRect = ground?.outer_road || (outer ? [
      Number(grassRect[0]), Number(outer.y) - Number(outer.width) / 2,
      Number(grassRect[2]), Number(outer.y) + Number(outer.width) / 2] : [-52, -51.5, 52, -45.5]);
    this._slab(outerRect, 0.0, P.road);

    // 3) 9 块铺装（lane/plaza/aisle 用不同明度区分）
    for (const surf of roadSurfaces) {
      const material = surf.kind === 'plaza' ? P.plaza : P.road;
      this._slab(surf.rect, 0.008, material);
    }
    if (!roadSurfaces.length) {
      // 旧 manifest 无 road_surfaces：按 corridor/plazas/aisles 兜底
      const c = m.corridor();
      const pl = m.plazas() || {};
      this._slab([c.in_x - 1.75, -45.5, c.in_x + 1.75, 22.75], 0.008, P.road);
      this._slab([c.out_x - 1.75, -45.5, c.out_x + 1.75, 22.75], 0.008, P.road);
      for (const key of ['n', 's']) {
        const p = pl[key];
        if (!p) continue;
        this._slab([p.x - p.w / 2, p.y - p.h / 2, p.x + p.w / 2, p.y + p.h / 2], 0.008, P.plaza);
      }
    }

    // 4) 绿化分区（wings/planting/south_strips/median_segments，按 raise 抬高）
    const addGreen = (list = [], material = P.median) => {
      for (const g of list) {
        const h = Number(g.raise || 0.1);
        this._slab(g.rect, h, material);
        // 路缘：沿矩形外圈画一圈略高的细条（简化：4 条 curb 板）
        const [x0, y0, x1, y1] = g.rect.map(Number);
        const t = 0.12;
        this._slab([x0, y0, x1, y0 + t], h + 0.005, P.curb);
        this._slab([x0, y1 - t, x1, y1], h + 0.005, P.curb);
        this._slab([x0, y0, x0 + t, y1], h + 0.005, P.curb);
        this._slab([x1 - t, y0, x1, y1], h + 0.005, P.curb);
      }
    };
    if (green) {
      addGreen(green.wings, P.median);
      addGreen(green.planting, P.grass);
      addGreen(green.south_strips, P.grass);
      addGreen(green.median_segments, P.median);
    }

    // 5) 停止线（2 条：入口 -25.8 / 出口 -33.7）
    const stops = markings?.stop_lines || [
      { x: -3.5, paint_y: -25.8, width: 3.5, thickness: 0.4 },
      { x: 3.5, paint_y: -33.7, width: 3.5, thickness: 0.4 },
    ];
    for (const s of stops) {
      const w = Number(s.width || 3.5), t = Number(s.thickness || 0.4);
      this._slab([s.x - w / 2, s.paint_y - t / 2, s.x + w / 2, s.paint_y + t / 2], 0.02, P.line);
    }
    return { meshes: this.group.children.length };
  }

  clear() {
    for (const c of [...this.group.children]) {
      this.group.remove(c);
      c.geometry?.dispose();
    }
  }
}
