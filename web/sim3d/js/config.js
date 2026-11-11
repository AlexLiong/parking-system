// 仿真端全局配置与坐标约定（无构建 ES 模块）。
//
// 坐标约定（与 assets_3d/src/scene_full.py 及后端 layout 生成器逐一对齐）：
//   后端 layout 使用 (x, z, rotation°)，其中 x=Blender X(东)，z=Blender Y(北)，
//   rotation=Blender rotation_euler.z（度）。glTF 导出把 Blender Z-up 转为 Y-up：
//     three_x = x,  three_y = 高度,  three_z = -z
//   经推导，绕 up 轴的旋转在单轴翻转下保持同号：
//     object.rotation.y = deg2rad(rotation)
//   车辆 glb 的"车头"在 rotation=0 时朝向 three -Z（cars.py: 车头朝 Blender +Y
//   → 导出后 three -Z）。因此让车沿世界方向 d=(dx,dz) 行驶时，需解
//   R_y(θ)·(0,0,-1) = (dx,dz)/|d|，得 θ = atan2(-dx, -dz)。
export const SPACE_W = 3.0;
export const SPACE_D = 6.0;
export const LINE_W = 0.15;

export const deg2rad = (d) => (d * Math.PI) / 180;
export const rad2deg = (r) => (r * 180) / Math.PI;

// 后端 layout (x, z) → three.js Vector3(x, y, -z)
export function toThree(x, z, y = 0) {
  return { x, y, z: -z };
}

// 车头朝向：沿世界方向 (dx, dz) 行驶时的 rotation.y（车头本地朝 -Z）
export function headingTo(dx, dz) {
  return Math.atan2(-dx, -dz);
}

// 场景几何缺省（route-v2：以 assets_3d/layout/lot_v2.json 为准；
// 此表仅作 manifest / /api/layout.scene 均不可用时的离线兜底，保证页面不白屏）。
// 单位米，Blender 坐标（x 东、y 北），映射见上方注释。
export const SCENE_DEFAULT = {
  // ---- 旧字段（兼容 GroundBuilder / PathNetwork / 旧后端 scene） ----
  lot_x: 36.0,
  top_y: 29.5,
  bot_y: -30.0,
  main_road_half: 3.0,
  entry_gate_x: -3.5,
  exit_gate_x: 3.5,
  gate_y: -25.5,
  outer_road_y: -48.5,
  stop_line_entry_y: -25.8,
  stop_line_exit_y: -33.7,
  // ---- route-v2 新增字段（对照审查报告 §8.9.3 / lot_v2.json） ----
  road_graph_version: 2,
  lot_x_min: -36.0,
  lot_x_max: 36.0,
  lot_y_min: -30.0,
  lot_y_max: 29.5,
  apron_y_min: -45.5,
  apron_y_max: -30.0,
  gate_out_y: -34.0,             // 出口闸南移至 -34.0
  stop_entry_center_y: -28.4,    // 入口车心停止点
  stop_exit_center_y: -31.1,     // 出口车心停止点
  median_half: 1.75,             // 中央隔离带半宽（入口/出口车道之间）
  aisle_w: 6.5,                  // 场内支路总宽
  aisle_n: 19.5,                 // 北支路中心 y
  aisle_s: -13.5,                // 南支路中心 y
  lane_n_eb: 17.875,             // 北支路东行车道中心 y
  lane_n_wb: 21.125,             // 北支路西行车道中心 y
  lane_s_eb: -15.125,            // 南支路东行车道中心 y
  lane_s_wb: -11.875,            // 南支路西行车道中心 y
  plaza_n: { x: 0.0, y: 19.5, w: 12.0, h: 6.5 },
  plaza_s: { x: 0.0, y: -13.5, w: 12.0, h: 6.5 },
};

// 道闸摆放（对齐 scene_full.place_gates）：E1 绕 Z 转 180°，位于 entry_gate_x+1.95
export const GATE_PLACEMENT = {
  in: { dx: 1.95, rotDeg: 180 },
  out: { dx: -1.95, rotDeg: 0 },
};

// 抬杆角度（three.js 中 Arm.rotation.z: 0 → +82°，见 barrier_gate.py 注释）
export const ARM_OPEN_RAD = deg2rad(82);

// 车型 glb 与碰撞尺寸（§8.1：sedan 4.6×1.8, suv 4.8×1.9, van 5.2×2.0）
export const CAR_MODELS = {
  blue: [
    { file: 'car_sedan_standard_blue.glb', family: 'sedan', len: 4.6, wid: 1.8 },
    { file: 'car_sedan_standard_white.glb', family: 'sedan', len: 4.6, wid: 1.8 },
    { file: 'car_sedan_standard_red.glb', family: 'sedan', len: 4.6, wid: 1.8 },
    { file: 'car_suv_urban_white.glb', family: 'suv', len: 4.8, wid: 1.9 },
    { file: 'car_suv_urban_black.glb', family: 'suv', len: 4.8, wid: 1.9 },
    { file: 'car_hatchback_city_blue.glb', family: 'hatch', len: 4.2, wid: 1.8 },
    { file: 'car_van_passenger_white.glb', family: 'van', len: 5.2, wid: 2.0 },
  ],
  green: [
    { file: 'car_nev_sedan_white.glb', family: 'sedan', len: 4.6, wid: 1.8 },
    { file: 'car_nev_suv_white.glb', family: 'suv', len: 4.8, wid: 1.9 },
    { file: 'car_nev_hatch_green.glb', family: 'hatch', len: 4.2, wid: 1.8 },
    { file: 'car_nev_sport_blue.glb', family: 'sedan', len: 4.6, wid: 1.8 },
    { file: 'car_nev_van_blue.glb', family: 'van', len: 5.2, wid: 2.0 },
  ],
};

