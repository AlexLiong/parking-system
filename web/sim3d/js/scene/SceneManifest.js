// 场景清单（scene/SceneManifest.js）—— route-v2 的单一事实源加载器。
//
// 事实源：assets_3d/layout/lot_v2.json（由 _qa_audit/route_v2/build_manifest.py 生成，
// 115 节点 / 113 边 / 7 冲突区 / 80 车位 / 道具与调色板）。本模块负责：
//   1) 按候选 URL 加载 manifest（后端 /assets3d 挂载 → /layout → /sim/assets → 相对路径）；
//   2) 校验 version / 必需字段 / manifest_hash 格式（Python 侧 canonical JSON 与 JS 浮点
//      repr 不完全一致，故只做格式与可选期望值比对，详细信任链由后端挂载保证）；
//   3) 坐标换算：Blender (x, y, z) → three (x, z, -y)，three_z = -y；
//   4) 派生：sceneCfg / gatesCfg / defaultSpaces() / 静态碰撞体 colliders / 道具与调色板访问器。
//
// 说明：本文件属于 three-scene 写作用域（js/scene/**），不修改 manifest JSON 本体。
import { SCENE_DEFAULT, SCENE_MANIFEST_URLS, SHELL, MARKING_DEFAULTS } from '../config.js';

const REQUIRED_SECTIONS = [
  'lot', 'outer_road', 'apron', 'corridor', 'gates', 'aisles', 'plazas', 'rows',
  'space_grid', 'zone_lanes', 'zone_ports', 'queue', 'conflict_zones',
  'props', 'palette', 'markings', 'features',
];
const ZONES = ['A', 'B', 'C', 'D'];

export class ManifestError extends Error {
  constructor(message, attempts = []) {
    super(message);
    this.name = 'ManifestError';
    this.attempts = attempts;
  }
}

/** 校验 manifest 结构；返回错误字符串数组（空数组=通过）。 */
export function validateManifest(raw) {
  const errors = [];
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return ['manifest 不是 JSON 对象'];
  }
  if (raw.manifest_version == null) errors.push('缺 manifest_version');
  else if (Number(raw.manifest_version) < 2) errors.push(`manifest_version=${raw.manifest_version} < 2`);
  if (raw.coordinate_system && raw.coordinate_system !== 'blender_xy') {
    errors.push(`coordinate_system=${raw.coordinate_system}（期望 blender_xy）`);
  }
  for (const key of REQUIRED_SECTIONS) {
    if (raw[key] == null) errors.push(`缺字段 ${key}`);
  }
  const gates = raw.gates || {};
  for (const g of ['in', 'out']) {
    if (!gates[g] || !Number.isFinite(Number(gates[g].x)) || !Number.isFinite(Number(gates[g].y))) {
      errors.push(`gates.${g} 坐标非法`);
    }
  }
  const sg = raw.space_grid || {};
  for (const z of ZONES) {
    const g = sg[z];
    if (!g) { errors.push(`space_grid.${z} 缺失`); continue; }
    if (!Number.isFinite(Number(g.x_start)) || !Number.isFinite(Number(g.pitch)) ||
        !Number.isInteger(Number(g.columns)) || !Array.isArray(g.rows) || !g.rows.length) {
      errors.push(`space_grid.${z} 结构非法`);
    }
  }
  const props = raw.props || {};
  for (const key of ['booth', 'cameras', 'signs', 'info_screen', 'chargers', 'lamps', 'trees']) {
    if (props[key] == null) errors.push(`props.${key} 缺失`);
  }
  // 围墙：旧版放 props.walls，manifest v2.1 起移到顶层 walls（两者兼容）
  if (!Array.isArray(raw.walls) && !Array.isArray(props.walls)) {
    errors.push('缺 walls（顶层或 props.walls）');
  }
  // 可选：静态碰撞体（manifest v2.1 新增，供调度器）
  if (raw.static_colliders != null && !Array.isArray(raw.static_colliders)) {
    errors.push('static_colliders 非数组');
  }
  const hash = raw.manifest_hash;
  if (hash != null && !/^sha256:[0-9a-f]{16,64}$/i.test(String(hash))) {
    errors.push(`manifest_hash 格式非法: ${hash}`);
  }
  if (!raw.conflict_zones || !Array.isArray(raw.conflict_zones)) errors.push('conflict_zones 非数组');
  return errors;
}

