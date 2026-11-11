// 2026-10 SIM_UI 相关纯逻辑/静态回归测试：
// 手动派车策略、后端 plan 归一化、事件摘要/详情、新剧本 vehicles 格式、暂停相机、移除识别说明。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as THREE from 'three';
import {
  validatePlate, normalizePlate, normalizeDispatchPlan, durationToSeconds,
  normalizePlateType, vehicleModelFor, plateTypeToKind,
} from '../js/ui/DispatchPolicy.js';
import { normalizeEvent, eventTypeLabel, normalizeError, unwrapEvent, formatDetail } from '../js/ui/EventFormatter.js';
import { clampPanelPosition, readPanelLayout, writePanelLayout } from '../js/ui/PanelDrag.js';
import { ScenarioPlayer } from '../js/sim/ScenarioPlayer.js';
import { TrafficScheduler } from '../js/sim/TrafficScheduler.js';
import { Vehicle } from '../js/entities/Vehicle.js';

const ROOT = new URL('../../..', import.meta.url); // parking-system/
function read(rel) { return fs.readFileSync(new URL(rel, ROOT), 'utf8'); }

test('手动派车：车牌 7/8 位校验与分隔符归一化', () => {
  assert.equal(normalizePlate(' 湘N·7D071 '), '湘N7D071');
  assert.equal(validatePlate('湘N7D071').ok, true);
  assert.equal(validatePlate('沪UD76840').ok, true);
  assert.equal(validatePlate('湘N7D07').ok, false);
  assert.equal(validatePlate('A123456').ok, false);
  assert.equal(validatePlate('湘N7D07111').ok, false);
});

test('手动派车：plate_type/vehicle_kind/duration 归一化与车型映射', () => {
  const p = normalizeDispatchPlan({ plan: {
    plate: '沪UD76840', plate_type: 'green', image_url: '/media/plate.png',
    vehicle_kind: 'hatch', duration: 5, duration_unit: 'min',
  }});
  assert.equal(p.plate, '沪UD76840');
  assert.equal(p.plateType, 'green');
  assert.equal(p.kind, 'green');
  assert.equal(p.durationSec, 300);
  assert.equal(p.imageUrl, '/media/plate.png');
  assert.equal(p.model, 'car_nev_hatch_green.glb');
  assert.equal(normalizePlateType('大型新能源'), 'green_large');
  assert.equal(plateTypeToKind('yellow'), 'blue');
  assert.ok(vehicleModelFor('green_large', 'van').includes('van'));
  assert.equal(durationToSeconds(2, 'min'), 120);
  assert.equal(durationToSeconds(2.5, 'sec'), 2.5);
});

test('事件流：摘要包含类型/车牌/门/金额/原因，详情保留图片 URL', () => {
  const ev = normalizeEvent({ type: 'plate_recognized', gate: 'in', plate: '湘N7D071',
    conf: 0.98, image_url: '/media/frame.png', need_manual: false });
  assert.equal(eventTypeLabel('plate_recognized'), '车牌识别');
  assert.match(ev.summary, /入口识别/);
  assert.match(ev.summary, /湘N7D071/);
  assert.ok(ev.detail.some((d) => d.label === '图片' && d.url === '/media/frame.png'));
  const fee = normalizeEvent({ type: 'fee_quote', gate: 'out', plate: '湘N7D071',
    amount: 12.5, minutes: 90, reason: '超时' });
  assert.match(fee.summary, /¥12.50/);
  assert.ok(fee.detail.some((d) => d.label === '金额'));
  assert.ok(fee.detail.some((d) => d.label === '原因'));
  const incident = normalizeEvent({ type: 'incident', kind: 'barrier_fault', gate: 'in',
    detail: { reason: 'ack_timeout' } });
  assert.match(incident.summary, /异常/);
  assert.ok(incident.detail.some((d) => d.value === 'ack_timeout'));
  const manual = normalizeEvent({ type: 'manual_dispatch', plate: '沪UD76840',
    plate_type: 'green', duration_sec: 120, image_url: '/media/x.png' });
  assert.match(manual.summary, /手动派车/);
  assert.ok(manual.detail.some((d) => d.label === '停车时长' && d.value === '120 仿真秒'));
  assert.ok(!manual.detail.some((d) => d.label === '时长' && d.value === '120 分钟'));
});

