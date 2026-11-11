// Route V2 纯逻辑单测（web/sim3d/test/route.test.js）
//
// 运行：node --test web/sim3d/test/route.test.js
// 覆盖：RoutePlanner / ConflictZones / ReservationTable / PathNetwork 门面，
//       以及 80 车位 × 进出场全量几何统计（对向共用、交叉、单点汇聚、路径长度、
//       排队、停止线、Z1-Z7 覆盖、净距、每车位 access 节点）。
//
// 数据源：assets_3d/layout/lot_v2.json（manifest 单一事实源）。
// 统计报告同时落盘到 <workspace>/_qa_audit/runtime/three-route/test-report.json（若目录可写）。

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { RoutePlanner, ROUTE_DEFAULTS } from '../js/sim/RoutePlanner.js';
import { PathNetwork } from '../js/sim/PathNetwork.js';
import { ReservationTable, RESERVATION_SAFETY, rectsOverlap, Z7_MARGIN } from '../js/sim/ReservationTable.js';
import { buildConflictZones, legPolyline } from '../js/sim/ConflictZones.js';
import { computeRouteStats, formatMarkdown, CAR_LENGTHS } from './route-stats.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../..');            // <workspace>/parking-system
const WORKSPACE = path.resolve(REPO, '..');             // <workspace>
const RUNTIME_DIR = path.join(WORKSPACE, '_qa_audit/runtime/three-route');

// ---------------------------------------------------------------------------
// manifest 定位（多个候选，支持环境变量覆盖）
// ---------------------------------------------------------------------------
function locateManifest() {
  const candidates = [
    process.env.LOT_V2_MANIFEST,
    path.join(WORKSPACE, 'assets_3d/layout/lot_v2.json'),
    path.join(REPO, 'assets_3d/layout/lot_v2.json'),
    path.join(process.cwd(), 'assets_3d/layout/lot_v2.json'),
    path.join(process.cwd(), '../assets_3d/layout/lot_v2.json'),
  ].filter(Boolean);
  for (const p of candidates) {
    try { if (fs.existsSync(p)) return p; } catch { /* ignore */ }
  }
  throw new Error(`route.test.js: 找不到 lot_v2.json，已尝试:\n${candidates.join('\n')}`);
}

const MANIFEST_PATH = locateManifest();
const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));

const spaces = RoutePlanner.spacesFromManifest(manifest);
const planner = new RoutePlanner(manifest, spaces);
const ZONES = buildConflictZones(manifest);

// 全量统计只算一次（80×80 交叉分析）
const stats = computeRouteStats(planner, manifest);
const spaceNos = planner.spaceNos();

const r3 = (v) => Math.round(Number(v) * 1000) / 1000;

// ---------------------------------------------------------------------------
// 0. manifest / 图
// ---------------------------------------------------------------------------
test('manifest 与图规模', () => {
  assert.ok(manifest.manifest_hash, 'manifest_hash 存在');
  assert.equal(manifest.road_graph_version, 2);
  assert.equal(manifest.nodes.length, 115, '115 节点');
  assert.equal(manifest.edges.length, 113, '113 边');
  assert.equal(spaceNos.length, 80, '80 车位');
  assert.equal(planner.nodes.size, 115);
  assert.equal(planner.edges.size, 113);
  // 自动生成的车位编号与 generator.py 一致
  assert.deepEqual(spaceNos.slice(0, 3), ['A-01', 'A-02', 'A-03']);
  assert.deepEqual(spaceNos.slice(18, 22), ['A-19', 'A-20', 'B-01', 'B-02']);
  const a01 = planner.space('A-01');
  assert.equal(a01.x, -34.5);
  assert.equal(a01.y, 25.75, 'A-01 为远排 y=25.75');
  assert.equal(planner.space('A-11').y, 13.25, 'A-11 为近排 y=13.25');
  assert.equal(planner.space('B-01').y, -7.25, 'B-01 为南近排 y=-7.25');
});

test('全部 80 车位入场/出场均可达', () => {
  for (const no of spaceNos) {
    const e = planner.planEntry(no);
    const x = planner.planExit(no);
    assert.ok(e.legs.length > 0, `${no} 入场腿为空`);
    assert.ok(x.legs.length > 0, `${no} 出场腿为空`);
    assert.ok(e.length > 20 && e.length < 160, `${no} 入场长度异常 ${e.length}`);
    assert.ok(x.length > 20 && x.length < 160, `${no} 出场长度异常 ${x.length}`);
  }
});

// ---------------------------------------------------------------------------
// 1. 几何验收：对向共用 / 交叉 / 单点汇聚
// ---------------------------------------------------------------------------
test('对向共用段（circulation）= 0 m', () => {
  assert.equal(stats.sharedOpposite.circulation, 0, 'circulation 对向共用必须为 0');
  assert.equal(stats.sharedOpposite.pairMax, 0, '任意一对路径的对向共用必须为 0');
  assert.equal(stats.sharedOpposite.pairsWithOpposite, 0);
});

test('交叉点：全部落在冲突区内，冲突区外 = 0', () => {
  assert.equal(stats.crossings.outsideZones, 0,
    `冲突区外交叉点必须为 0，实际 ${JSON.stringify(stats.crossings.outsideZoneList)}`);
  assert.ok(stats.crossings.uniquePoints > 0, '分岔区转向确实存在交叉（应被互斥区覆盖）');
  assert.equal(stats.crossings.insideZones, stats.crossings.uniquePoints,
    '所有交叉点都应落在 Z1..Z6/ZN/ZS 内');
});

test('经 (0,0) 的路径数 = 0', () => {
  assert.equal(stats.throughOrigin.count, 0, '新路网不应有任何路径经过原点');
});

test('同车位存取段单独统计（不共线，优于"允许"）', () => {
  assert.equal(stats.stallAccess.total, 0, '同一车位进出场存取段不共线');
  assert.ok(stats.stallAccess.accessLenPerSpace > 15, '每车位存取段长度应为 ~20.8 m');
});