/** Blender → three 坐标换算（唯一约定）：x 不变、three_y=blender_z、three_z=-blender_y。 */
export function toThree(x, y, z = 0) {
  return { x: Number(x), y: Number(z), z: -Number(y) };
}
export function toThreeXZ(x, y) {
  return { x: Number(x), z: -Number(y) };
}

export class SceneManifest {
  /**
   * @param {object} raw lot_v2.json 解析结果
   * @param {{sourceUrl?:string, expectedHash?:string}} [meta]
   */
  constructor(raw, meta = {}) {
    if (!raw || typeof raw !== 'object') throw new ManifestError('manifest 为空');
    this.raw = raw;
    this.sourceUrl = meta.sourceUrl || '(inline)';
    this.expectedHash = meta.expectedHash || null;
    this.manifestVersion = Number(raw.manifest_version || 0);
    this.roadGraphVersion = Number(raw.road_graph_version || 0);
    this.manifestHash = String(raw.manifest_hash || '');
    this.synthetic = !!raw._synthetic;
    this.validationErrors = validateManifest(raw);
    if (this.validationErrors.length) {
      throw new ManifestError(`manifest 校验失败: ${this.validationErrors.join('; ')}`,
        [{ url: this.sourceUrl, error: this.validationErrors.join('; ') }]);
    }
    // 可选：期望 hash 精确比对（后端可把 hash 注入 /api/layout 或 URL ?hash=）
    this.hashVerified = null;
    if (this.expectedHash && this.manifestHash) {
      this.hashVerified = String(this.expectedHash).toLowerCase() === this.manifestHash.toLowerCase();
      if (!this.hashVerified) {
        console.warn('[SceneManifest] hash 与期望值不一致:', this.expectedHash, '!=', this.manifestHash);
      }
    }
    this._colliders = null;
    this._spaces = null;
  }

  get hashShort() {
    return this.manifestHash ? this.manifestHash.replace(/^sha256:/i, '').slice(0, 8) : 'n/a';
  }

  /** 按候选 URL 依次尝试加载；全部失败抛 ManifestError（含每次失败原因）。 */
  static async load({ urls = null, timeoutMs = 8000, expectedHash = null } = {}) {
    const attempts = [];
    const candidates = [...(urls || [])];
    // 允许 ?manifest=<url> 显式覆盖（自检/联调方便）
    try {
      const qp = new URLSearchParams(globalThis.location?.search || '');
      if (qp.get('manifest')) candidates.unshift(qp.get('manifest'));
      if (!expectedHash && qp.get('hash')) expectedHash = qp.get('hash');
    } catch (_) { /* 非浏览器环境忽略 */ }
    for (const url of (candidates.length ? candidates : SCENE_MANIFEST_URLS)) {
      try {
        const res = await fetch(url, { cache: 'no-cache', credentials: 'same-origin' });
        if (!res.ok) { attempts.push({ url, error: `HTTP ${res.status}` }); continue; }
        const text = await res.text();
        if (!text || !text.trim().startsWith('{')) {
          attempts.push({ url, error: '响应不是 JSON 对象（可能被静态站点回退成 index.html）' });
          continue;
        }
        const raw = JSON.parse(text);
        if (raw && raw.spaces && raw.scene && !raw.space_grid) {
          // /api/layout 兜底：从后端布局合成 manifest（无静态壳，程序化地面）
          const synth = SceneManifest.fromLayout(raw, { sourceUrl: url });
          attempts.push({ url, error: '非 manifest，已用 /api/layout 合成' });
          return synth;
        }
        return new SceneManifest(raw, { sourceUrl: url, expectedHash });
      } catch (e) {
        attempts.push({ url, error: e.message || String(e) });
      }
    }
    throw new ManifestError(
      `manifest 加载失败（尝试 ${attempts.length} 个地址）: ` +
      attempts.map((a) => `${a.url} → ${a.error}`).join(' | '), attempts);
  }