// 演示用真实样式车牌图库（相对项目根；后端识别用，前端只上报路径）
export const PLATE_IMAGES = {
  "blue": [
    "datasets/demo-lpr/blue-000-clean.png",
    "datasets/demo-lpr/blue-001-clean.png",
    "datasets/demo-lpr/blue-002-clean.png",
    "datasets/demo-lpr/blue-003-clean.png",
    "datasets/demo-lpr/blue-004-clean.png"
  ],
  "green": [
    "datasets/demo-lpr/green_car-000-clean.png",
    "datasets/demo-lpr/green_car-001-clean.png",
    "datasets/demo-lpr/green_car-002-clean.png",
    "datasets/demo-lpr/green_car-003-clean.png",
    "datasets/demo-lpr/green_car-004-clean.png",
    "datasets/demo-lpr/green_large-000-clean.png",
    "datasets/demo-lpr/green_large-001-clean.png",
    "datasets/demo-lpr/green_large-002-clean.png",
    "datasets/demo-lpr/green_large-003-clean.png"
  ],
  "green_large": [
    "datasets/demo-lpr/green_large-000-clean.png",
    "datasets/demo-lpr/green_large-001-clean.png",
    "datasets/demo-lpr/green_large-002-clean.png",
    "datasets/demo-lpr/green_large-003-clean.png"
  ],
  "yellow": [
    "datasets/demo-lpr/yellow-000-clean.png",
    "datasets/demo-lpr/yellow-001-clean.png",
    "datasets/demo-lpr/yellow-002-clean.png",
    "datasets/demo-lpr/yellow-003-clean.png"
  ]
};

// 运动学参数（§7.3）
export const KIN = {
  straightSpeed: 2.5,   // m/s 直行
  turnSpeed: 1.2,       // m/s 弯道
  accelTime: 1.2,       // s 起步到全速
  brakeTime: 0.8,       // s 制动
  minFollowGap: 1.0,    // m 最小跟车间距
  wheelRadius: 0.33,    // m 车轮半径（角速度 = v / r）
};

// 杆区（虚拟地感）半宽/半长：route-v2 语义改为“车头到杆平面 ±0.9 m”（审查报告 §8.8）；
// 调度器若仍按中心点判定，可用 ZONE_HALF.centerFallback 兼容。
export const ZONE_HALF = { along: 0.9, across: 1.9, centerFallback: { along: 0.6, across: 1.9 } };

// ---------------------------------------------------------------------------
// route-v2 场景装配（SceneManifest / 静态壳 / 道具 / 相机）
// ---------------------------------------------------------------------------
// 单一事实源 manifest 的候选地址（按优先级依次尝试；后端可挂 /assets3d，
// 也可把 manifest 复制到 /sim/assets/）。允许 URL 查询参数 ?manifest=<url> 覆盖。
export const SCENE_MANIFEST_URLS = [
  '/assets3d/layout/lot_v2.json',
  '/layout/lot_v2.json',
  '/sim/assets/lot_v2.json',
  './assets/lot_v2.json',
];

// 静态壳（Blender 导出的 lot_shell.glb，由 blender-scene 产出并复制到 web/sim3d/assets/）。
// shell 缺失时自动回退 GroundBuilder 程序化地面；shell_enabled=false 可强制走回退路径。
export const SHELL = {
  enabled: true,
  file: 'lot_shell.glb',
  // 壳内已含地面/道路/标线/绿岛/围墙时置 true：程序化 MarkingBuilder 只补车位线
  includesGround: true,
  includesWalls: true,
  includesMarkings: true,
};

// 素材库 glb 中同名模板沿 +X 的 base 偏移（导出时的平铺位置；按节点名实例化时须减去）。
// blender-assets 若调整素材库平铺位置，只需改这里。
export const ASSET_BASE_OFFSETS = {
  booth_props: { Booth: 0, CameraPole: 3, LprCamera_in: 6, LprCamera_out: 9 },
  signage: {
    ZoneSign_A: 0, ZoneSign_B: 3, ZoneSign_C: 6, ZoneSign_D: 9,
    SignText_A: 0, SignText_B: 3, SignText_C: 6, SignText_D: 9,
    InfoPylon: 12, InfoScreen: 12, InfoScreenDemo: 12,
  },
  env_props: {
    TreeCone: 0, TreeBall: 5, Lamp: 10, WallSegment: 15,
    ChargingPile: 20, ChargingPile_B: 25,
  },
};

// 程序化标线参数（manifest.markings 会覆盖）
export const MARKING_DEFAULTS = {
  space_w: 3.0, space_d: 6.0, line_width_m: 0.15,
  line_y: 0.045, tint_y: 0.03, label_y: 0.055,
};

export const API_BASE = '';   // 同源（FastAPI 托管），空前缀

// 演示终端凭据（仅仿真端自动登录用，供"缴费/摄像头注入/人工放行"等管理动作）。
// 生产部署应关闭或由操作员手动登录；此处便于离线一体化演示。