// ---------------------------------------------------------------------------
// 2. 路径长度 / 排队 / 净距 / 停止线
// ---------------------------------------------------------------------------
test('平均路径长度（入口 ≈57m / 出口 ≈63m，±15%）', () => {
  const e = stats.length.entryFromStop.mean;
  const x = stats.length.exitToStop.mean;
  assert.ok(e >= 57 * 0.85 && e <= 57 * 1.15, `入口均值 ${e} 超出 57±15%`);
  assert.ok(x >= 63 * 0.85 && x <= 63 * 1.15, `出口均值 ${x} 超出 63±15%`);
  assert.ok(stats.length.entryFromStop.min < 30, '存在靠近入口的近车位（约 27.7 m）');
  assert.ok(stats.length.entryFull > stats.length.entryFromStop.mean, '含排队接近段应更长');
});

test('闸前排队 17.1 m / 14.35 m（≥14m）', () => {
  assert.equal(stats.queue.entry, 17.1, '入口排队 = 外场道北缘到停止线车心');
  assert.equal(stats.queue.exit, 14.35, '出口排队 = 出口车心到南分岔区南缘');
  assert.ok(stats.queue.entry >= 14 && stats.queue.exit >= 14);
  // 与 manifest 声明交叉校验
  assert.equal(manifest.queue.entry.length_m, stats.queue.entry);
  assert.equal(manifest.queue.exit.length_m, stats.queue.exit);
});

test('停止线公式：车头距杆平面恒为 0.3 m（≥0.3 且 ≤0.6）', () => {
  assert.equal(stats.stopLine.length, CAR_LENGTHS.length);
  for (const s of stats.stopLine) {
    assert.ok(s.entryClearance >= 0.3 - 1e-9, `${s.len}: 入口车头压杆 ${s.entryClearance}`);
    assert.ok(s.entryClearance <= 0.6 + 1e-9, `${s.len}: 入口车头离杆过远 ${s.entryClearance}`);
    assert.ok(s.exitClearance >= 0.3 - 1e-9, `${s.len}: 出口车头压杆 ${s.exitClearance}`);
    assert.ok(s.exitClearance <= 0.6 + 1e-9);
  }
  // 具体公式：入口 z = -gateY + 0.3 + len/2；出口 z = -gateY - 0.3 - len/2
  assert.equal(planner.entryStopZ(5.2), 28.4);
  assert.equal(planner.exitStopZ(5.2), 31.1);
  assert.equal(planner.entryStopZ(4.6), 28.1);
  assert.equal(planner.exitStopZ(4.6), 31.4);
});

test('对向车道净距：走廊 7.0 m / 支路 3.25 m，van 净距 1.25 m ≥ 1.0 m', () => {
  assert.equal(stats.laneGaps.corridor, 7);
  assert.equal(stats.laneGaps.aisleN, 3.25);
  assert.equal(stats.laneGaps.aisleS, 3.25);
  assert.ok(stats.laneGaps.minCarGap >= 1.0, `最小净距 ${stats.laneGaps.minCarGap}`);
});

// ---------------------------------------------------------------------------
// 3. 冲突区
// ---------------------------------------------------------------------------
test('冲突区覆盖：Z1..Z6 + 派生 ZN/ZS + 80 个 Z7', () => {
  assert.deepEqual(stats.zones.missing, [], 'Z1..Z6 必须全部被至少一条路径经过');
  assert.deepEqual(stats.zones.missingStall, [], '每个车位都要有 Z7:<spaceNo>');
  assert.deepEqual(stats.zones.derivedCovered.sort(), ['ZN', 'ZS'], '派生中央区应被转向轨迹命中');
  assert.equal(stats.zones.coverage.Z1, 80);
  // Z7 的 hold 与矩形来自 ConflictZones
  const z1 = ZONES.zones.get('Z1');
  assert.equal(z1.hold_s, 4);
  assert.deepEqual(ZONES.rectThree('Z1'), { xmin: -5.25, xmax: -1.75, zmin: 25.5, zmax: 31 });
  assert.deepEqual(ZONES.rectThree('Z2'), { xmin: 1.75, xmax: 5.25, zmin: 28.5, zmax: 34 });
});

test('Z7 动态区：车位矩形 + 支路侧机动带 + 邻位 ±1.5 m', () => {
  const a01 = planner.space('A-01');            // x=-34.5, y=25.75（远排，支路 19.5）
  const z = ZONES.stallZone({ space_no: 'A-01', zone: 'A', x: a01.x, z: a01.y });
  assert.equal(z.id, 'Z7:A-01');
  assert.equal(z.hold_s, 4.5);
  // x: -34.5 ± (1.5 + 1.5) = [-37.5, -31.5]
  assert.equal(z.rect[0], -37.5);
  assert.equal(z.rect[2], -31.5);
  // y: 车位 [22.75,28.75] ∪ 支路 [16.25,22.75] ⇒ [16.25,28.75]
  assert.equal(z.rect[1], 16.25);
  assert.equal(z.rect[3], 28.75);
  // 近排（A-11, y=13.25）应向下扩展到支路南缘
  const a11 = planner.space('A-11');
  const z11 = ZONES.stallZone({ space_no: 'A-11', zone: 'A', x: a11.x, z: a11.y });
  assert.equal(z11.rect[1], 10.25);
  assert.equal(z11.rect[3], 22.75);
});