  /**
   * 从旧 /api/layout 响应合成最小 manifest（仅在 manifest 完全不可用时兜底）。
   * 目的：让车位/道闸/相机仍能工作；无静态壳、无道具清单时由 ParkingScene 回退旧常量。
   */
  static fromLayout(layout, meta = {}) {
    const s = layout.scene || {};
    const g = layout.gates || {};
    const entry = g.entry || {};
    const exit = g.exit || {};
    const lotX = Number(s.lot_x || SCENE_DEFAULT.lot_x);
    const topY = Number(s.top_y || SCENE_DEFAULT.top_y);
    const botY = Number(s.bot_y || SCENE_DEFAULT.bot_y);
    const raw = {
      _synthetic: true,
      manifest_version: 2,
      road_graph_version: Number(s.road_graph_version || 2),
      coordinate_system: 'blender_xy',
      lot: { x_min: -lotX, x_max: lotX, y_min: botY, y_max: topY },
      outer_road: { y: Number(s.outer_road_y ?? SCENE_DEFAULT.outer_road_y), width: 6 },
      apron: { y_min: Number(s.apron_y_min ?? SCENE_DEFAULT.apron_y_min), y_max: botY },
      corridor: { in_x: Number(entry.x ?? s.entry_gate_x ?? -3.5), out_x: Number(exit.x ?? s.exit_gate_x ?? 3.5), lane_w: 3.5, median_half: 1.75 },
      gates: {
        in: { x: Number(entry.x ?? s.entry_gate_x ?? -3.5), y: Number(entry.y ?? s.gate_y ?? -25.5), stop_paint_y: Number(entry.stop_line_y ?? s.stop_line_entry_y ?? -25.8), stop_clearance_m: 0.3 },
        out: { x: Number(exit.x ?? s.exit_gate_x ?? 3.5), y: Number(exit.y ?? s.gate_out_y ?? -34), stop_paint_y: Number(exit.stop_line_y ?? s.stop_line_exit_y ?? -33.7), stop_clearance_m: 0.3 },
      },
      aisles: { n_y: 19.5, s_y: -13.5, width: 6.5, n_lanes: { eb: 17.875, wb: 21.125 }, s_lanes: { eb: -15.125, wb: -11.875 } },
      plazas: { n: { x: 0, y: 19.5, w: 12, h: 6.5 }, s: { x: 0, y: -13.5, w: 12, h: 6.5 } },
      rows: {},
      space_grid: {},
      zone_lanes: {},
      zone_ports: {},
      queue: { entry: { from_y: -28.4, to_y: -45.5 }, exit: { from_y: -31.1, to_y: -16.75 } },
      conflict_zones: [],
      nodes: [],
      edges: [],
      props: { booth: { x: 0, y: -27.5, rot: 180 }, cameras: [], signs: [], info_screen: null, chargers: [], lamps: [], trees: [], walls: [], decor: {} },
      palette: { asphalt: '#2b2d31', road: '#222529', line_white: '#e9e6dc', line_yellow: '#d9b23a', curb: '#b8b3a6', grass: '#4a6b35', wall: '#b5b0a4', median_grass: '#557a3e', plaza: '#26292e' },
      markings: { ...MARKING_DEFAULTS },
      features: { static_shell: '', procedural_space_lines: true, reservation_v2: false },
      manifest_hash: '',
    };
    // 从 spaces 反推每区行/列（去重 y；x 间距取相邻差的中位数）
    const byZone = new Map(ZONES.map((z) => [z, []]));
    for (const sp of (layout.spaces || [])) {
      const z = String(sp.zone || '').toUpperCase();
      if (!byZone.has(z)) byZone.set(z, []);
      byZone.get(z).push(sp);
    }
    for (const [zone, list] of byZone) {
      if (!list.length) continue;
      const ys = [...new Set(list.map((sp) => Number(sp.z)))].sort((a, b) => b - a);
      const xs = [...new Set(list.map((sp) => Number(sp.x)))].sort((a, b) => a - b);
      const pitch = xs.length > 1 ? Math.round((xs[1] - xs[0]) * 1000) / 1000 : 3;
      raw.space_grid[zone] = {
        x_start: Math.round((xs[0] - pitch / 2) * 1000) / 1000,
        pitch,
        columns: xs.length,
        rows: ys.map((y) => ({ y, rotation: Number(list.find((sp) => Number(sp.z) === y)?.rotation || 0) })),
      };
      raw.rows[zone] = raw.space_grid[zone].rows.map((r) => ({ ...r, side: 'unknown' }));
    }
    return new SceneManifest(raw, { sourceUrl: meta.sourceUrl || 'api/layout', expectedHash: meta.expectedHash || null });
  }

