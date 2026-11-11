// 程序化地面/道路/车位线（scene/GroundBuilder.js）。
// glb 只含道具（车/道闸/岗亭/导视/环境）；地面、车道、车位线、导向箭头、C 区淡绿底
// 由前端**按 /api/layout 数据**绘制，从而支持布局热更新（§7.2、§3.9）。
//
// 复刻 assets_3d/src/scene_full.py 的 ground_and_roads()/paint_lines()/c_zone_tint()：
// Blender 盒 (cx, cy, sx, sy) → three.js 平面 位置 (cx, y, -cy)、rotation.x=-π/2、
// 尺寸 (sx, sy)。
import * as THREE from 'three';
import { SPACE_W, SPACE_D, LINE_W, SCENE_DEFAULT } from '../config.js';

const C = {
  asphalt: 0x292b2e, road: 0x1c1e21, grass: 0x33571f,
  line: 0xd9dbd8, ctint: 0x2f4533, bumpY: 0xd9a60d, bumpB: 0x0d0d0d,
};

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({
    color, roughness: opts.rough ?? 0.95, metalness: opts.metal ?? 0.0,
    polygonOffset: opts.polygonOffset ?? false,
    polygonOffsetFactor: opts.polygonOffset ? -1 : 0,
    polygonOffsetUnits: opts.polygonOffset ? -1 : 0,
  });
}

export class GroundBuilder {
  constructor(scene) {
    this.scene = scene;
    this.staticGroup = new THREE.Group(); this.staticGroup.name = 'GroundStatic';
    this.spaceGroup = new THREE.Group(); this.spaceGroup.name = 'SpaceLines';
    scene.add(this.staticGroup);
    scene.add(this.spaceGroup);
    this._mats = {
      asphalt: mat(C.asphalt), road: mat(C.road), grass: mat(C.grass, { rough: 1 }),
      line: mat(C.line, { rough: 0.7 }), ctint: mat(C.ctint, { rough: 0.95 }),
      bumpY: mat(C.bumpY, { rough: 0.6 }), bumpB: mat(C.bumpB, { rough: 0.7 }),
    };
    this._numTex = new Map();
  }

  /** 在 Blender (cx,cy) 处放一块 sx×sy 的水平面板（y 为高度）。 */
  _slab(group, cx, cy, sx, sy, y, material) {
    const g = new THREE.PlaneGeometry(sx, sy);
    const m = new THREE.Mesh(g, material);
    m.rotation.x = -Math.PI / 2;
    m.position.set(cx, y, -cy);
    m.receiveShadow = true;
    group.add(m);
    return m;
  }

  /** 静态地面与道路（不依赖车位数据）。 */
  buildStatic(sceneCfg = {}) {
    const s = { ...SCENE_DEFAULT, ...sceneCfg };
    const LOT_X = s.lot_x, TOP_Y = s.top_y, BOT_Y = s.bot_y;
    const MAIN = s.main_road_half, E1 = s.entry_gate_x, E2 = s.exit_gate_x;
    const OUTER = s.outer_road_y;
    const g = this.staticGroup;
    const { asphalt, road, grass, line } = this._mats;

    this._slab(g, 0, -5, 125, 115, -0.08, grass);            // GrassBase
    this._slab(g, 0, 0, LOT_X * 2 + 2, TOP_Y - BOT_Y + 1, -0.03, asphalt); // LotGround
    this._slab(g, 0, OUTER, 125, 6, 0.0, road);               // OuterRoad
    this._slab(g, 0, -36.5, 16, 13, -0.02, asphalt);          // Apron
    this._slab(g, 0, 0, LOT_X * 2, MAIN * 2, 0.012, road);    // MainRoad
    // 区支路
    this._slab(g, -21, 19.5, 30, 5, 0.012, road);             // AisleA
    this._slab(g, 21, 19.5, 30, 5, 0.012, road);              // AisleC
    this._slab(g, -21, -19.5, 30, 5, 0.012, road);            // AisleB
    this._slab(g, 21, -19.5, 30, 5, 0.012, road);             // AisleD
    // 入口/出口竖向车道
    this._slab(g, E1, -23.0, 3.5, 40, 0.012, road);           // EntryLane
    this._slab(g, E2, -23.0, 3.5, 40, 0.012, road);           // ExitLane
    // 主车道中央虚线
    for (let x = -33; x <= 33; x += 4) this._slab(g, x + 1.0, 0, 2.0, 0.15, 0.02, line);
    // 停止线 + 闸前竖向导流
    this._slab(g, E1, s.stop_line_entry_y, 3.5, 0.4, 0.02, line);
    this._slab(g, E2, s.stop_line_exit_y, 3.5, 0.4, 0.02, line);
    this._slab(g, E1, -36.5, 0.35, 2.2, 0.02, line);
    this._slab(g, E2, -36.5, 0.35, 2.2, 0.02, line);
    // 减速带（黄黑相间）
    [E1, E2].forEach((gx) => {
      for (let i = 0; i < 6; i++) {
        const m = i % 2 === 0 ? this._mats.bumpY : this._mats.bumpB;
        this._slab(g, gx - 1.45 + i * 0.58, -40.5, 0.5, 0.18, 0.05, m);
      }
    });
    // 入口/出口导向箭头
    this._arrow(g, E1, -35.3, +1, line);
    this._arrow(g, E2, -37.7, -1, line);
  }