test('每车位 zones 序列（含 Z1/Z2 与分岔区）', () => {
  assert.deepEqual(planner.planEntry('A-01').zones, ['Z1', 'Z3', 'Z7:A-01']);
  assert.deepEqual(planner.planEntry('C-01').zones, ['Z1', 'Z4', 'ZN', 'Z7:C-01']);
  assert.deepEqual(planner.planEntry('B-01').zones, ['Z1', 'Z5', 'Z7:B-01']);
  assert.deepEqual(planner.planEntry('D-01').zones, ['Z1', 'Z6', 'ZS', 'Z7:D-01']);
  assert.deepEqual(planner.planExit('A-01').zones, ['Z7:A-01', 'Z3', 'ZN', 'Z2']);
  assert.deepEqual(planner.planExit('B-01').zones, ['Z7:B-01', 'Z5', 'ZS', 'Z2']);
});

// ---------------------------------------------------------------------------
// 4. 每车位 access 节点
// ---------------------------------------------------------------------------
test('派生区 ZN/ZS：仅转向轨迹穿越中央带时占用（直行不占）', () => {
  // 语义：派生区按"腿"几何命中（见 ConflictZones.js 顶部注释）。
  //   直行穿过分岔区的走廊腿在 x=∓3.5，不在中央带 x∈(-1.75,1.75) ⇒ 永不命中；
  //   只有"入口→C/D"（右转）与"A/B 出口→出口车道"（左转）这类横跨中央带的转向腿命中。
  const zonesOf = (no, kind) => (kind === 'in' ? planner.planEntry(no) : planner.planExit(no, { to: 'stop' })).zones;

  // ① A/C 入场要**直行穿过南分岔区**（去北区/东区） ⇒ 不得占用南分岔区 Z5/Z6 与 ZS
  for (const no of ['A-01', 'A-10', 'A-11', 'A-20', 'C-01', 'C-10', 'C-11', 'C-20']) {
    const z = zonesOf(no, 'in');
    assert.ok(!z.includes('ZS'), `${no} 直行穿南分岔区不应占 ZS`);
    assert.ok(!z.includes('Z5') && !z.includes('Z6'), `${no} 直行穿南分岔区不应占 Z5/Z6`);
  }
  // ② B/D 入场要**直行穿过北分岔区**（去南区/东区） ⇒ 不得占用 ZN 与 Z3/Z4
  for (const no of ['B-01', 'B-10', 'B-11', 'B-20', 'D-01', 'D-10', 'D-11', 'D-20']) {
    const z = zonesOf(no, 'in');
    assert.ok(!z.includes('ZN'), `${no} 直行穿北分岔区不应占 ZN`);
    assert.ok(!z.includes('Z3') && !z.includes('Z4'), `${no} 直行穿北分岔区不应占 Z3/Z4`);
  }
  // ③ 左转（A 入场 / B 入场）全程在西侧，不跨中央带 ⇒ 不占 ZN/ZS
  for (const no of ['A-01', 'A-20', 'B-01', 'B-20']) {
    assert.ok(!zonesOf(no, 'in').includes('ZN'), `${no} 左转不应占 ZN`);
    assert.ok(!zonesOf(no, 'in').includes('ZS'), `${no} 左转不应占 ZS`);
  }
  // ④ 右转（C/D 入场）与左转跨带（A/B 出场）才占用派生区 —— 这正是 8 个交叉点被覆盖的原因
  assert.ok(zonesOf('C-01', 'in').includes('ZN'), 'C 入场右转跨中央带 → ZN');
  assert.ok(zonesOf('D-01', 'in').includes('ZS'), 'D 入场右转跨中央带 → ZS');
  assert.ok(zonesOf('A-01', 'out').includes('ZN'), 'A 出场左转跨中央带 → ZN');
  assert.ok(zonesOf('B-01', 'out').includes('ZS'), 'B 出场左转跨中央带 → ZS');
  // ⑤ C/D 出场不跨中央带 ⇒ 不占 ZN/ZS
  assert.ok(!zonesOf('C-01', 'out').includes('ZN'));
  assert.ok(!zonesOf('D-01', 'out').includes('ZS'));

  // ⑥ 全量计数（每路径计一次）：ZN = 20 条 C 入场 + 20 条 A 出场；ZS 对称
  const znIn = spaceNos.filter((no) => zonesOf(no, 'in').includes('ZN'));
  const znOut = spaceNos.filter((no) => zonesOf(no, 'out').includes('ZN'));
  const zsIn = spaceNos.filter((no) => zonesOf(no, 'in').includes('ZS'));
  const zsOut = spaceNos.filter((no) => zonesOf(no, 'out').includes('ZS'));
  assert.equal(znIn.length, 20);
  assert.equal(znOut.length, 20);
  assert.equal(zsIn.length, 20);
  assert.equal(zsOut.length, 20);
  assert.ok(znIn.every((no) => planner.space(no).zone === 'C'), 'ZN 入场只应来自 C 区');
  assert.ok(znOut.every((no) => planner.space(no).zone === 'A'), 'ZN 出场只应来自 A 区');
  assert.ok(zsIn.every((no) => planner.space(no).zone === 'D'), 'ZS 入场只应来自 D 区');
  assert.ok(zsOut.every((no) => planner.space(no).zone === 'B'), 'ZS 出场只应来自 B 区');
  assert.equal(stats.zones.coverage.ZN, 40);
  assert.equal(stats.zones.coverage.ZS, 40);

  // ⑦ 派生区几何可在 ConflictZones 查询，且带 derived 标记
  assert.deepEqual(ZONES.rectThree('ZN'),
    { xmin: -1.75, xmax: 1.75, zmin: -22.75, zmax: -16.25 });
  assert.deepEqual(ZONES.rectThree('ZS'),
    { xmin: -1.75, xmax: 1.75, zmin: 10.25, zmax: 16.75 });
  assert.equal(ZONES.holdSec('ZN'), 6);
  assert.equal(ZONES.isDerived('ZN'), true);
  assert.equal(ZONES.isDerived('Z3'), false);
  // ⑧ 关闭开关：manifest.features.derived_plaza_zones=false 时不派生
  const czOff = buildConflictZones({
    ...manifest, features: { ...(manifest.features || {}), derived_plaza_zones: false },
  });
  assert.equal(czOff.isDerived('ZN'), false);
  assert.equal(czOff.zones.has('ZN'), false);
});