test('新剧本 vehicles 格式：load 预取后端车牌图/vehicle_kind/车型，spawn 传图并自动出场', async () => {
  const spawned = [], images = [];
  const kindMap = { blue: 'sedan', green: 'hatch', green_large: 'van', yellow: 'van' };
  const scheduler = {
    vehicles: new Map(),
    async spawnEntry(s) {
      spawned.push(s);
      return { id: s.vid, vid: s.vid, state: 'approach', meta: {},
        setPlate() {}, setPlateImage(img, plate) { images.push({ plate, img }); } };
    },
    startExit() { return null; },
  };
  const loadedCalls = [];
  const plateLoader = async (plate, plateType) => {
    loadedCalls.push({ plate, plateType });
    return { image: { fakePlate: plate }, imageUrl: `/media/plate-${plate}.png`,
      vehicleKind: kindMap[plateType], color: plateType.includes('green') ? 'green' : (plateType === 'yellow' ? 'yellow' : 'blue') };
  };
  const p = new ScenarioPlayer({ scheduler, onEvent() {}, plateLoader });
  await p.load({ name: '新剧本', description: '两车演示', duration: 180, vehicles: [
    { vid: 'V1', t: 2, plate: '湘N7D071', plate_type: 'blue', duration: 30 },
    { vid: 'V2', t: 10, plate: '沪UD76840', plate_type: 'green_large', duration: 60 },
  ]});
  assert.equal(loadedCalls.length, 2, 'load 应为每辆车预取一次 plate-image');
  assert.equal(p.name, '新剧本');
  assert.equal(p.description, '两车演示');
  assert.equal(p.duration, 180);
  assert.equal(p.totalVehicles, 2);
  assert.equal(p.ready, true);
  assert.equal(p.steps[0].plate_image_url, '/media/plate-湘N7D071.png');
  assert.equal(p.steps[0].vehicle_kind, 'sedan');
  assert.equal(p.steps[1].vehicle_kind, 'van');
  assert.match(p.steps[1].model, /van/);
  assert.match(p.progress().nextEvent, /t=2s 派车 湘N7D071/);
  p.play(); p.update(2.1); p.update(10);
  await new Promise((r) => setImmediate(r));
  assert.equal(spawned[0].duration, 30);
  assert.equal(spawned[0].plateType, 'blue');
  assert.deepEqual(spawned[0].plateImage, { fakePlate: '湘N7D071' });
  assert.equal(spawned[1].duration, 60);
  assert.equal(spawned[1].plateType, 'green_large');
  assert.equal(spawned[1].vehicleKind, 'van');
  assert.ok(images.some((x) => x.plate === '沪UD76840'));
  p.markVehicleLeft('V1');
  assert.equal(p.leftVehicles, 1);
  assert.equal(p.completedVehicles, 2);
  assert.equal(p.progress().completedVehicles, 2);
  p.update(200);
  await new Promise((r) => setImmediate(r));
  assert.ok(p.finished);
  assert.equal(p.playing, false, '完成后播放状态应停止，让重播按钮生效');
});

test('新剧本默认 6 车：逐车预取后端车牌图，blue/green_large/yellow 车型不被压成同一种', async () => {
  const calls = [];
  const kindMap = { blue: 'sedan', green: 'hatch', green_large: 'van', yellow: 'van' };
  const plateLoader = async (plate, plateType) => {
    calls.push({ plate, plateType });
    return { image: { fakePlate: plate }, imageUrl: `/media/dispatch-${plate}.png`,
      vehicleKind: kindMap[plateType], color: plateType.includes('green') ? 'green' : (plateType === 'yellow' ? 'yellow' : 'blue') };
  };
  const scheduler = { vehicles: new Map(), async spawnEntry(s) { return null; }, startExit() { return null; } };
  const p = new ScenarioPlayer({ scheduler, onEvent() {}, plateLoader });
  await p.load({ name: '默认6车', duration: 2640, vehicles: [
    { vid: 'E1', t: 1, plate: '粤C4K1CZ', plate_type: 'blue', duration: 600 },
    { vid: 'E2', t: 4, plate: '甘ADMYYNR', plate_type: 'green', duration: 1800 },
    { vid: 'E3', t: 90, plate: '鄂ABWXTXHF', plate_type: 'green_large', duration: 1200 },
    { vid: 'E4', t: 120, plate: '皖MJ42S3', plate_type: 'yellow', duration: 2400 },
    { vid: 'E5', t: 180, plate: '蒙EYA8H5', plate_type: 'blue', duration: 960 },
    { vid: 'E6', t: 240, plate: '皖FDKLV32', plate_type: 'green', duration: 300 },
  ]});
  assert.equal(calls.length, 6);
  assert.equal(p.ready, true);
  assert.equal(p.steps.length, 6);
  assert.ok(p.steps.every((s) => s.plate_image_url && s.plate_image));
  assert.equal(p.steps[2].vehicle_kind, 'van');
  assert.match(p.steps[2].model, /van/);
  assert.equal(p.steps[3].vehicle_kind, 'van');
  assert.equal(p.steps[3].plate_color, 'yellow');
  assert.equal(p.steps[1].model, 'car_nev_hatch_green.glb');
});