  // ----------------------------- 访问器 -----------------------------
  lot() { return this.raw.lot; }
  outerRoad() { return this.raw.outer_road; }
  apron() { return this.raw.apron; }
  corridor() { return this.raw.corridor; }
  gates() { return this.raw.gates; }
  aisles() { return this.raw.aisles; }
  plazas() { return this.raw.plazas; }
  rows() { return this.raw.rows; }
  spaceGrid() { return this.raw.space_grid; }
  zoneLanes() { return this.raw.zone_lanes || {}; }
  zonePorts() { return this.raw.zone_ports || {}; }
  queue() { return this.raw.queue || {}; }
  props() { return this.raw.props || {}; }
  /** manifest v2.1：地面分区（grass_extent/lot_asphalt/apron/outer_road）。 */
  ground() { return this.raw.ground || null; }
  /** manifest v2.1：9 块铺装（中央双车道/分岔区/四条支路/场前区）。 */
  roadSurfaces() { return this.raw.road_surfaces || []; }
  /** manifest v2.1：绿化分区（wings/planting/south_strips/median_segments，含 raise）。 */
  greenAreas() { return this.raw.green_areas || null; }
  /** manifest v2.1：静态壳契约（根节点 LotShell / 子网格 / 材质前缀 / ground_top_z）。 */
  shellContract() { return this.raw.shell_contract || {}; }
  /** manifest v2.1：预计算静态碰撞体（Blender rect）。 */
  staticColliders() { return this.raw.static_colliders || []; }
  /** manifest v2.1：固定标线（停止线/斑马线/箭头/导流），供调试与“避免重复”判断。 */
  markingsV2() { return this.raw.markings_v2 || null; }
  /** 围墙（顶层优先，兼容旧 props.walls）。 */
  walls() { return this.raw.walls || this.props().walls || []; }
  palette() { return { ...(this.raw.palette || {}) }; }
  markings() {
    return { ...MARKING_DEFAULTS, space_w: 3, space_d: 6, line_width_m: 0.15, ...(this.raw.markings || {}) };
  }
  features() { return this.raw.features || {}; }
  conflictZones() { return this.raw.conflict_zones || []; }
  nodes() { return this.raw.nodes || []; }
  edges() { return this.raw.edges || []; }

  /** C 区默认新能源优先（与后端 generator DEFAULT_ZONE_GEOM 一致）。 */
  static kindOf(zone) { return String(zone).toUpperCase() === 'C' ? 'ne_prefer' : 'normal'; }

  /** 80 个车位（与后端 SpaceSpec 同形：x/z 用 Blender 坐标，z=Blender y）。供 ?preview=1。 */
  defaultSpaces() {
    if (this._spaces) return this._spaces;
    const grid = this.spaceGrid();
    const out = [];
    for (const zone of ZONES) {
      const g = grid[zone];
      if (!g) continue;
      let idx = 0;
      for (const row of g.rows) {
        for (let k = 0; k < Number(g.columns); k++) {
          idx += 1;
          out.push({
            space_no: `${zone}-${String(idx).padStart(2, '0')}`,
            zone,
            kind: SceneManifest.kindOf(zone),
            x: Math.round((Number(g.x_start) + Number(g.pitch) * (k + 0.5)) * 1000) / 1000,
            z: Number(row.y),          // Blender y → three z 在场景侧换算
            rotation: Number(row.rotation || 0),
            status: 'free',
          });
        }
      }
    }
    this._spaces = out;
    return out;
  }