test('倒车入库/出库扫掠不与邻位已停车辆相交（OBB/SAT，含 van 最坏情况）', () => {
  // 背景：qa-sim 在 headless 实测 stallCurveM=2.6 时，相邻车位倒车会与已停车 OBB 相交 2~7 帧；
  // 默认改为 1.2 后 OBB/AABB 均为 0。本用例用解析扫掠 OBB 复算并锁定参数。
  for (const car of ['sedan', 'van']) {
    const r = stats.stallSweep[car];
    assert.ok(r, `缺少 ${car} 的扫掠统计`);
    assert.equal(r.collisionsEntry, 0,
      `${car}: 倒车入库扫掠与邻位已停车相交 ${r.collisionsEntry} 次`);
    assert.equal(r.collisionsExit, 0,
      `${car}: 前行出库扫掠与邻位已停车相交 ${r.collisionsExit} 次`);
    assert.ok(r.minGapEntry >= 0.1,
      `${car}: 入库扫掠最小间距 ${r.minGapEntry} m < 0.10 m`);
    assert.ok(r.minGapExit >= 0.1,
      `${car}: 出库扫掠最小间距 ${r.minGapExit} m < 0.10 m`);
    assert.ok(r.checkedNeighbors >= 140, `${car}: 邻位检查数量异常 ${r.checkedNeighbors}`);
  }
  // 参数回归：默认 stallCurveM = 1.2（2.6 会让 van 与邻位车相交）
  assert.equal(ROUTE_DEFAULTS.stallCurveM, 1.2);
  assert.equal(ROUTE_DEFAULTS.stallTangentM, 1.6);
  // 单次覆盖生效：planEntry(no,{stallCurveM}) 只改本次弧线长度
  const c02 = planner.space('C-02');
  const tight = planner.planEntry('C-02', { from: 'stop', stallCurveM: 1.0 });
  const bzTight = tight.legs[tight.legs.length - 1];
  assert.ok(Math.abs(bzTight.p1.x - (c02.x - 1.0)) < 1e-6, 'p1 横向伸出量 = stallCurveM');
  const normal = planner.planEntry('C-02', { from: 'stop' });
  const bzNormal = normal.legs[normal.legs.length - 1];
  assert.ok(Math.abs(bzNormal.p1.x - (c02.x - 1.2)) < 1e-6, '默认 1.2');
  const loose = planner.planEntry('C-02', { from: 'stop', stallCurveM: 2.6 });
  const bzLoose = loose.legs[loose.legs.length - 1];
  assert.ok(Math.abs(bzLoose.p1.x - (c02.x - 2.6)) < 1e-6);
});

test('贴墙边界列：入库/出库扫掠不穿场界墙（内缘 ±35.85，容忍 0.05 m 墙面公差）', () => {
  // 背景：A-01/B-01/A-11/B-11/C-10/C-20/D-10/D-20 车位中心距墙内缘仅 1.35 m，
  // 原几何在车位中心处做 90° 转向会让车头/前角扫出墙外（解析复核 van 入库 1.315 / 出库 1.596 m）。
  // 修复：boundaryInsetM=1.5（机动基准内收到 ±33）+ boundaryStallCurveM=0.4（收紧弧线）⇒ 车位落点/朝向不变。
  const w = stats.wallSweep;
  assert.ok(w, '缺少 wallSweep 统计');
  assert.equal(w.wallInner, 35.85);
  assert.ok(w.worstIntrusion <= 0.05,
    `最大越墙 ${w.worstIntrusion} m 超过 0.05 m（车位 ${w.worstSpace}）`);
  // 只允许 8 个贴墙列、且只有 van（5.2×2.0）会触及墙面公差层；sedan 应完全为 0
  const spaces = new Set(w.affected.map((a) => a.spaceNo));
  const expectBoundary = ['A-01', 'A-11', 'B-01', 'B-11', 'C-10', 'C-20', 'D-10', 'D-20'];
  for (const no of spaces) assert.ok(expectBoundary.includes(no), `非边界列出现越墙: ${no}`);
  assert.deepEqual(w.affected.filter((a) => a.car === 'sedan'), [], 'sedan 不应越墙');
  // 边界参数回归
  assert.equal(ROUTE_DEFAULTS.boundaryInsetM, 1.5);
  assert.equal(ROUTE_DEFAULTS.boundaryStallCurveM, 0.4);
  // 边界列车位落点/朝向不变：终点仍是车位中心，入位车头仍等于 rotation
  const a01 = planner.planEntry('A-01', { from: 'stop' });
  const bz = a01.legs[a01.legs.length - 1];
  assert.equal(r3(bz.p3.x), planner.space('A-01').x);
  assert.equal(r3(bz.p3.z), -planner.space('A-01').y);
  // 非边界列不受影响：普通车位弧线仍用 stallCurveM（1.2）
  const a05 = planner.planEntry('A-05', { from: 'stop' });
  const bz5 = a05.legs[a05.legs.length - 1];
  assert.ok(Math.abs(bz5.p1.x - (planner.space('A-05').x + 1.2)) < 1e-6, '非边界列 p1 仍为 stallCurveM=1.2');
});