test('新剧本车牌图预取失败：ready=false、播放被拒绝、HUD 可见错误（不静默 Canvas）', async () => {
  const scheduler = { vehicles: new Map(), async spawnEntry() { return null; }, startExit() { return null; } };
  const p = new ScenarioPlayer({ scheduler, onEvent() {}, plateLoader: async () => { throw new Error('HTTP 401'); } });
  await p.load({ name: 'fail', duration: 5, vehicles: [
    { vid: 'F1', t: 0, plate: '湘N7D071', plate_type: 'blue', duration: 5 },
  ]});
  assert.equal(p.ready, false);
  assert.match(p.preloadError, /401/);
  assert.equal(p.play(), false);
  assert.equal(p.playing, false);
  assert.ok(p.steps[0].plate_error);
  assert.equal(p.progress().preloadLoaded, 0);
});

test('暂停仍允许拖拽：main.js 不得在暂停时禁用 OrbitControls', () => {
  const main = read('web/sim3d/js/main.js');
  assert.match(main, /controls\.enabled\s*=\s*true/);
  assert.doesNotMatch(main, /controls\.enabled\s*=\s*!paused/);
});

test('手动派车 duration 在入库后按仿真时间到点自动 startExit', () => {
  const calls = [];
  const s = Object.create(TrafficScheduler.prototype);
  s.simTime = 100;
  s.startExit = (id) => { calls.push(id); return true; };
  s._emit = (e) => calls.push(e.type);
  const v = { id: 'M1', vid: 'M1', state: 'parked', meta: { autoExitAt: 105, duration: 5 } };
  assert.equal(s._maybeAutoExit(v), false); // 未到期
  s.simTime = 105;
  assert.equal(s._maybeAutoExit(v), true);  // 到期自动出场
  assert.equal(v.meta.autoExitAt, null);
  assert.ok(calls.includes('M1'));
  assert.ok(calls.includes('auto_exit'));
});

test('识别输入说明文字已移除，场景进度/手动派车/事件详情 DOM 已加入', () => {
  const html = read('web/sim3d/index.html');
  assert.doesNotMatch(html, /recog-note/);
  assert.doesNotMatch(html, /识别输入 = 3D 仿真闸口相机截图/);
  assert.match(html, /id="scenario-progress"/);
  assert.match(html, /id="manual-plate"/);
  assert.match(html, /id="manual-type"/);
  assert.match(html, /id="manual-duration"/);
  assert.match(html, /id="btn-dispatch"/);
  assert.match(html, /id="events"/);
});

test('3D 车牌面 UV 纵向翻转：修复相机帧中车牌倒置导致的 expected mismatch', async () => {
  const assets = {
    cloneModel() {
      const g = new THREE.Group();
      const node = new THREE.Mesh(new THREE.BoxGeometry(0.44, 0.14, 0.01), new THREE.MeshBasicMaterial());
      node.name = 'Plate_F';
      g.add(node);
      return g;
    },
  };
  const v = new Vehicle('uv-plate', assets, { family: 'sedan', file: 'stub.glb' });
  await v.load(new THREE.Scene());
  assert.equal(v.plateMeshes.length, 1);
  const uv = v.plateMeshes[0].geometry.attributes.uv;
  // PlaneGeometry 默认 UV: (0,1),(1,1),(0,0),(1,0)；修复后 V 轴翻转为正向。
  assert.equal(uv.getY(0), 0);
  assert.equal(uv.getY(2), 1);
});

