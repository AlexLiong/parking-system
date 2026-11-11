// 停车场总装（scene/ParkingScene.js）—— route-v2 场景装配。
//
// 装配顺序：manifest（唯一事实源）→ 静态场地壳 lot_shell.glb（缺失回退 GroundBuilder）
//          → MarkingBuilder（只补车位白线/编号/C 区绿底，避免与壳重复）
//          → 按 manifest.props 摆道具（岗亭/摄像头杆/导视/信息屏/充电桩/路灯/树；y 用 props.z）
//          → 两座道闸（入口 x=-3.5,y=-25.5；出口 x=+3.5,y=-34.0，来自 manifest.gates）
//          → 注册静态碰撞体（manifest.static_colliders → three AABB，供调度器查询）
//
// 保持的外部 API（供 main.js / TrafficScheduler / UI 使用）：
//   scene.gates / scene.spaces / spaceByNo() / markers / setFreeBoard() / setGateScreen()
//   scene.sceneCfg / scene.gatesCfg / buildFromLayout() / applyLayout()
//   scene.scene（THREE.Scene）/ scene.assets / scene.spaceByNo() / scene.colliders / staticColliders
import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { SCENE_DEFAULT, SHELL, ASSET_BASE_OFFSETS, deg2rad } from '../config.js';
import { Lighting } from './Lighting.js';
import { GroundBuilder } from './GroundBuilder.js';
import { ProceduralGround } from './ProceduralGround.js';
import { MarkingBuilder } from './MarkingBuilder.js';
import { AssetFactory } from './AssetFactory.js';
import { BarrierGate } from '../entities/BarrierGate.js';
import { SpaceMarker } from '../entities/SpaceMarker.js';

// 旧场景兜底（manifest 不可用、仅 /api/layout 老后端时）道具/树/围墙常量，复刻 scene_full.py
const LEGACY = {
  trees: [
    [-30, 28.2, 0], [-22, 28.2, 1], [-14, 28.2, 0], [10, 28.2, 1],
    [18, 28.2, 0], [26, 28.2, 1], [34.5, 10, 0], [34.5, -10, 1],
    [-34.5, 8, 1], [-34.5, -8, 0], [20, -31.5, 1], [28, -31.5, 0],
    [-20, -31.5, 0], [-28, -31.5, 1], [-12, -33.5, 0], [12, -33.5, 1],
  ],
  signs: [['A', -5.6, -16.8, 200], ['C', 5.6, -16.8, 160],
    ['B', -5.6, 16.8, 20], ['D', 5.6, 16.8, -20]],
  chargers: [[12, 'ChargingPile'], [17, 'ChargingPile_B'], [22, 'ChargingPile'], [27, 'ChargingPile_B']],
};

const CAMERA_NODES = ['CameraPole'];
const BOOTH_NODES = ['Booth'];

export class ParkingScene {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = opts;
    this.renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, powerPreference: 'high-performance',
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    Lighting.configureRenderer(this.renderer);

    this.scene = new THREE.Scene();
    this.lighting = new Lighting(this.scene);
    this.lighting.setNight(false);

    this.assets = new AssetFactory();
    // 程序化地面：manifest v2 → ProceduralGround（新路网铺装回退）；
    // 无 manifest（老 /api/layout）→ 旧 GroundBuilder（旧路网回退）。
    this.ground = null;                 // 旧版程序化地面（惰性创建）
    this.proceduralGround = null;       // V2 程序化地面（shell 缺失时的回退）
    this.markingBuilder = new MarkingBuilder(this.scene);
    this.markers = new SpaceMarker(this.scene);

    this.shellGroup = new THREE.Group(); this.shellGroup.name = 'ShellGroup';
    this.propGroup = new THREE.Group(); this.propGroup.name = 'Props';
    this.gateGroup = new THREE.Group(); this.gateGroup.name = 'Gates';
    this.debugGroup = new THREE.Group(); this.debugGroup.name = 'DebugColliders';
    this.debugGroup.visible = false;
    this.scene.add(this.shellGroup, this.propGroup, this.gateGroup, this.debugGroup);