test('每车位 access：车道 y / 方向 / 入口出口节点与 manifest 一致', () => {
  assert.equal(stats.access.length, 80);
  const zl = manifest.zone_lanes;
  for (const a of stats.access) {
    const lane = zl[a.zone];
    assert.equal(a.aisle, lane.aisle, `${a.spaceNo} aisle`);
    assert.equal(a.entryLaneY, lane.entry_y, `${a.spaceNo} 入口车道 y`);
    assert.equal(a.exitLaneY, lane.exit_y, `${a.spaceNo} 出口车道 y`);
    assert.equal(a.entryDir, Math.sign(lane.entry_dir), `${a.spaceNo} 入口方向`);
    assert.equal(a.exitDir, Math.sign(lane.exit_dir), `${a.spaceNo} 出口方向`);
    assert.ok(a.entryNode && a.exitNode, `${a.spaceNo} 节点缺失`);
    assert.match(a.entryNode, new RegExp(`^${manifest.aisle_nodes[a.zone].entry}_\\d+$`));
    assert.match(a.exitNode, new RegExp(`^${manifest.aisle_nodes[a.zone].exit}_\\d+$`));
    assert.ok(a.entryLen > 0 && a.exitLen > 0);
  }
  // 抽样：A-01 入口节点应为 NAI_9（x=-33，车位 x=-34.5 的"未越过"最后节点）
  const a01 = planner.spaceAccess('A-01');
  assert.equal(a01.entryNode, 'NAI_9');
  assert.equal(a01.exitNode, 'NAO_1');
  // C-01（x=7.5）入口节点应为 NCI_0（x=6）
  assert.equal(planner.spaceAccess('C-01').entryNode, 'NCI_0');
});

test('倒车入库/出库朝向：80 车位入位后车头朝向 = layout rotation（±1°）', () => {
  // 车头朝向换算：three 中车头 local -Z，沿 (dx,dz) 行驶时 heading = atan2(-dx,-dz)；
  // reverse 段车头与运动方向相反（+180°）。
  const deg = (rad) => (rad * 180) / Math.PI;
  const norm = (d) => ((d % 360) + 360) % 360;
  const angDiff = (a, b) => Math.min(Math.abs(a - b), 360 - Math.abs(a - b));
  const headingOf = (dx, dz) => deg(Math.atan2(-dx, -dz));
  let nEntry = 0;
  let nExit = 0;
  for (const no of spaceNos) {
    const sp = planner.space(no);
    const expect = norm(sp.rotation);
    // 入场：最后一段 Bezier（reverse），末端运动方向 = p3-p2
    const e = planner.planEntry(no, { from: 'stop' });
    const last = e.legs[e.legs.length - 1];
    assert.equal(last.kind, 'bezier', `${no} 末段应为 Bezier`);
    assert.equal(last.reverse, true, `${no} 倒车入库应为 reverse`);
    assert.equal(r3(last.p3.x), r3(sp.x), `${no} 终点 x = 车位中心`);
    assert.equal(r3(last.p3.z), r3(-sp.y), `${no} 终点 z = -Blender y`);
    const carHeading = norm(headingOf(last.p3.x - last.p2.x, last.p3.z - last.p2.z) + 180);
    assert.ok(angDiff(carHeading, expect) <= 1,
      `${no}: 入位车头 ${carHeading.toFixed(1)}° ≠ layout rotation ${expect}°`);
    nEntry++;
    // 出场：首段 Bezier（前进），起点运动方向 = p1-p0，车头即运动方向
    const x = planner.planExit(no, { to: 'stop' });
    const first = x.legs[0];
    assert.equal(first.kind, 'bezier');
    assert.notEqual(first.reverse, true);
    assert.equal(r3(first.p0.x), r3(sp.x), `${no} 起点 x = 车位中心`);
    assert.equal(r3(first.p0.z), r3(-sp.y), `${no} 起点 z = -Blender y`);
    const outHeading = norm(headingOf(first.p1.x - first.p0.x, first.p1.z - first.p0.z));
    assert.ok(angDiff(outHeading, expect) <= 1,
      `${no}: 出库车头 ${outHeading.toFixed(1)}° ≠ layout rotation ${expect}°`);
    nExit++;
  }
  assert.equal(nEntry, 80);
  assert.equal(nExit, 80);
  // 抽样固化（回归保护）：远排 rotation=180 → 车头朝 +z（南/朝支路）；近排 rotation=0 → 车头朝 -z
  const a01 = planner.planEntry('A-01', { from: 'stop' });
  const a01b = a01.legs[a01.legs.length - 1];
  assert.equal(r3(a01b.p2.z), -24.15, 'A-01 末端控制点应在车位口的来向一侧');
  assert.ok(a01b.p3.z < a01b.p2.z, 'A-01 末端运动方向 -z（朝车位，reverse 后车头朝 +z/180°）');
  const a11 = planner.planEntry('A-11', { from: 'stop' });
  const a11b = a11.legs[a11.legs.length - 1];
  assert.ok(a11b.p3.z > a11b.p2.z, 'A-11 末端运动方向 +z（reverse 后车头朝 -z/0°）');
});

// ---------------------------------------------------------------------------
// 5. 路径形状（bezier 倒车入库 / 车位中心起终点）
// ---------------------------------------------------------------------------
test('入场末段 = 横移 + 倒车入库 Bezier（reverse，终点车位中心）', () => {
  const res = planner.planEntry('A-11', { from: 'stop' });   // x=-34.5, y=13.25（近排）
  const last = res.legs[res.legs.length - 1];
  assert.equal(last.kind, 'bezier');
  assert.equal(last.reverse, true);
  assert.equal(r3(last.p3.x), -34.5);
  assert.equal(r3(last.p3.z), -13.25, '终点 three z = -Blender y');
  // 倒数第二段是横移（等 x）
  const lateral = res.legs[res.legs.length - 2];
  assert.equal(lateral.kind, 'line');
  assert.equal(r3(lateral.ax - lateral.bx), 0);
  assert.equal(r3(Math.abs(lateral.bz - lateral.az)), 1.625);
  // 起点为停止线车心（from:'stop'）
  assert.equal(r3(res.legs[0].ax), -3.5);
  assert.equal(r3(res.legs[0].az), 28.4);
});