  _arrow(group, cx, cy, dir, material) {
    const w = 1.2, h = 1.0;
    const shape = new THREE.Shape();
    shape.moveTo(-w / 2, 0); shape.lineTo(w / 2, 0); shape.lineTo(0, dir * h);
    shape.closePath();
    const geo = new THREE.ShapeGeometry(shape);
    const m = new THREE.Mesh(geo, material);
    m.rotation.x = -Math.PI / 2;
    m.position.set(cx, 0.03, -cy);
    m.receiveShadow = true;
    group.add(m);
  }

  /**
   * 依据车位数据重建车位白线 + C 区淡绿底 + 编号（布局热更新时整体重建）。
   * @param {Array} spaces SpaceSpec[]：{space_no, zone, kind, x, z, rotation}
   */
  buildSpaces(spaces) {
    // 清理旧 mesh（释放几何，材质共享不释放）
    for (const child of [...this.spaceGroup.children]) {
      this.spaceGroup.remove(child);
      if (child.geometry) child.geometry.dispose();
    }
    const { line, ctint } = this._mats;
    for (const sp of spaces) {
      const face = this._faceOf(sp.rotation);   // 开口方向（Blender ±Y）
      this._spaceLines(sp.x, sp.z, face, line);
      if (sp.kind === 'ne_prefer') this._slab(this.spaceGroup, sp.x, sp.z, 2.7, 5.7, 0.014, ctint);
      this._spaceLabel(sp);
    }
  }

  // rotation=0 → 车头/开口朝南(Blender -Y, face=+1 表示 back 在南)；据生成器约定推断
  _faceOf(rotation) {
    // 远排 heading=180（back 在北 y+），近排 heading=0（back 在南 y-）
    const r = ((rotation % 360) + 360) % 360;
    return r > 90 && r < 270 ? -1 : +1;   // -1: back 在北；+1: back 在南
  }

  _spaceLines(cx, cy, face, material) {
    // 两侧边线 + 一条底线（back），复刻 scene_full.space()
    this._slab(this.spaceGroup, cx - SPACE_W / 2 + LINE_W / 2, cy, LINE_W, SPACE_D, 0.02, material);
    this._slab(this.spaceGroup, cx + SPACE_W / 2 - LINE_W / 2, cy, LINE_W, SPACE_D, 0.02, material);
    this._slab(this.spaceGroup, cx, cy - face * (SPACE_D / 2 - LINE_W / 2), SPACE_W, LINE_W, 0.02, material);
  }

  _spaceLabel(sp) {
    let tex = this._numTex.get(sp.space_no);
    if (!tex) {
      const cv = document.createElement('canvas'); cv.width = 128; cv.height = 64;
      const g = cv.getContext('2d');
      g.fillStyle = 'rgba(255,255,255,0.82)';
      g.font = 'bold 34px "PingFang SC","Microsoft YaHei",sans-serif';
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText(sp.space_no, 64, 34);
      tex = new THREE.CanvasTexture(cv);
      tex.colorSpace = THREE.SRGBColorSpace;
      this._numTex.set(sp.space_no, tex);
    }
    const sm = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
    const sprite = new THREE.Sprite(sm);
    sprite.scale.set(1.6, 0.8, 1);
    sprite.position.set(sp.x, 0.05, -sp.z);
    this.spaceGroup.add(sprite);
  }
}