    this.gates = {};                 // 'in' | 'out' -> BarrierGate
    this.spaces = new Map();         // space_no -> {no,zone,kind,x,z(three),rotation,status}
    this.sceneCfg = { ...SCENE_DEFAULT };
    this.gatesCfg = null;
    this.layoutVersion = 0;
    this.manifest = null;
    this.shell = null;
    this.shellLoaded = false;
    this.shellFallback = false;
    this.colliders = [];             // three AABB：{id,kind,minX,maxX,minZ,maxZ,height}
    this.staticColliders = this.colliders;  // 别名（调度器契约）
    this._infoScreenMesh = null;     // 运行时余位屏（自建 CanvasTexture 平面）
    this._infoScreenHolder = null;
    this._infoScreenBakedDemo = null; // 旧 signage.glb 的 InfoScreenDemo（烘死“余位 42”文字，隐藏）
    this._lastFree = null;           // 缓存最近一次余位数据，屏就绪后立即回填
    this._propsBuilt = false;
    this._spaceList = [];
  }

  // ----------------------------- 构建 -----------------------------
  /**
   * 首次依据布局（+可选 manifest）构建整个场景。
   * @param {object} layout /api/layout 响应（preview 模式由 main.js 合成）
   * @param {Function} ackCb 道闸动画回执 (gate,state)=>void
   * @param {{manifest?:object, shellEnabled?:boolean, preview?:boolean}} [opts]
   */
  async buildFromLayout(layout, ackCb, opts = {}) {
    this.manifest = opts.manifest || this.manifest || null;
    this.preview = !!opts.preview;
    this.shellEnabled = opts.shellEnabled !== false;
    this.layoutVersion = layout?.version ?? 0;
    this.sceneCfg = this._mergeSceneCfg(layout?.scene);
    this.gatesCfg = this._resolveGatesCfg(layout?.gates);
    this._indexSpaces(layout?.spaces || []);

    await this._buildStatic(opts);
    this.markingBuilder.build(layout?.spaces || [], this.manifest);
    this.markers.build(layout?.spaces || []);
    await this._placeGates(ackCb);
    if (!this._propsBuilt) {
      await this._placeProps();
      // 性能：道具（岗亭/摄像头/导视/信息屏/充电桩/路灯/树）全部静态，按材质合并；
      // 动态余位屏（InfoScreenCanvas）排除在合批之外，保持可更新。
      try { this.propMergeStats = this._mergeStaticProps(); } catch (e) { console.warn('[ParkingScene] 道具合批失败（保持原状）：', e); }
      this._propsBuilt = true;
    }
    this._registerColliders();
    this.setFreeBoard(layout?.free || {}, layout?.free_total);
    return this;
  }

  _mergeSceneCfg(layoutScene) {
    // 优先级：SCENE_DEFAULT < /api/layout.scene < manifest（v2 事实源）
    const fromManifest = this.manifest && !this.manifest.synthetic ? this.manifest.sceneCfg() : {};
    return { ...SCENE_DEFAULT, ...(layoutScene || {}), ...fromManifest };
  }

  _resolveGatesCfg(layoutGates) {
    if (this.manifest && !this.manifest.synthetic) return this.manifest.gatesCfg();
    if (layoutGates && layoutGates.entry && layoutGates.exit) return layoutGates;
    return {
      entry: { x: this.sceneCfg.entry_gate_x, y: this.sceneCfg.gate_y, stop_line_y: this.sceneCfg.stop_line_entry_y },
      exit: { x: this.sceneCfg.exit_gate_x, y: this.sceneCfg.gate_out_y ?? this.sceneCfg.gate_y, stop_line_y: this.sceneCfg.stop_line_exit_y },
      booth: { x: 0, y: -27.5 },
      outer_road_y: this.sceneCfg.outer_road_y,
    };
  }

  /**
   * 静态部分：壳 or 程序化 V2 地面 or 旧 GroundBuilder + 围墙。
   * 强约束（qa-runtime P0 回归）：无论壳加载/材质修正/遍历是否抛错，本方法都不得中断
   * 场景构建；结束时必须处于“壳 / ProceduralGround / 旧 GroundBuilder”三种可用状态之一
   * （最坏情况纯道具场景也必须可渲染，且不访问 null._built）。
   */
  async _buildStatic(opts = {}) {
    try {
      const file = this.manifest ? this.manifest.shellFile() : '';
      const canTryShell = !!(this.manifest && !this.manifest.synthetic && file &&
        SHELL.enabled && this.shellEnabled && !this.shellLoaded);
      if (canTryShell) {
        try {
          this.shell = await this.assets.loadShell(file, { scale: SHELL.scale || 1 });
        } catch (e) {
          console.warn('[ParkingScene] loadShell 异常，按无壳回退：', e);
          this.shell = null;
        }
      }
      if (this.shell) {
        this._attachShell(this.shell);
      }
    } catch (e) {
      console.error('[ParkingScene] 静态壳装配失败，回退程序化地面：', e);
      this.shell = null;
      this.shellLoaded = false;
    }
    // 安全网：只要没有可用壳，就保证有程序化地面（幂等，重复调用无副作用）
    if (!this.shellLoaded) this._ensureFallbackGround();
    // 围墙：壳内已含则跳过；否则异步摆瓦（单段失败不影响整体）
    if (!this.shellLoaded) {
      try { await this._buildFallbackWalls(); } catch (e) { console.warn('[ParkingScene] 回退围墙失败：', e); }
    }
  }

  _attachShell(shell) {
    this.shell = shell;
    this.shellLoaded = true;
    this.shellFallback = false;
    if (!shell.parent) this.shellGroup.add(shell);
    if (this.proceduralGround) { try { this.proceduralGround.clear(); } catch (_) { /* ignore */ } this.proceduralGround = null; }
    if (this.ground) this.ground.staticGroup.visible = false;
    try { this.lighting.fitToLot(this.manifest.lot()); } catch (_) { /* ignore */ }
    const sc = (this.manifest && this.manifest.shellContract) ? this.manifest.shellContract() : {};
    try {
      if (sc.children && shell.getObjectByName(sc.root_node || 'LotShell')) {
        this.shellContractOk = sc.children.every((n) => !!shell.getObjectByName(n));
        if (!this.shellContractOk) console.warn('[ParkingScene] shell 子节点与契约不一致:', sc.children);
      }
    } catch (_) { /* 契约自检失败不影响渲染 */ }
  }

  /** 幂等回退：优先 ProceduralGround（manifest v2 新路网），否则旧 GroundBuilder。 */
  _ensureFallbackGround() {
    this.shellFallback = true;
    if (this.shell) { try { this.shell.visible = false; } catch (_) { /* ignore */ } this.shell = null; }
    try {
      if (this.manifest && !this.manifest.synthetic) {
        if (!this.proceduralGround) {
          this.proceduralGround = new ProceduralGround(this.scene, this.manifest);
          this.proceduralGround.build();
        }
        if (this.ground) this.ground.staticGroup.visible = false;
        try { this.lighting.fitToLot(this.manifest.lot()); } catch (_) { /* ignore */ }
        return;
      }
      if (!this.ground) this.ground = new GroundBuilder(this.scene);
      if (!this.ground._built) {
        this.ground.buildStatic(this.sceneCfg);
        this.ground._built = true;
      }
      this.ground.staticGroup.visible = true;
    } catch (e) {
      // 最后兜底：无地面也必须让场景可渲染（道具/标线/道闸仍可见）
      console.error('[ParkingScene] 程序化地面构建失败（继续渲染道具）：', e);
      this.shellFallback = true;
    }
  }

  async _buildFallbackWalls() {
    if (this._wallsBuilt) return;
    this._wallsBuilt = true;
    const walls = this.manifest ? this.manifest.walls() : [];
    if (walls.length) {
      for (const w of walls) {
        const dx = w.x1 - w.x0, dy = w.y1 - w.y0;
        const n = Math.max(1, Math.floor(Math.hypot(dx, dy) / 4.0));
        const vertical = Math.abs(dy) > Math.abs(dx);
        for (let i = 0; i < n; i++) {
          const t = (i + 0.5) / n;
          const wx = w.x0 + dx * t, wy = w.y0 + dy * t;
          this._inst('env_props.glb', ['WallSegment'], ASSET_BASE_OFFSETS.env_props.WallSegment,
            wx, wy, 0, vertical ? 90 : 0).catch(() => {});
        }
      }
    } else {
      this._wallRunsLegacy();
    }
  }

  _wallRunsLegacy() {
    const s = this.sceneCfg;
    const LOT_X = s.lot_x, TOP_Y = s.top_y, BOT_Y = s.bot_y;
    const runs = [
      [[-LOT_X, TOP_Y], [LOT_X, TOP_Y]],
      [[-LOT_X, BOT_Y], [-LOT_X, TOP_Y]],
      [[LOT_X, BOT_Y], [LOT_X, TOP_Y]],
      [[-LOT_X, BOT_Y], [-8.0, BOT_Y]],
      [[8.0, BOT_Y], [LOT_X, BOT_Y]],
    ];
    for (const [[x0, y0], [x1, y1]] of runs) {
      const dx = x1 - x0, dy = y1 - y0;
      const n = Math.max(1, Math.floor(Math.hypot(dx, dy) / 4.0));
      const vertical = Math.abs(dy) > Math.abs(dx);
      for (let i = 0; i < n; i++) {
        const t = (i + 0.5) / n;
        this._inst('env_props.glb', ['WallSegment'], ASSET_BASE_OFFSETS.env_props.WallSegment,
          x0 + dx * t, y0 + dy * t, 0, vertical ? 90 : 0).catch(() => {});
      }
    }
  }

  _indexSpaces(spaces) {
    this._spaceList = spaces || [];
    this.spaces.clear();
    for (const sp of this._spaceList) {
      this.spaces.set(sp.space_no, {
        no: sp.space_no, zone: sp.zone, kind: sp.kind,
        x: sp.x, z: -sp.z,             // three 世界坐标（z = -Blender y）
        rotation: sp.rotation || 0,
        status: sp.status || 'free',
      });
    }
  }

  spaceByNo(no) { return this.spaces.get(no) || null; }

  /** 找一个空闲车位（本地兜底展示；权威分配仍在后端）。 */
  freeSpaceIn(zone) {
    for (const s of this.spaces.values()) {
      if (s.zone === zone && s.status === 'free') return s;
    }
    return null;
  }

  /** 布局热更新：重建动态车位内容并重新注册固定manifest碰撞体。 */
  applyLayout(layout) {
    this.layoutVersion = layout?.version ?? this.layoutVersion;
    this.sceneCfg = this._mergeSceneCfg(layout?.scene);
    this.gatesCfg = this._resolveGatesCfg(layout?.gates) || this.gatesCfg;
    this._indexSpaces(layout?.spaces || []);
    this.markingBuilder.build(layout?.spaces || [], this.manifest);
    this.markers.build(layout?.spaces || []);
    this._registerColliders();
    this.setFreeBoard(layout?.free || {}, layout?.free_total);
    return this;
  }

  // ----------------------------- 道闸 -----------------------------
  async _placeGates(ackCb) {
    const g = this.gatesCfg || {};
    const entry = g.entry || { x: this.sceneCfg.entry_gate_x, y: this.sceneCfg.gate_y };
    const exit = g.exit || { x: this.sceneCfg.exit_gate_x, y: this.sceneCfg.gate_out_y ?? this.sceneCfg.gate_y };
    const mk = (id, cfg) => {
      const gate = new BarrierGate(id, this.assets, ackCb, { gateX: cfg.x, gateY: cfg.y });
      this.gateGroup.add(gate.root);
      return gate;
    };
    this.gates.in = mk('in', entry);
    this.gates.out = mk('out', exit);
    await Promise.all([this.gates.in.ready, this.gates.out.ready]);
    // 性能：道闸 26 个 mesh → 静态部件按材质合批（Arm/DisplayScreen 保留动画/动态贴图能力）
    for (const gate of Object.values(this.gates)) {
      try { this._mergeGateStatics(gate); } catch (e) { console.warn('[ParkingScene] 道闸合批失败（保持原状）：', e); }
    }
  }

  /**
   * 道闸静态部件合批：Arm/ArmTip（动画）与 DisplayScreen（动态贴图）保留，
   * 其余 mesh 按材质合并到 gate.root 下，降低 draw call（每闸 13 → 约 7~8）。
   */
  _mergeGateStatics(gate) {
    const root = gate.root;
    root.updateMatrixWorld(true);
    const invRoot = new THREE.Matrix4().copy(root.matrixWorld).invert();
    const KEEP = new Set(['Arm', 'ArmTip', 'DisplayScreen', 'GateScreenCanvas']);
    const isUnderArm = (o) => { let p = o; while (p) { if (p.name === 'Arm' || p.name === 'ArmTip') return true; p = p.parent; } return false; };
    const meshes = [];
    root.traverse((o) => {
      if (!o.isMesh) return;
      if (KEEP.has(o.name) || isUnderArm(o)) return;
      meshes.push(o);
    });
    const buckets = new Map();
    for (const mesh of meshes) {
      const mat = mesh.material;
      if (!mat || Array.isArray(mat)) continue;      // 多材质部件保留（极少）
      const g = mesh.geometry;
      const sig = Object.keys(g.attributes || {}).sort().join(',') + '|idx=' + (g.index ? 1 : 0);
      const key = `${mat.uuid}|${sig}`;
      if (!buckets.has(key)) buckets.set(key, { mat, geos: [], meshes: [], cast: false, receive: true });
      const b = buckets.get(key);
      const rel = new THREE.Matrix4().multiplyMatrices(invRoot, mesh.matrixWorld);
      const cg = g.clone();
      cg.applyMatrix4(rel);
      b.geos.push(cg);
      b.meshes.push(mesh);
      b.cast = b.cast || mesh.castShadow;
      b.receive = b.receive && mesh.receiveShadow;
    }
    let mergedCount = 0;
    const replaced = [];
    for (const b of buckets.values()) {
      if (!b.geos.length) continue;
      const merged = b.geos.length === 1 ? b.geos[0] : mergeGeometries(b.geos, false);
      if (b.geos.length > 1) {
        if (!merged) { for (const g of b.geos) g.dispose(); continue; }  // 合批失败：保留原 mesh
        for (const g of b.geos) g.dispose();
      }
      if (!merged) continue;
      const m = new THREE.Mesh(merged, b.mat);
      m.name = `GateMerged_${gate.gate}_${b.mat.name || mergedCount}`;
      m.castShadow = b.cast;
      m.receiveShadow = b.receive;
      root.add(m);
      mergedCount++;
      replaced.push(...b.meshes);
    }
    for (const mesh of replaced) {
      if (mesh.parent) mesh.parent.remove(mesh);
      try { mesh.geometry?.dispose(); } catch (_) { /* ignore */ }
    }
    return { before: meshes.length, after: mergedCount };
  }

  gateCenter(gateId) {
    const g = this.gates[gateId];
    return g ? g.zoneCenter() : null;
  }

  // ----------------------------- 道具 -----------------------------
  /**
   * 实例化素材库节点。
   * @param {string} file 素材库 glb
   * @param {string[]} nodeNames 候选节点名（按序尝试，兼容 blender-assets 重命名）
   * @param {number} base 素材库中沿 +X 平铺的 base 偏移（实例化时减去）
   * @param {number} x Blender x
   * @param {number} y Blender y（three z = -y）
   * @param {number} height three y（落地高度，manifest props.z）
   * @param {number} rotDeg Blender 绕 Z 旋转（度）
   */
  async _inst(file, nodeNames, base, x, y, height = 0, rotDeg = 0, parent = null) {
    const c = await this.assets.cloneNodeAny(file, nodeNames);
    if (!c) return null;
    c.position.x -= Number(base) || 0;
    const holder = new THREE.Group();
    holder.add(c);
    holder.position.set(Number(x), Number(height) || 0, -Number(y));
    holder.rotation.y = deg2rad(rotDeg || 0);
    holder.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    (parent || this.propGroup).add(holder);
    return { holder, node: c };
  }

  async _placeProps() {
    if (this.manifest && !this.manifest.synthetic) return this._placePropsFromManifest();
    return this._placePropsLegacy();
  }

  /** route-v2：按 manifest.props 摆放（y = props.z 落地高度，rot 为 Blender 绕 Z 度）。 */
  async _placePropsFromManifest() {
    const p = this.manifest.props();
    const off = ASSET_BASE_OFFSETS;
    const tasks = [];

    // 岗亭（中央隔离带上，z=0.15）
    if (p.booth) {
      tasks.push(this._inst('booth_props.glb', BOOTH_NODES, off.booth_props.Booth,
        p.booth.x, p.booth.y, p.booth.z || 0, p.booth.rot || 0));
    }
    // 两台摄像头立杆（跟随新停止线/出口闸）
    for (const cam of (p.cameras || [])) {
      tasks.push(this._inst('booth_props.glb', CAMERA_NODES, off.booth_props.CameraPole,
        cam.x, cam.y, cam.z || 0, cam.rot || 0));
    }
    // Idea2 主项目接入：入口/出口 LPR 闸口相机（blender-assets 在 booth_props.glb 新增
    // LprCamera_in/out 节点；镜头中心=manifest.view_three，pitch 已烘进模型，运行时只转 yaw）。
    for (const cam of (p.lpr_cameras || [])) {
      const node = cam.node || (cam.gate === 'out' ? 'LprCamera_out' : 'LprCamera_in');
      const base = off.booth_props[node];
      if (base == null) {
        console.warn(`[ParkingScene] 未找到 LPR 相机节点 ${node} 的素材偏移，跳过摆放`);
        continue;
      }
      tasks.push(this._inst('booth_props.glb', [node], base,
        cam.x, cam.y, cam.z || 0, cam.rot || 0));
    }
    // 四块区域导视（种植带 z=0.10）
    for (const s of (p.signs || [])) {
      tasks.push(this._zoneSign(s.zone, s.x, s.y, s.rot || 0, s.z || 0));
    }
    // 入口余位信息屏（运行时贴余位）
    if (p.info_screen) {
      tasks.push(this._infoScreen(p.info_screen.x, p.info_screen.y,
        p.info_screen.rot || 0, p.info_screen.z || 0));
    }
    // 充电桩（C 区远排背线外，style A/B 交替）
    for (const ch of (p.chargers || [])) {
      const node = (ch.style || 'A') === 'B' ? 'ChargingPile_B' : 'ChargingPile';
      tasks.push(this._inst('env_props.glb', [node], off.env_props[node],
        ch.x, ch.y, ch.z || 0, ch.rot || 0));
    }
    // 路灯（中分带/闸口/场前）
    for (const lp of (p.lamps || [])) {
      tasks.push(this._inst('env_props.glb', ['Lamp'], off.env_props.Lamp,
        lp.x, lp.y, lp.z || 0, lp.rot || 0));
    }
    // 树（中央绿岛/场外/南咽喉带；kind = cone|ball）
    for (const t of (p.trees || [])) {
      const node = String(t.kind) === 'ball' ? 'TreeBall' : 'TreeCone';
      tasks.push(this._inst('env_props.glb', [node], off.env_props[node],
        t.x, t.y, t.z || 0, 0));
    }
    await Promise.all(tasks);
    // 无壳回退时才补围墙（壳内已含 Shell_Walls）
    if (!this.shellLoaded) await this._buildFallbackWalls();
  }

  /** 旧后端/合成 manifest 兜底：复刻 scene_full.py 的固定摆放（坐标随旧 sceneCfg）。 */
  async _placePropsLegacy() {
    const s = this.sceneCfg;
    const E1 = s.entry_gate_x, E2 = s.exit_gate_x;
    const off = ASSET_BASE_OFFSETS;
    const tasks = [];
    tasks.push(this._inst('booth_props.glb', BOOTH_NODES, off.booth_props.Booth, 0, 21.5, 0, 180));
    tasks.push(this._inst('booth_props.glb', CAMERA_NODES, off.booth_props.CameraPole, E1 - 2.1, 27.2, 0, -35));
    tasks.push(this._inst('booth_props.glb', CAMERA_NODES, off.booth_props.CameraPole, E2 + 2.1, 23.8, 0, 145));
    for (const [L, x, y, rot] of LEGACY.signs) tasks.push(this._zoneSign(L, x, y, rot, 0));
    tasks.push(this._infoScreen(E1 - 4.6, 34.5, 150, 0));
    for (const x of [-28, -14, 0, 14, 28]) {
      tasks.push(this._inst('env_props.glb', ['Lamp'], off.env_props.Lamp, x, -4.2, 0, 180));
    }
    tasks.push(this._inst('env_props.glb', ['Lamp'], off.env_props.Lamp, 5.6, 27.5, 0, 135));
    tasks.push(this._inst('env_props.glb', ['Lamp'], off.env_props.Lamp, -8.5, 36.0, 0, 45));
    for (const [x, y, t] of LEGACY.trees) {
      const node = t === 0 ? 'TreeCone' : 'TreeBall';
      tasks.push(this._inst('env_props.glb', [node], off.env_props[node], x, -y, 0, 0));
    }
    for (const [x, node] of LEGACY.chargers) {
      tasks.push(this._inst('env_props.glb', [node], off.env_props[node], x, -28.6, 0, 180));
    }
    await Promise.all(tasks);
    await this._buildFallbackWalls();
  }

  /**
   * 道具静态合批：岗亭/摄像头杆/导视/信息屏基座/充电桩/路灯/树全部固定不动，
   * 按“材质 + 顶点属性签名”合批成少数 Mesh（新精修资产每件含多个 primitive/material，
   * 不合批时 draw call 会到 200+）。InfoScreenCanvas（动态余位屏）排除，保持可更新。
   */
  _mergeStaticProps() {
    const group = this.propGroup;
    group.updateMatrixWorld(true);
    const invGroup = new THREE.Matrix4().copy(group.matrixWorld).invert();
    const keep = new Set([this._infoScreenMesh]);
    const meshes = [];
    group.traverse((o) => {
      if (!o.isMesh || !o.visible || keep.has(o)) return;
      if (Array.isArray(o.material)) return;      // 多材质部件保留（glb 通常已拆成单材质 primitive）
      meshes.push(o);
    });
    const buckets = new Map();
    for (const mesh of meshes) {
      const mat = mesh.material;
      const g = mesh.geometry;
      if (!mat || !g) continue;
      const sig = Object.keys(g.attributes || {}).sort().join(',') + '|idx=' + (g.index ? 1 : 0);
      const key = `${mat.uuid}|${sig}`;
      if (!buckets.has(key)) buckets.set(key, { mat, geos: [], meshes: [], cast: false, receive: true });
      const b = buckets.get(key);
      const rel = new THREE.Matrix4().multiplyMatrices(invGroup, mesh.matrixWorld);
      const cg = g.clone();
      cg.applyMatrix4(rel);
      b.geos.push(cg);
      b.meshes.push(mesh);
      b.cast = b.cast || mesh.castShadow;
      b.receive = b.receive && mesh.receiveShadow;
    }
    const mergedGroup = new THREE.Group();
    mergedGroup.name = 'PropsMerged';
    let mergedCount = 0;
    const replaced = [];
    for (const b of buckets.values()) {
      if (!b.geos.length) continue;
      const merged = b.geos.length === 1 ? b.geos[0] : mergeGeometries(b.geos, false);
      if (b.geos.length > 1) {
        if (!merged) { for (const g of b.geos) g.dispose(); continue; }
        for (const g of b.geos) g.dispose();
      }
      if (!merged) continue;
      const m = new THREE.Mesh(merged, b.mat);
      m.name = `PropMerged_${b.mat.name || mergedCount}`;
      m.castShadow = b.cast;
      m.receiveShadow = b.receive;
      mergedGroup.add(m);
      mergedCount++;
      replaced.push(...b.meshes);
    }
    if (!mergedCount) return { before: meshes.length, after: meshes.length, merged: 0 };
    group.add(mergedGroup);
    for (const mesh of replaced) {
      if (mesh.parent) mesh.parent.remove(mesh);
      try { mesh.geometry?.dispose(); } catch (_) { /* ignore */ }
    }
    return { before: meshes.length, after: mergedCount, merged: mergedCount };
  }

  async _zoneSign(L, x, y, rotDeg, height = 0) {
    const holder = new THREE.Group();
    holder.position.set(Number(x), Number(height) || 0, -Number(y));
    holder.rotation.y = deg2rad(rotDeg || 0);
    const off = ASSET_BASE_OFFSETS.signage;
    const frame = await this.assets.cloneNodeAny('signage.glb', [`ZoneSign_${L}`, 'ZoneSign']);
    const text = await this.assets.cloneNodeAny('signage.glb', [`SignText_${L}`, 'SignText']);
    const base = off[`ZoneSign_${L}`] ?? 0;
    if (frame) { frame.position.x -= base; holder.add(frame); }
    if (text) { text.position.x -= (off[`SignText_${L}`] ?? base); holder.add(text); }
    // 背面文字：新 signage 的 SignText 只在 -Z 面（正面）。entry/plazaN 等常用机位从背面看
    // 只见纯色板，故克隆一份文字绕 Y 转 180°（几何 z 自动镜像到 +Z 面），保证双面可读；
    // 合批后与正面文字同材质，不增加 draw call。
    if (text) {
      const backText = await this.assets.cloneNodeAny('signage.glb', [`SignText_${L}`, 'SignText']);
      if (backText) {
        backText.position.x -= (off[`SignText_${L}`] ?? base);
        backText.rotation.y = Math.PI;
        holder.add(backText);
      }
    }
    holder.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    this.propGroup.add(holder);
    return { holder };
  }

  /**
   * 入口余位信息屏。
   * 重要（P0 回归）：旧 `signage.glb` 的 `InfoScreenDemo` 节点是把“余位 42”**烘成几何文字**
   * 的占位资产（无贴图/无法动态改字），必须隐藏；运行时用自建 Plane + CanvasTexture 覆盖，
   * 尺寸对齐 InfoScreen 面板（1.1×0.88，屏幕面朝 -Z），内容与 HUD 完全一致。
   */
  async _infoScreen(x, y, rotDeg, height = 0) {
    const holder = new THREE.Group();
    holder.position.set(Number(x), Number(height) || 0, -Number(y));
    holder.rotation.y = deg2rad(rotDeg || 0);
    const off = ASSET_BASE_OFFSETS.signage;
    const pylon = await this.assets.cloneNodeAny('signage.glb', ['InfoPylon', 'InfoScreenPylon']);
    const frame = await this.assets.cloneNodeAny('signage.glb', ['InfoScreen', 'InfoScreenFrame']);
    const baked = await this.assets.cloneNodeAny('signage.glb', ['InfoScreenDemo', 'InfoScreenPanel']);
    if (pylon) { pylon.position.x -= off.InfoPylon; holder.add(pylon); }
    if (frame) { frame.position.x -= off.InfoScreen; holder.add(frame); }
    // 旧占位文字（几何）隐藏，不参与渲染/不占 draw call
    if (baked) {
      baked.position.x -= off.InfoScreenDemo;
      baked.visible = false;
      baked.traverse((o) => { if (o.isMesh) o.visible = false; });
      holder.add(baked);
      this._infoScreenBakedDemo = baked;
    }
    // 自建动态屏：覆盖 InfoScreen 面板正面。
    // 新 signage.glb 面板：y∈[1.36,2.36]、前沿 z=-0.104；旧占位文字 z∈[-0.112,-0.104]。
    // 屏面须放在两者之前（z=-0.118）否则会被不透明面板/占位文字深度遮挡（实测会整块空白）。
    const screen = new THREE.Mesh(
      new THREE.PlaneGeometry(1.06, 0.72),
      new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false, side: THREE.FrontSide }),
    );
    screen.name = 'InfoScreenCanvas';
    screen.position.set(0, 1.86, -0.118);
    screen.rotation.y = Math.PI;                // 让贴图正面朝 -Z（面向入口来车）
    screen.renderOrder = 2;
    holder.add(screen);
    this._infoScreenMesh = screen;              // setFreeBoard 只更新这张屏
    this._infoScreenHolder = holder;
    // 先放一张“余位 --”，避免首帧空白/露出旧占位
    this._refreshInfoScreenTexture(null);
    holder.traverse((o) => { if (o.isMesh && o !== screen) { o.castShadow = true; o.receiveShadow = true; } });
    this.propGroup.add(holder);
    // 若余位数据先到（buildFromLayout 先 setFreeBoard 后 _placeProps 的时序），立即回填
    if (this._lastFree) this._refreshInfoScreenTexture(this._lastFree);
    return { holder };
  }

  // ----------------------------- 静态碰撞体 -----------------------------
  _registerColliders() {
    const list = this.manifest ? this.manifest.colliders() : [];
    this.colliders = list;
    this.staticColliders = list;
    this._buildColliderDebug(list);
    return list;
  }

  _buildColliderDebug(list) {
    // 清空旧调试体
    while (this.debugGroup.children.length) {
      const c = this.debugGroup.children.pop();
      c.geometry?.dispose(); c.material?.dispose();
    }
    const mat = new THREE.MeshBasicMaterial({ color: 0xff9f1c, wireframe: true, transparent: true, opacity: 0.85 });
    for (const c of list) {
      const w = Math.max(0.1, c.maxX - c.minX);
      const d = Math.max(0.1, c.maxZ - c.minZ);
      const h = Math.max(0.1, c.height || 1);
      const geo = new THREE.BoxGeometry(w, h, d);
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set((c.minX + c.maxX) / 2, h / 2, (c.minZ + c.maxZ) / 2);
      this.debugGroup.add(mesh);
    }
    this.debugGroup.visible = this.opts.debugColliders === true;
  }

  setDebugColliders(on) {
    this.debugGroup.visible = !!on;
    return this.debugGroup.visible;
  }

  /** 静态/动态统计（供 __SIM__.dump 与排查）。 */
  stats() {
    const info = this.renderer.info?.render || {};
    let triangles = 0;
    this.scene.traverse((o) => {
      if (o.isMesh && o.geometry?.index) triangles += o.geometry.index.count / 3;
      else if (o.isMesh && o.geometry?.attributes?.position) triangles += o.geometry.attributes.position.count / 3;
    });
    return {
      triangles: Math.round(triangles),
      drawCalls: info.calls ?? null,
      shellLoaded: this.shellLoaded,
      shellFallback: this.shellFallback,
      spaces: this.spaces.size,
      colliders: this.colliders.length,
      gates: Object.keys(this.gates),
      shellMerge: this.shell?.userData?.mergeStats || null,
      propMerge: this.propMergeStats || null,
      markers: this.markers?.stats?.() || null,
    };
  }

  // ----------------------------- 余位屏 -----------------------------
  /**
   * 更新余位屏（HUD 同源数据）。缓存 `_lastFree`：即使屏尚未创建（时序先 setFreeBoard
   * 后 _placeProps），屏就绪时会立即用缓存回填，避免旧占位/空白露馅。
   */
  setFreeBoard(free, freeTotal) {
    this._lastFree = { free: free || {}, freeTotal };
    return this._refreshInfoScreenTexture(this._lastFree);
  }

  _refreshInfoScreenTexture(payload) {
    const data = payload || this._lastFree;
    if (!data || !this._infoScreenMesh) return false;
    const free = data.free || {};
    const freeTotal = data.freeTotal;
    const total = (free && (free.total ?? free.free_total)) ?? freeTotal;
    const lines = [
      `余位 ${total == null ? '--' : total}|#39ff88`,
      `A ${free.A ?? '-'}  B ${free.B ?? '-'}|#7fd4ff`,
      `C ${free.C ?? '-'}  D ${free.D ?? '-'}|#7fd4ff`,
    ];
    const tex = this.assets.screenTexture(lines, { texture: this._infoScreenMesh.material.map, w: 512, h: 320, bg: '#04121a' });
    const old = this._infoScreenMesh.material;
    if (!old.map) old.map = tex;
    old.needsUpdate = true;
    return true;
  }

  setGateScreen(gateId, lines, opts = {}) {
    const g = this.gates[gateId];
    if (g) g.setScreen(lines, opts);
  }

  // ----------------------------- 帧循环 -----------------------------
  update(dt) {
    for (const g of Object.values(this.gates)) g.update(dt);
    this.markers.update(dt);
  }

  render(camera) {
    this.renderer.render(this.scene, camera);
  }

  resize(w, h) {
    this.renderer.setSize(w, h, false);
  }
}