test('出场首段 = 前行出库 Bezier（起点车位中心），终点停止线', () => {
  const res = planner.planExit('D-20', { to: 'stop' });   // x=34.5, y=-19.75（远排）
  const first = res.legs[0];
  assert.equal(first.kind, 'bezier');
  assert.notEqual(first.reverse, true);
  assert.equal(r3(first.p0.x), 34.5);
  assert.equal(r3(first.p0.z), 19.75);
  const last = res.legs[res.legs.length - 1];
  assert.equal(r3(last.bx), 3.5);
  assert.equal(r3(last.bz), 31.1);
});

test('按车长微调停止点（startZ / endZ）', () => {
  const res = planner.planEntry('A-01', { from: 'stop', startZ: planner.entryStopZ(4.6) });
  assert.equal(r3(res.legs[0].az), 28.1);
  const ex = planner.planExit('A-01', { to: 'stop', endZ: planner.exitStopZ(4.6) });
  const last = ex.legs[ex.legs.length - 1];
  assert.equal(r3(last.bz), 31.4);
});

// ---------------------------------------------------------------------------
// 6. 确定性
// ---------------------------------------------------------------------------
test('确定性：同输入两次规划完全一致（无 Math.random）', () => {
  const a = JSON.stringify(planner.planEntry('C-11').legs);
  const b = JSON.stringify(planner.planEntry('C-11').legs);
  assert.equal(a, b);
  assert.equal(stats.deterministic, true);
  // 不同 planner 实例同样一致
  const p2 = new RoutePlanner(manifest, spaces);
  assert.equal(JSON.stringify(p2.planEntry('C-11').legs), a);
});

// ---------------------------------------------------------------------------
// 7. ReservationTable
// ---------------------------------------------------------------------------
test('预约表：1.3 安全系数 + 幂等 + 冲突拒绝 + 释放', () => {
  const rt = new ReservationTable(ZONES);
  assert.equal(RESERVATION_SAFETY, 1.3);
  const r1 = rt.request('v1', 'Z1', 100, 4);
  assert.equal(r1.granted, true);
  assert.equal(r1.until, 100 + 4 * 1.3);
  // 幂等
  const r1b = rt.request('v1', 'Z1', 100, 4);
  assert.equal(r1b.granted, true);
  assert.equal(r1b.idempotent, true);
  assert.equal(rt.size('Z1'), 1, '幂等申请不应重复占位');
  // 冲突拒绝
  const r2 = rt.request('v2', 'Z1', 101, 4);
  assert.equal(r2.granted, false);
  assert.equal(r2.reason, 'conflict');
  assert.equal(r2.blockedBy, 'v1');
  assert.equal(r2.until, 105.2);
  // 不重叠则授予
  assert.equal(rt.request('v2', 'Z1', 110, 4).granted, true);
  assert.equal(rt.isFree('Z1', 116, 4), true);
  assert.equal(rt.isFree('Z1', 104, 4), false);
  // 释放
  assert.equal(rt.release('v1', 'Z1'), true);
  assert.equal(rt.release('v1', 'Z1'), false, '重复释放应幂等返回 false');
  assert.equal(rt.isFree('Z1', 101, 2), true);
  // 释放某车全部
  assert.equal(rt.request('v3', 'Z3', 200, 6).granted, true);
  assert.equal(rt.request('v3', 'Z7:A-01', 210, 4.5).granted, true);
  assert.equal(rt.releaseAll('v3'), 2);
});

test('预约表：优先级（出口优先 / 高优先级可抢占未开始的窗口）', () => {
  const rt = new ReservationTable(ZONES);
  assert.equal(rt.request('in1', 'Z3', 100, 6).granted, true);
  // 出口方向（priority 10）请求一个与 in1 冲突、且开始时刻不晚于 in1 的窗口 ⇒ 可抢占
  const pre = rt.request('out1', 'Z3', 95, 6, { direction: 'out' });
  assert.equal(pre.granted, true);
  assert.equal(pre.preempted, 'in1');
  assert.deepEqual(rt.heldBy('in1'), [], '被抢占者的窗口应被移除');
  // 窗口已开始（t0 更早）则不可抢占
  const late = rt.request('in2', 'Z3', 97, 6, { direction: 'in' });
  assert.equal(late.granted, false);
  // 无优先级差异时同级 FIFO：冲突即拒绝并进入等待队列
  const rt2 = new ReservationTable(ZONES);
  rt2.request('a', 'Z5', 10, 6);
  rt2.request('b', 'Z5', 12, 6);
  assert.equal(rt2.waiters('Z5').length, 1);
  assert.equal(rt2.waiters('Z5')[0].vehicleId, 'b');
});

test('预约表：snapshot / windows / reset', () => {
  const rt = new ReservationTable(ZONES);
  rt.request('v1', 'Z2', 50, 5, { emergency: true });
  const snap = rt.snapshot();
  assert.equal(snap.safety, 1.3);
  assert.equal(snap.activeWindows, 1);
  assert.equal(snap.zones.Z2[0].vehicleId, 'v1');
  assert.equal(snap.zones.Z2[0].emergency, true);
  assert.deepEqual(snap.vehicles.v1, ['Z2']);
  assert.equal(rt.windows('Z2').length, 1);
  rt.reset();
  assert.equal(rt.snapshot().activeWindows, 0);
});