  /** 每区总车位数 {A:20,...}（由 space_grid 计算，供预览/校验）。 */
  zoneTotals() {
    const out = {};
    for (const sp of this.defaultSpaces()) out[sp.zone] = (out[sp.zone] || 0) + 1;
    return out;
  }

  /** 预览模式余位（全空）。 */
  freeByZone() {
    return this.zoneTotals();
  }

  /** 兼容旧 GroundBuilder / PathNetwork / CameraRig 的 sceneCfg（Blender 数值，three 换算在使用处）。 */
  sceneCfg() {
    const lot = this.lot();
    const g = this.gates();
    const q = this.queue();
    const a = this.aisles();
    const c = this.corridor();
    const p = this.plazas() || {};
    const entryCenter = Number(q?.entry?.from_y ?? (g.in.y - 0.3 - 2.6));
    const exitCenter = Number(q?.exit?.from_y ?? (g.out.y + 0.3 + 2.6));
    return {
      ...SCENE_DEFAULT,
      road_graph_version: this.roadGraphVersion,
      lot_x: Math.max(Math.abs(lot.x_min), Math.abs(lot.x_max)),
      lot_x_min: lot.x_min, lot_x_max: lot.x_max,
      lot_y_min: lot.y_min, lot_y_max: lot.y_max,
      top_y: lot.y_max, bot_y: lot.y_min,
      apron_y_min: this.apron().y_min, apron_y_max: this.apron().y_max,
      outer_road_y: this.outerRoad().y,
      entry_gate_x: g.in.x, exit_gate_x: g.out.x,
      gate_y: g.in.y, gate_out_y: g.out.y,
      stop_line_entry_y: Number(g.in.stop_paint_y ?? SCENE_DEFAULT.stop_line_entry_y),
      stop_line_exit_y: Number(g.out.stop_paint_y ?? SCENE_DEFAULT.stop_line_exit_y),
      stop_entry_center_y: entryCenter, stop_exit_center_y: exitCenter,
      median_half: Number(c.median_half ?? SCENE_DEFAULT.median_half),
      aisle_w: Number(a.width ?? SCENE_DEFAULT.aisle_w),
      aisle_n: Number(a.n_y ?? SCENE_DEFAULT.aisle_n),
      aisle_s: Number(a.s_y ?? SCENE_DEFAULT.aisle_s),
      lane_n_eb: Number(a.n_lanes?.eb ?? SCENE_DEFAULT.lane_n_eb),
      lane_n_wb: Number(a.n_lanes?.wb ?? SCENE_DEFAULT.lane_n_wb),
      lane_s_eb: Number(a.s_lanes?.eb ?? SCENE_DEFAULT.lane_s_eb),
      lane_s_wb: Number(a.s_lanes?.wb ?? SCENE_DEFAULT.lane_s_wb),
      plaza_n: p.n ? { ...p.n } : SCENE_DEFAULT.plaza_n,
      plaza_s: p.s ? { ...p.s } : SCENE_DEFAULT.plaza_s,
    };
  }

  /** 兼容旧 /api/layout.gates / PathNetwork.configure / BarrierGate 的闸口配置（Blender 坐标）。 */
  gatesCfg() {
    const g = this.gates();
    const q = this.queue() || {};
    return {
      entry: {
        id: 'in', x: g.in.x, y: g.in.y,
        stop_line_y: Number(g.in.stop_paint_y ?? SCENE_DEFAULT.stop_line_entry_y),
        stop_center_y: Number(q.entry?.from_y ?? (g.in.y - 0.3 - 2.6)),
      },
      exit: {
        id: 'out', x: g.out.x, y: g.out.y,
        stop_line_y: Number(g.out.stop_paint_y ?? SCENE_DEFAULT.stop_line_exit_y),
        stop_center_y: Number(q.exit?.from_y ?? (g.out.y + 0.3 + 2.6)),
      },
      booth: { ...(this.props().booth || { x: 0, y: -27.5 }) },
      outer_road_y: this.outerRoad().y,
    };
  }