test('后端车牌图优先：入口识别后的 setPlate 不得覆盖 setPlateImage', async () => {
  const assets = {
    cloneModel() {
      const g = new THREE.Group();
      const node = new THREE.Mesh(new THREE.BoxGeometry(0.44, 0.14, 0.01), new THREE.MeshBasicMaterial());
      node.name = 'Plate_F';
      g.add(node);
      return g;
    },
    plateTexture() { throw new Error('后端图存在时不应回退 plateTexture'); },
  };
  const v = new Vehicle('img-priority', assets, { family: 'sedan', file: 'stub.glb' });
  await v.load(new THREE.Scene());
  v.setPlateImage({ width: 440, height: 140, fake: true }, '皖FDKLV32', 'green');
  const map = v._plateMaterial.map;
  v.setPlate('皖FDKLV32', 'green');
  assert.equal(v._plateMaterial.map, map, 'setPlate 不能重置后端渲染图');
  assert.equal(v.meta.plate, '皖FDKLV32');
});

test('新格式 vehicles 同时下发 steps：保留 pay/safety_probe 等非 spawn/exit 动作', async () => {
  const scheduler = { vehicles: new Map(), async spawnEntry() { return null; }, startExit() { return null; } };
  const p = new ScenarioPlayer({
    scheduler,
    onEvent() {},
    plateLoader: async (plate) => ({ image: { fakePlate: plate }, imageUrl: `/media/plate-${plate}.png`,
      vehicleKind: 'sedan' }),
  });
  await p.load({
    name: 'vehicles+steps',
    vehicles: [{ vid: 'E1', t: 1, plate: '粤C4K1CZ', plate_type: 'blue', duration: 10 }],
    steps: [
      { t: 10, action: 'pay', vid: 'E1', plate: '粤C4K1CZ' },
      { t: 11, action: 'exit', vid: 'E1' },
      { t: 12, action: 'safety_probe' },
      { t: 13, action: 'spawn', vid: 'E1', plate: '粤C4K1CZ' }, // 不应重复 spawn
    ],
  });
  const actions = p.steps.map((s) => s.action);
  assert.deepEqual(actions.slice().sort(), ['pay', 'safety_probe', 'spawn']);
  assert.equal(p.steps.filter((s) => s.action === 'spawn').length, 1, 'steps 中的重复 spawn 应丢弃');
  assert.equal(p.totalVehicles, 1);
});