test('Z7 邻位互斥：相邻车位的 Z7 矩形重叠时不得同时持有', () => {
  // 设计：Z7:<spaceNo> = 车位 3×6 + 支路侧机动带 + 邻位 ±1.5 m ⇒ 相邻车位矩形重叠 3.0 m，
  // 仅按 zone id 字符串互斥会漏（qa-runtime multi 采样 z7Violations=2867 的根因）。
  const rt = new ReservationTable(ZONES);
  const r1 = rt.request('m1', 'Z7:A-01', 100, 4.5);
  const r2 = rt.request('m3', 'Z7:A-02', 100.5, 4.5);        // 同排相邻
  assert.equal(r1.granted, true);
  assert.equal(r2.granted, false, 'A-01/A-02 相邻车位必须互斥');
  assert.equal(r2.reason, 'z7-overlap');
  assert.equal(r2.blockedBy, 'm1');
  assert.equal(r2.zoneConflict, 'Z7:A-01');
  // 释放后立即可获
  assert.equal(rt.release('m1', 'Z7:A-01'), true);
  assert.equal(rt.request('m3', 'Z7:A-02', 106, 4.5).granted, true);
  // 不相邻（矩形仅在 x=-31.5 边界相切）可同时
  const rt2 = new ReservationTable(ZONES);
  assert.equal(rt2.request('a', 'Z7:A-01', 10, 4.5).granted, true);
  assert.equal(rt2.request('b', 'Z7:A-03', 10, 4.5).granted, true, 'A-01/A-03 不相邻应可同时');
  // 同列对面排：支路侧机动带重叠 ⇒ 也应互斥
  const rt3 = new ReservationTable(ZONES);
  assert.equal(rt3.request('a', 'Z7:A-01', 10, 4.5).granted, true);
  assert.equal(rt3.request('b', 'Z7:A-11', 10, 4.5).granted, false, 'A-01/A-11 对面排机动带重叠应互斥');
  // 时间窗不重叠 ⇒ 可先后持有
  const rt4 = new ReservationTable(ZONES);
  assert.equal(rt4.request('a', 'Z7:A-01', 10, 4.5).granted, true);
  assert.equal(rt4.request('b', 'Z7:A-02', 20, 4.5).granted, true, '时间不重叠应可获');
  // 同车幂等仍保留
  const rt5 = new ReservationTable(ZONES);
  const a1 = rt5.request('x', 'Z7:A-01', 0, 4.5);
  const a2 = rt5.request('x', 'Z7:A-01', 0, 4.5);
  assert.equal(a1.granted, true);
  assert.equal(a2.idempotent, true);
  // isFree 也遵守 Z7 矩形互斥
  assert.equal(rt5.isFree('Z7:A-02', 0, 4.5), false);
  assert.equal(rt5.isFree('Z7:A-03', 0, 4.5), true);
  // 全 80 车位两两扫描：矩形重叠(>0.1m) ⇔ 互斥；其余必须可同时（3160 对）
  let pairs = 0, overlapPairs = 0, mismatch = 0;
  for (let i = 0; i < spaceNos.length; i++) {
    for (let j = i + 1; j < spaceNos.length; j++) {
      const idA = `Z7:${spaceNos[i]}`;
      const idB = `Z7:${spaceNos[j]}`;
      const overlap = rectsOverlap(ZONES.stallRectById(idA), ZONES.stallRectById(idB), Z7_MARGIN);
      const t = new ReservationTable(ZONES);
      const g1 = t.request('x', idA, 0, 4.5).granted;
      const g2 = t.request('y', idB, 0, 4.5).granted;
      pairs++;
      if (overlap) {
        overlapPairs++;
        if (g1 && g2) mismatch++;
      } else if (!(g1 && g2)) mismatch++;
    }
  }
  assert.equal(pairs, 3160);
  assert.equal(overlapPairs, 184, '重叠对数应为 184（同排相邻 72 + 同列对面排 40 + 斜对 72）');
  assert.equal(mismatch, 0, '任何一对的判定都必须与矩形重叠一致');
  // 无 ConflictZones 时退化为仅同 id 互斥（向后兼容）
  const bare = new ReservationTable(null);
  assert.equal(bare.request('p', 'Z7:A-01', 0, 4.5).granted, true);
  assert.equal(bare.request('q', 'Z7:A-02', 0, 4.5).granted, true);
});

// ---------------------------------------------------------------------------
// 8. PathNetwork 门面（three-scene 集成契约）
// ---------------------------------------------------------------------------
test('门面：对象式构造 + 四个只读属性', () => {
  const pn = new PathNetwork({ manifest, spaces });
  assert.equal(pn.manifestReady, true);
  assert.equal(pn.usingPlanner, true);
  assert.equal(pn.manifest, manifest);
  assert.ok(pn.planner instanceof RoutePlanner);
  assert.ok(pn.reservations instanceof ReservationTable);
  assert.equal(typeof pn.reservations.snapshot, 'function');
  assert.ok(pn.zones && typeof pn.zones.rectThree === 'function');
  assert.equal(pn.manifest.manifest_hash, manifest.manifest_hash);
  assert.equal(pn.outerZ, 48.5);
});

test('门面：两阶段注入 + 热更新 setSpaces', () => {
  const pn = new PathNetwork({}, {});              // 旧式：无 manifest
  assert.equal(pn.manifestReady, false);
  assert.equal(pn.planner, null);
  assert.equal(pn.reservations, null);
  assert.throws(() => pn.entryToSpace('A-01'), /需要先注入 manifest/);
  // 注入
  pn.configure({ manifest, spaces });
  assert.equal(pn.manifestReady, true);
  assert.ok(pn.entryToSpace('A-01').length > 0);
  // 热更新：只换车位表，planner/reservations 保持同一实例
  const plannerRef = pn.planner;
  const resRef = pn.reservations;
  pn.setSpaces(spaces.slice(0, 79));
  assert.equal(pn.planner, plannerRef, 'setSpaces 不应重建 planner');
  assert.equal(pn.reservations, resRef);
  assert.equal(pn.spaces.length, 79);
  pn.setSpaces([]);                                 // 空 ⇒ 回退 manifest 80 位
  assert.equal(pn.spaces.length, 80);
});