  /**
   * 静态碰撞体（three 坐标 AABB，供调度器做粗检/静态障碍判断）。
   * 元素：{id, kind, rect:[xmin,ymin,xmax,ymax](Blender，便于对照), minX,maxX,minZ,maxZ, height}
   * 优先使用 manifest v2.1 的 `static_colliders`（绿岛/绿化带/围墙/岗亭/闸箱/信息屏），
   * 缺失时才按审查报告 §8.5.1/§8.5.5 的带宽和道具位置做保守推导。
   */
  colliders() {
    if (this._colliders) return this._colliders;
    const out = [];
    const HEIGHT = {
      wall: 2.2, island: 0.15, planting: 0.1, median: 0.15,
      booth: 2.6, gate: 1.2, prop: 2.8, camera: 3.2, sign: 2.6,
      lamp: 5.5, tree: 4.0, charger: 1.5, bollard: 0.8, planter: 0.6,
    };
    const push = (id, kind, rect) => {
      const [x0, y0, x1, y1] = rect.map(Number);
      const xmin = Math.min(x0, x1), xmax = Math.max(x0, x1);
      const ymin = Math.min(y0, y1), ymax = Math.max(y0, y1);
      out.push({
        id: String(id), kind: String(kind || 'unknown'),
        rect: [xmin, ymin, xmax, ymax],
        minX: xmin, maxX: xmax, minZ: -ymax, maxZ: -ymin,
        height: HEIGHT[kind] ?? 1.0,
      });
    };
    const provided = this.staticColliders();
    if (provided.length) {
      for (const c of provided) {
        if (Array.isArray(c.rect) && c.rect.length === 4) push(c.id, c.kind, c.rect);
      }
      this._colliders = out;
      return out;
    }
    // ---- 兜底推导（旧 manifest / /api/layout 合成）----
    const props = this.props();
    const add = (id, kind, x0, y0, x1, y1) => push(id, kind, [x0, y0, x1, y1]);
    for (const [i, w] of this.walls().entries()) {
      const vertical = Math.abs(w.x1 - w.x0) < 1e-6;
      const pad = 0.15;
      add(`wall_${i}`, 'wall',
        Math.min(w.x0, w.x1) - (vertical ? pad : 0), Math.min(w.y0, w.y1) - (vertical ? 0 : pad),
        Math.max(w.x0, w.x1) + (vertical ? pad : 0), Math.max(w.y0, w.y1) + (vertical ? 0 : pad));
    }
    const c = this.corridor();
    const lot = this.lot();
    // 中央隔离带（走廊中分带，两段：咽喉带内 + 两分岔区之间）
    add('median_corridor', 'median', Number(c.in_x ?? -3.5) + 1.75, -30, Number(c.out_x ?? 3.5) - 1.75, 16.25);
    // 中央绿岛两翼（y∈[-3,3]）
    add('green_island_w', 'island', Number(lot.x_min), -3, -6, 3);
    add('green_island_e', 'island', 6, -3, Number(lot.x_max), 3);
    if (props.booth) add('booth', 'booth', props.booth.x - 1.1, props.booth.y - 1.1, props.booth.x + 1.1, props.booth.y + 1.1);
    for (const [i, cam] of (props.cameras || []).entries()) {
      add(`camera_${cam.gate || i}`, 'camera', cam.x - 0.2, cam.y - 0.2, cam.x + 0.2, cam.y + 0.2);
    }
    for (const s of (props.signs || [])) {
      add(`sign_${s.zone || 'x'}`, 'sign', s.x - 0.35, s.y - 0.35, s.x + 0.35, s.y + 0.35);
    }
    if (props.info_screen) {
      const p = props.info_screen;
      add('info_screen', 'prop', p.x - 0.9, p.y - 0.35, p.x + 0.9, p.y + 0.35);
    }
    for (const [i, ch] of (props.chargers || []).entries()) {
      add(`charger_${i}`, 'charger', ch.x - 0.55, ch.y - 0.35, ch.x + 0.55, ch.y + 0.35);
    }
    for (const [i, lp] of (props.lamps || []).entries()) {
      add(`lamp_${i}`, 'lamp', lp.x - 0.18, lp.y - 0.18, lp.x + 0.18, lp.y + 0.18);
    }
    for (const [i, t] of (props.trees || []).entries()) {
      const inside = t.x >= lot.x_min && t.x <= lot.x_max && t.y >= lot.y_min && t.y <= lot.y_max;
      if (!inside) continue;
      add(`tree_${i}`, 'tree', t.x - 0.3, t.y - 0.3, t.x + 0.3, t.y + 0.3);
    }
    for (const [i, b] of (props.decor?.bollards || []).entries()) {
      add(`bollard_${i}`, 'bollard', b.x - 0.12, b.y - 0.12, b.x + 0.12, b.y + 0.12);
    }
    for (const [i, pl] of (props.decor?.planters || []).entries()) {
      const w = Number(pl.w || 2), d = Number(pl.d || 1);
      add(`planter_${i}`, 'planter', pl.x - w / 2, pl.y - d / 2, pl.x + w / 2, pl.y + d / 2);
    }
    // 闸箱（道闸立柱底座；杆臂不注册为静态障碍，开合由状态机裁决）
    const gc = this.gatesCfg();
    for (const gate of ['entry', 'exit']) {
      const cfg = gc[gate];
      if (!cfg) continue;
      const dx = gate === 'entry' ? 1.95 : -1.95;   // 与 config.GATE_PLACEMENT 对齐
      add(`gatebox_${gate}`, 'gate', cfg.x + dx - 0.3, cfg.y - 0.3, cfg.x + dx + 0.3, cfg.y + 0.3);
    }
    this._colliders = out;
    return out;
  }