test('事件/错误规范化：解包 data、未知类型 fallback、incident 详情非裸 JSON、错误统一口径', () => {
  const ev = normalizeEvent({ type: 'plate_recognized', data: { plate: '粤C4K1CZ', gate: 'in', conf: 0.99 } });
  assert.equal(ev.plate, '粤C4K1CZ');
  assert.match(ev.summary, /入口识别 粤C4K1CZ/);
  const unk = normalizeEvent({ type: 'brand_new_event', data: { foo: 1 } });
  assert.match(unk.label, /事件\(brand_new_event\)/);
  assert.ok(unk.summary.length > 0);
  assert.ok(unk.detail.some((d) => d.label === '事件类型'));
  const inc = normalizeEvent({ type: 'incident', detail: { reason: 'blacklist', plate: '粤C4K1CZ' } });
  const incDetail = inc.detail.find((d) => d.label === '详情');
  assert.ok(incDetail && incDetail.value.includes('reason=blacklist'));
  assert.doesNotMatch(incDetail.value, /^\s*\{/);
  assert.deepEqual(unwrapEvent({ type: 'x', data: { a: 1 } }), { type: 'x', a: 1 });
  assert.match(formatDetail([{ loc: ['body', 'plate'], msg: 'invalid' }]), /body.plate: invalid/);

  const err = normalizeError({ error: { code: 'blacklist', message: '黑名单车辆' }, plate: '粤C4K1CZ' });
  assert.deepEqual([err.code, err.message, err.plate], ['blacklist', '黑名单车辆', '粤C4K1CZ']);
  const axiosLike = new Error('409 /api/sim/dispatch-plan: {"code":"duplicate_entry","message":"已有记录","detail":[{"loc":["plate"],"msg":"重复"}]}');
  axiosLike.status = 409;
  const err2 = normalizeError(axiosLike);
  assert.equal(err2.code, 'duplicate_entry');
  assert.match(err2.detail, /plate: 重复/);
});

test('面板位置：clamp 边界 + 布局持久化', () => {
  const p = clampPanelPosition(9999, 9999, 260, 120, { width: 800, height: 600 });
  assert.ok(p.left >= 6 && p.left + 260 <= 800 - 6);
  assert.ok(p.top >= 46 && p.top + 120 <= 600 - 6);
  assert.deepEqual(clampPanelPosition(-20, 0, 100, 50, { width: 800, height: 600 }), { left: 6, top: 46 });
  const store = new Map();
  const fake = { getItem: (k) => store.get(k) || null, setItem: (k, v) => store.set(k, v) };
  assert.equal(writePanelLayout(fake, { hud: { left: 12, top: 60 } }, 'k'), true);
  assert.deepEqual(readPanelLayout(fake, 'k'), { hud: { left: 12, top: 60 } });
});

test('DOM/速度/模态契约：独立 messagebox、速度 0.25–10、无旧内联字段', () => {
  const html = read('web/sim3d/index.html');
  assert.match(html, /id="manual-modal"/);
  assert.match(html, /id="btn-open-dispatch"/);
  assert.match(html, /id="btn-cancel-dispatch"/);
  assert.match(html, /id="btn-dispatch"/);
  assert.match(html, /id="manual-plate"/);
  assert.doesNotMatch(html, /manual-title|manual-hint/);
  assert.match(html, /id="speed"[^>]*min="0\.25"[^>]*max="10"/);
  const main = read('web/sim3d/js/main.js');
  assert.match(main, /Math\.min\(10, Number\(value\)/);
  assert.match(main, /Math\.max\(\.25, Math\.min\(10/);
  assert.match(main, /unknown|未知 WS/);
  assert.doesNotMatch(main, /switch \(m\.type\) \{[\s\S]{0,5000}default: break;/);
});

test('剧本 t 唯一时间轴 + t+duration 预告 + 已入场/已出场进度', async () => {
  const scheduler = { vehicles: new Map(), async spawnEntry() { return null; }, startExit() { return null; } };
  const p = new ScenarioPlayer({
    scheduler,
    onEvent() {},
    plateLoader: async (plate) => ({ image: { fakePlate: plate }, imageUrl: `/media/${plate}.png`, vehicleKind: 'sedan' }),
  });
  await p.load({
    name: 'forecast', entry_transit_est: 4, vehicles: [
      { vid: 'V1', t: 10, plate: '粤C4K1CZ', plate_type: 'blue', duration: 20 },
      { vid: 'V2', t: 5, plate: '湘N7D071', plate_type: 'blue', duration: 5 },
    ],
    steps: [
      { t: 10, action: 'pay', vid: 'V1', order: 99 },
      { t: 11, action: 'exit', vid: 'V1', order: 0 },
    ],
  });
  assert.equal(p.steps[0].vid, 'V2', 't 为唯一时间轴，不能被子步骤 order 打乱');
  assert.equal(p.steps[0].t, 5);
  assert.equal(p.duration, 34, '新格式总时长 = max(t + 入场估计 + duration)');
  assert.deepEqual(p.forecast.map((f) => f.t), [14, 34]);
  assert.match(p.progress().nextForecast, /14s/);
  assert.match(p.progress().nextForecast, /预计，不含排队\/故障/);
  assert.equal(p.enteredVehicles, 0);
  p.markVehicleEntered('V1');
  assert.equal(p.enteredVehicles, 1);
  p.markVehicleLeft('V1');
  assert.equal(p.leftVehicles, 1);
  assert.equal(p.progress().enteredVehicles, 1);
});

test('入口拒绝：无后车倒车离场/有后车前行再出场/不建记录/授权恢复', () => {
  const s = Object.create(TrafficScheduler.prototype);
  const events = [];
  const fake = (id, z, state = 'wait_entry') => ({
    id, vid: id, remote: false, state, pos: { x: -3.5, z }, dims: { len: 4.4 },
    visualLen: 4.4, meta: { plate: id }, reservedZones: new Set(), zoneWindows: new Map(),
    setLegs(legs) { this.legs = legs; },
  });
  Object.assign(s, {
    entry: { x: -3.5 }, exit: { x: 3.5 }, outerSouthZ: 40, outerZ: 45, simTime: 1,
    vehicles: new Map(), reservations: null, _emit: (e) => events.push(e),
    _releaseZone: () => {}, _queuedIds: new Set(), _visualLen: () => 4.4,
    _entryStopZ: () => 25.8, _requestDeniedGate: () => {},
    _entryApproachLegs: () => [{ kind: 'line', ax: -3.5, az: 20, bx: -3.5, bz: 25.8, reverse: false }],
  });
  const a = fake('A', 28);
  s.vehicles.set('A', a);
  s.onEntryDenied({ vehicle_id: 'A', plate: 'A', reason: 'blacklist' });
  assert.equal(a.state, 'DENIED_REVERSE_WAIT_ENTRY_GATE');
  assert.equal(a.meta.noRecord, true);
  assert.equal(a.meta.deniedMode, 'reverse_out');
  assert.ok(events.some((e) => e.type === 'entry_denied' && e.no_record === true));

  const b = fake('B', 28);
  const rear = fake('R', 34);
  s.vehicles = new Map([['B', b], ['R', rear]]);
  s.onEntryDenied({ vehicle_id: 'B', plate: 'B', reason: 'duplicate_entry' });
  assert.equal(b.state, 'DENIED_BYPASS_WAIT_ENTRY_GATE');
  assert.equal(b.meta.deniedMode, 'forward_then_exit');

  s.onEntryAuthorized({ vehicle_id: 'B', plate: 'B' });
  assert.equal(b.state, 'approach');
  assert.equal(b.meta.denied, false);
  assert.ok(events.some((e) => e.type === 'entry_authorized'));

  const e = fake('E', 31, 'wait_exit');
  s.vehicles.set('E', e);
  s.onExitAuthorized({ vehicle_id: 'E', plate: 'E', record_id: 'r1' });
  assert.equal(e.meta.exitAuthorized, true);
  assert.equal(e.meta.recordId, 'r1');
});

test('实现B：denied 闸杆路径/统一通行条件/重置/出口摄像头/下一事件合并', () => {
  const gate = read('web/sim3d/js/entities/BarrierGate.js');
  assert.match(gate, /get isPassable\(\)/);
  assert.match(gate, /setPassable\(/);
  assert.match(gate, /manualOpen\(\)/);
  assert.match(gate, /MANUAL_OPEN/);
  const main = read('web/sim3d/js/main.js');
  assert.match(main, /setPassable\?\./);
  assert.match(main, /passable: m\.passable/);
  assert.match(main, /resetLocalDemo/);
  assert.match(main, /forceState\?\.\('CLOSED'/);
  const scheduler = read('web/sim3d/js/sim/TrafficScheduler.js');
  assert.match(scheduler, /_gatePassable/);
  assert.match(scheduler, /_requestDeniedGate/);
  assert.match(scheduler, /DENIED_REVERSE_WAIT_ENTRY_GATE/);
  assert.match(scheduler, /DENIED_BYPASS_WAIT_ENTRY_GATE/);
  assert.match(scheduler, /DENIED_BYPASS_WAIT_EXIT_GATE/);
  assert.match(scheduler, /this\._gatePassable\('in'\)/);
  assert.match(scheduler, /this\._gatePassable\('out'\)/);
  const api = read('web/sim3d/js/api.js');
  assert.match(api, /blacklistGate/);
  assert.match(api, /\/api\/sim\/blacklist-gate/);
  assert.match(api, /force: !!force/);
  const simApi = read('web/sim3d/js/sim/SimApi.js');
  assert.match(simApi, /resetDemo\(force = true\)/);
  const index = read('web/sim3d/index.html');
  assert.match(index, /⟲ 重置/);
  assert.match(index, /btn-cam-fault-out/);
  assert.match(index, /btn-cam-recover-out/);
  assert.doesNotMatch(index, /id="scn-forecast"/);
  const hud = read('web/sim3d/js/ui/HudOverlay.js');
  assert.match(hud, /scn-next-event/);
  assert.match(hud, /scn-next-forecast/);
  assert.match(hud, /预计出场\/缴费/);
  const s = Object.create(TrafficScheduler.prototype);
  s.scene = { gates: { in: { isPassable: true, isOpen: false }, out: { isPassable: false, isOpen: true } } };
  assert.equal(s._gatePassable('in'), true);
  assert.equal(s._gatePassable('out'), false);
});

test('黑名单后车恢复：按 retried_vehicle_ids 清 needManual，未重试 wait_entry 后车排程重新抓拍', () => {
  const s = Object.create(TrafficScheduler.prototype);
  const events = [];
  Object.assign(s, { vehicles: new Map(), _emit: (e) => events.push(e), _reportArrival: async () => null });
  const denied = { id: 'D', vid: 'D', remote: false, state: 'gone', meta: { plate: '京A00001', denied: true } };
  const follower = { id: 'R', vid: 'R', remote: false, state: 'wait_entry', meta: { plate: '京A00002', needManual: true } };
  const fresh = { id: 'N', vid: 'N', remote: false, state: 'wait_entry', meta: { plate: '京A00003', needManual: false } };
  s.vehicles.set('R', follower); s.vehicles.set('N', fresh);
  s._applyBlacklistRetryResult(denied, { retried_vehicle_ids: ['R'], authorized: 1, failed: 0 });
  assert.equal(follower.meta.needManual, false);
  assert.equal(follower.meta.backendBlacklistRetry, true);
  assert.ok(events.some((e) => e.type === 'blacklist_retry_authorized' && e.id === 'R'));
  // N 不在后端 pending 重试列表，清场后应排程新抓拍；这里只断言排程标记（不等待真实网络）。
  s._scheduleFollowerArrivalRetry(denied, { retried_vehicle_ids: ['R'], authorized: 1, failed: 0 });
  assert.equal(fresh.meta._arrivalRetryScheduled, true);
});

test('N1 任意跟随：下拉保留选择/任选车辆/离场保持最后位置', () => {
  const index = read('web/sim3d/index.html');
  assert.match(index, /id="follow-vehicle"/);
  const cp = read('web/sim3d/js/ui/ControlPanel.js');
  assert.match(cp, /_syncFollowOptions/);
  assert.match(cp, /setFollowTarget/);
  assert.match(cp, /目标车辆已离场/);
  assert.match(cp, /this\._followId/);
  const rig = read('web/sim3d/js/scene/CameraRig.js');
  assert.match(rig, /holdFollowPosition/);
  assert.match(rig, /_followHold/);
});

test('重复车牌前置拒绝：spawnEntry 直接 409 duplicate_plate', async () => {
  const s = Object.create(TrafficScheduler.prototype);
  Object.assign(s, {
    options: {}, seed: 1, _seq: 0, _claimPromise: null, _spawnRequests: [], _queuedIds: new Set(),
    vehicles: new Map([['old', { remote: false, meta: { plate: '粤C4K1CZ' } }]]),
    _emit: () => {},
  });
  await assert.rejects(s.spawnEntry({ vehicleId: 'new', plate: '粤C4K1CZ' }),
    (e) => e.code === 'duplicate_plate' && e.status === 409);
});

test('最终 WS 契约：blacklist-resolution / manual_authorized(gate) / DEMO_RESET', () => {
  const api = read('web/sim3d/js/api.js');
  assert.match(api, /blacklistResolution/);
  assert.match(api, /\/api\/sim\/blacklist-resolution/);
  const scheduler = read('web/sim3d/js/sim/TrafficScheduler.js');
  assert.match(scheduler, /_reportBlacklistResolution/);
  assert.match(scheduler, /_scheduleFollowerArrivalRetry/);
  assert.match(scheduler, /_reportArrival\(cur, 'in'\)/);
  assert.match(scheduler, /retried_vehicle_ids/);
  assert.match(scheduler, /expected_authorized/);
  assert.match(scheduler, /onArrivalRetryRequested/);
  assert.match(scheduler, /'reversed'/);
  assert.match(scheduler, /'entered_then_left'/);
  assert.match(scheduler, /no_record: true/);
  const main = read('web/sim3d/js/main.js');
  assert.match(main, /case 'manual_authorized'/);
  assert.match(main, /case 'arrival_retry_requested'/);
  assert.match(main, /m\.gate === 'in'/);
  assert.match(main, /case 'demo_reset'/);
  assert.match(main, /onSnapshot\(payload\.snapshot\)/);
  assert.match(main, /m\.name/);
  assert.match(main, /speed_min|speed_max/);
});

test('duration 新语义：入口过闸→出口总时长，autoExitAt 反推 startExit', () => {
  const s = Object.create(TrafficScheduler.prototype);
  const v = { meta: { duration: 20, entryGateAtSim: 100, parkedAtSim: 110 }, dims: { len: 4.6 } };
  s.simTime = 115;
  const r = s._computeExitSchedule(v, 115);
  assert.equal(r.entryTravel, 10);
  assert.equal(r.exitTravelEst, 10);
  assert.equal(r.scheduledExitAt, 120);
  assert.equal(r.autoExitAt, 115, 'now 已超过 scheduled-exitEst 时立即 startExit');
  const r2 = s._computeExitSchedule({ ...v, meta: { ...v.meta, parkedAtSim: 106 } }, 102);
  assert.equal(r2.entryTravel, 6);
  assert.equal(r2.exitTravelEst, 6);
  assert.equal(r2.autoExitAt, 114); // 120-6
});

test('暂停按钮：never-played 恢复不启动剧本，finished 也可暂停/继续', () => {
  const cp = read('web/sim3d/js/ui/ControlPanel.js');
  assert.match(cp, /pause\.disabled = false/);
  assert.doesNotMatch(cp, /pause\.disabled = finished\s*&&\s*!paused/);
  assert.match(cp, /pause\.textContent = paused \? '▶ 继续' : '⏸ 暂停'/);
  assert.match(cp, /_resumeScenarioPlaying = !!scenario\?\.playing/);
  assert.match(cp, /if \(this\._resumeScenarioPlaying\) scenario\?\.play\?\.\(\)/);
  const main = read('web/sim3d/js/main.js');
  assert.match(main, /getPaused: \(\) => paused/);
});

test('自动扣费：PaymentCompleted 设置 exitAuthorized，wait_exit 无需剧本 pay 即可走', () => {
  const s = Object.create(TrafficScheduler.prototype);
  const v = { id: 'P', vid: 'P', remote: false, state: 'wait_exit',
    meta: { plate: 'P', recordId: null, exitAuthorized: false }, dims: { len: 4.4 } };
  s.vehicles = new Map([['P', v]]);
  s._emit = () => {};
  s.onPaymentCompleted({ record_id: 'r1', plate: 'P', amount_paid: 5 });
  assert.equal(v.meta.exitAuthorized, true);
  assert.equal(v.meta.recordId, 'r1');
  const src = read('web/sim3d/js/sim/TrafficScheduler.js');
  assert.match(src, /case 'wait_exit'[\s\S]{0,220}exitAuthorized[\s\S]{0,160}_gatePassable\('out'\)/);
});

test('P3 下一事件两行：下一事件/预计出场缴费分行，允许换行不截断', () => {
  const index = read('web/sim3d/index.html');
  assert.match(index, /id="scn-next-event"/);
  assert.match(index, /id="scn-next-forecast"/);
  assert.match(index, /下一事件：—/);
  assert.match(index, /预计出场\/缴费：—/);
  const css = read('web/sim3d/styles.css');
  assert.match(css, /\.sp-next\s*\{[^}]*white-space:\s*normal/);
  assert.match(css, /\.sp-next-line\s*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(css, /#scenario-progress\.panel\s*\{[^}]*width:\s*340px/);
  assert.doesNotMatch(css, /\.sp-next\s*\{[^}]*text-overflow:\s*ellipsis/);
  const hud = read('web/sim3d/js/ui/HudOverlay.js');
  assert.match(hud, /set\('scn-next-event'/);
  assert.match(hud, /set\('scn-next-forecast'/);
  assert.doesNotMatch(hud, /scn-next',/);
});

test('新外部模板 18 牌：index.json 与 PLATE_IMAGES 文件/类型桶一致', () => {
  const index = JSON.parse(read('web/sim3d/assets/plates/index.json'));
  assert.equal(Object.keys(index.plates).length, 18);
  for (const [plate, entry] of Object.entries(index.plates)) {
    assert.ok(fs.existsSync(new URL(`web/sim3d/assets/${entry.file}`, ROOT)), `${plate} 缺贴图 ${entry.file}`);
    assert.ok(['blue', 'green', 'green_large', 'yellow'].includes(entry.plate_type));
    assert.ok(['sedan', 'hatch', 'van'].includes(entry.vehicle_kind));
  }
  const config = read('web/sim3d/js/config.js');
  const paths = [...config.matchAll(/datasets\/demo-lpr\/([A-Za-z0-9_-]+\.png)/g)].map((m) => m[1]);
  assert.ok(paths.length >= 18);
  for (const f of paths) assert.ok(fs.existsSync(new URL(`datasets/demo-lpr/${f}`, ROOT)), `PLATE_IMAGES 缺文件 ${f}`);
  assert.doesNotMatch(config, /blue-005|green_car-005|green_truck/);
  assert.match(config, /"green_large":/);
  assert.match(config, /"yellow":/);
});