test('门面：旧 API 全部保留且返回旧格式 legs', () => {
  const pn = new PathNetwork({ manifest, spaces });
  const oldShape = (leg) => {
    assert.ok(Number.isFinite(leg.ax) && Number.isFinite(leg.az), 'ax/az');
    assert.ok(Number.isFinite(leg.bx) && Number.isFinite(leg.bz), 'bx/bz');
    assert.equal(typeof leg.reverse, 'boolean');
  };
  const ap = pn.entryApproach(48.5);
  assert.ok(ap.length >= 1);
  ap.forEach(oldShape);
  assert.equal(r3(ap[0].az), 48.5);
  assert.equal(r3(ap[ap.length - 1].bz), 28.4);

  const e = pn.entryToSpace({ no: 'A-01', x: -34.5, z: -25.75 });   // 场景口径(z=three)
  assert.ok(e.length > 5);
  e.forEach(oldShape);
  assert.equal(r3(e[e.length - 1].bz), -25.75, '终点=车位中心 three z');

  const x = pn.spaceToExitStop({ no: 'A-01', x: -34.5, z: -25.75 });
  x.forEach(oldShape);
  assert.equal(r3(x[x.length - 1].bz), 31.1);

  const cont = pn.exitContinue();
  assert.ok(cont.length >= 2);
  assert.equal(r3(cont[cont.length - 1].bz), 45.5);

  // 停止线 / 闸位
  assert.deepEqual(pn.entryStop(5.2), { x: -3.5, z: 28.4 });
  assert.deepEqual(pn.exitStop(5.2), { x: 3.5, z: 31.1 });
  assert.deepEqual(pn.entryGate(), { x: -3.5, z: 25.5 });
  assert.deepEqual(pn.exitGate(), { x: 3.5, z: 34 });
  assert.equal(pn.aisleZ(-25.75), -19.5);
  assert.equal(pn.aisleZ(7.25), 13.5);
  // 旧 format 的 bezier 采样：腿数明显多于 rich legs
  const rich = pn.planEntryRich('A-01', { from: 'stop' });
  assert.ok(e.length > rich.legs.length, '旧格式应把 bezier 采样成多段');
});

test('门面：planner 口径/场景口径/字符串三种车位入参等价', () => {
  const pn = new PathNetwork({ manifest, spaces });
  const a = pn.entryToSpace('A-01');
  const b = pn.entryToSpace({ space_no: 'A-01', x: -34.5, z: 25.75 });  // planner 口径
  const c = pn.entryToSpace({ no: 'A-01', x: -34.5, z: -25.75 });       // 场景口径
  assert.equal(JSON.stringify(a), JSON.stringify(b));
  assert.equal(JSON.stringify(a), JSON.stringify(c));
});

test('门面：reservations 可用于不依赖调度器的调试', () => {
  const pn = new PathNetwork({ manifest, spaces });
  const r = pn.reservations.request('probe', 'Z7:A-01', 0, 4.5);
  assert.equal(r.granted, true);
  assert.equal(r3(r.until), 5.85);
  const snap = pn.reservations.snapshot();
  assert.deepEqual(snap.vehicles.probe, ['Z7:A-01']);
  pn.reservations.releaseAll('probe');
  assert.equal(pn.reservations.snapshot().activeWindows, 0);
});

// ---------------------------------------------------------------------------
// 9. 汇总报告（打印给 Lead / demo-qa 引用 + 落盘）
// ---------------------------------------------------------------------------
test('汇总报告', () => {
  const summary = {
    manifest: { path: MANIFEST_PATH, hash: manifest.manifest_hash, roadGraphVersion: manifest.road_graph_version },
    graph: { nodes: planner.nodes.size, edges: planner.edges.size, spaces: spaceNos.length },
    acceptance: {
      circulationOppositeSharedM: stats.sharedOpposite.circulation,
      crossingsOutsideZones: stats.crossings.outsideZones,
      crossingsTotalInZones: stats.crossings.uniquePoints,
      throughOrigin: stats.throughOrigin.count,
      entryMeanM: stats.length.entryFromStop.mean,
      exitMeanM: stats.length.exitToStop.mean,
      entryRangeM: [stats.length.entryFromStop.min, stats.length.entryFromStop.max],
      exitRangeM: [stats.length.exitToStop.min, stats.length.exitToStop.max],
      queueEntryM: stats.queue.entry,
      queueExitM: stats.queue.exit,
      minCarGapM: stats.laneGaps.minCarGap,
      zonesCovered: stats.zones.covered,
      derivedZonesCovered: stats.zones.derivedCovered,
      stallZones: spaceNos.length,
      stallAccessSharedM: stats.stallAccess.total,
      deterministic: stats.deterministic,
    },
  };
  const accessTable = stats.access.map((a) => ({
    spaceNo: a.spaceNo, zone: a.zone, entryNode: a.entryNode, exitNode: a.exitNode,
    entryLaneY: a.entryLaneY, exitLaneY: a.exitLaneY, entryLen: a.entryLen, exitLen: a.exitLen,
  }));

  // 控制台输出（node --test 会原样打印，供 Lead/demo-qa 引用）
  console.log('\n================ Route V2 验收汇总 ================');
  console.log(JSON.stringify(summary, null, 2));
  console.log('---- 每车位 access 节点（80） ----');
  console.log(accessTable.map((a) => `${a.spaceNo}:${a.entryNode}->${a.exitNode}`).join('  '));
  console.log('==================================================\n');

  // 落盘（若 runtime 目录可写）
  try {
    fs.mkdirSync(RUNTIME_DIR, { recursive: true });
    fs.writeFileSync(path.join(RUNTIME_DIR, 'test-report.json'),
      JSON.stringify({ generatedAt: new Date().toISOString(), summary, access: accessTable, stats }, null, 2), 'utf8');
    fs.writeFileSync(path.join(RUNTIME_DIR, 'route-stats.md'), formatMarkdown(stats, {
      manifestPath: MANIFEST_PATH,
      generatedAt: new Date().toISOString(),
      previousHashes: ['sha256:099df023a091abaa'],
    }), 'utf8');
  } catch (e) {
    console.warn(`[route.test] 报告落盘失败（可忽略）: ${e.message}`);
  }
  assert.equal(summary.acceptance.circulationOppositeSharedM, 0);
});