  /** shell 文件名（shell_contract.file → features.static_shell → lot_shell.glb；空串表示无壳）。 */
  shellFile() {
    const sc = this.shellContract();
    if (sc && typeof sc.file === 'string' && sc.file) return sc.file;
    const f = this.features();
    return f.static_shell === '' ? '' : (f.static_shell || SHELL.file);
  }

  /** 供调试/日志的一行摘要。 */
  describe() {
    return `manifest v${this.manifestVersion} hash=${this.hashShort} ` +
      `gates(in ${this.gates().in.x},${this.gates().in.y}; out ${this.gates().out.x},${this.gates().out.y}) ` +
      `spaces=${this.defaultSpaces().length} source=${this.sourceUrl}`;
  }

  /**
   * 完整性报告（加载后打印/校验用）：
   * 版本、hash（格式+可选期望值比对）、车位/节点/边/冲突区/静态碰撞体数量、shell 契约。
   * 注意：Python build_manifest 的 canonical hash 含 float repr（如 180.0），JS 无法逐字节复现，
   * 故这里只做 hash 格式与“后端/URL 提供的期望 hash”比对；数值真值以 manifest 文件本身为准。
   */
  integrity() {
    const spaces = this.defaultSpaces();
    return {
      version: this.manifestVersion,
      roadGraphVersion: this.roadGraphVersion,
      hash: this.manifestHash,
      hashFormatOk: /^sha256:[0-9a-f]{16,64}$/i.test(this.manifestHash),
      hashVerified: this.hashVerified,     // true/false/null(未提供期望值)
      spaces: spaces.length,
      perZone: this.zoneTotals(),
      nodes: (this.raw.nodes || []).length,
      edges: (this.raw.edges || []).length,
      conflictZones: (this.raw.conflict_zones || []).length,
      staticColliders: this.staticColliders().length,
      walls: this.walls().length,
      props: {
        booth: !!this.props().booth,
        cameras: (this.props().cameras || []).length,
        signs: (this.props().signs || []).length,
        chargers: (this.props().chargers || []).length,
        lamps: (this.props().lamps || []).length,
        trees: (this.props().trees || []).length,
      },
      shell: this.shellFile(),
      sourceUrl: this.sourceUrl,
    };
  }
}
