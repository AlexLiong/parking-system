// P2-17 / P2-02 / P2-01 回归测试（web/sim3d 只读逻辑，不依赖浏览器）
// 运行：node --test web/sim3d/test/repair-p2.test.js
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as THREE from 'three';
import { Vehicle } from '../js/entities/Vehicle.js';
import { Collider } from '../js/entities/Collider.js';
import { TrafficScheduler } from '../js/sim/TrafficScheduler.js';
import { RoutePlanner } from '../js/sim/RoutePlanner.js';
import { GateDisplay } from '../js/ui/GateDisplay.js';

const manifest = JSON.parse(fs.readFileSync(new URL('../../../../assets_3d/layout/lot_v2.json', import.meta.url)));

test('P2-17: 视觉尺寸从 GLB Box3 实测，OBB dims 保持既有碰撞语义', async () => {
  const assets = {
    async cloneModel() {
      const g = new THREE.Group();
      // 视觉包围盒（含后视镜）比 OBB sedan 4.6×1.8 更大
      g.add(new THREE.Mesh(new THREE.BoxGeometry(2.035, 1.58, 4.719)));
      return g;
    },
  };
  const v = new Vehicle('visual', assets, { family: 'sedan', file: 'stub.glb' });
  assert.deepEqual(v.dims, Collider.dimsOf('sedan'));
  await v.load(new THREE.Scene());
  assert.equal(v.dims.len, 4.6, 'OBB 碰撞长度不能被视觉尺寸改写');
  assert.equal(v.dims.wid, 1.8, 'OBB 碰撞宽度不能被视觉尺寸改写');
  assert.ok(v.visualLen >= 4.719 - 1e-6, `visualLen=${v.visualLen}`);
  assert.ok(v.visualWid >= 2.035 - 1e-6, `visualWid=${v.visualWid}`);
  // 未载入车型（测试 stub）用保守 per-family 兜底
  const v2 = new Vehicle('fallback', {}, { family: 'van', file: 'stub.glb' });
  assert.ok(v2.visualLen >= Collider.dimsOf('van').len);
  assert.ok(v2.visualWid >= Collider.dimsOf('van').wid);
});

test('P2-17: 入口排队/停止线按视觉长度留间隙，OBB 碰撞判定不变', () => {
  const root = new THREE.Scene();
  const scene = {
    scene: root, sceneCfg: {}, gatesCfg: {},
    gates: { in: { isOpen: false }, out: { isOpen: true } },
    assets: { async cloneModel() { return new THREE.Group(); } },
    markers: { setOccupied() {} }, staticColliders: [],
  };
  const planner = new RoutePlanner(manifest, RoutePlanner.spacesFromManifest(manifest));
  const s = new TrafficScheduler({ scene, planner, manifest, onEvent() {} });
  const vLen = 4.82, vWid = 2.06;
  // 视觉车长会增大排队起点（车心后移），且 OBB 停止线 API 不传 visualLen 时保持旧值
  const zVisual = s._queueSpawnZ(4.6, 1.0, vLen);
  const zObb = s._queueSpawnZ(4.6, 1.0, 4.6);
  assert.ok(zVisual > zObb, `visual queue ${zVisual} should be behind OBB queue ${zObb}`);
  assert.ok(s._entryStopZ(4.6, vLen) > s._entryStopZ(4.6));
  assert.ok(s._exitStopZ(4.6, vLen) < s._exitStopZ(4.6));
  // 前车视觉车尾 + gap + 本车视觉半长
  const ahead = { state: 'wait_entry', pos: { x: 0, z: 28.0 },
    dims: { len: 4.6, wid: 1.8, hgt: 1.5 }, visualLen: vLen, visualWid: vWid,
    spec: { family: 'sedan' } };
  s.vehicles.set('ahead', ahead);
  const z2 = s._queueSpawnZ(4.6, 1.0, vLen);
  assert.ok(z2 - vLen / 2 >= ahead.pos.z + vLen / 2 + 1.0 - 1e-6);
  // OBB 相交工具仍使用 dimsOf 的 4.6×1.8，而非视觉值
  assert.equal(Collider.dimsOf('sedan').len, 4.6);
  assert.equal(Collider.dimsOf('sedan').wid, 1.8);
});

test('P2-17: 停止线等待车必须作为前车，视觉重叠但 OBB 未碰时后车停车', () => {
  const root = new THREE.Scene();
  const scene = {
    scene: root, sceneCfg: {}, gatesCfg: {},
    gates: { in: { isOpen: false }, out: { isOpen: true } },
    assets: { async cloneModel() { return new THREE.Group(); } },
    markers: { setOccupied() {} }, staticColliders: [],
  };
  const planner = new RoutePlanner(manifest, RoutePlanner.spacesFromManifest(manifest));
  const s = new TrafficScheduler({ scene, planner, manifest, onEvent() {} });
  const mk = (id, z, state) => {
    const v = new Vehicle(id, {}, { family: 'sedan' });
    v.model = new THREE.Group();
    v.model.add(new THREE.Mesh(new THREE.BoxGeometry(1.8, 1.5, 4.6)));
    v.visualLen = 4.82; v.visualWid = 2.06;
    v.pos = { x: 0, z }; v.theta = 0; v.state = state; v.speed = 0; v.cruise = 0;
    v.zones = []; v.reservedZones = new Set(); v.meta = {}; v.nextZoneId = null;
    return v;
  };
  const front = mk('front', 28.0, 'wait_entry');
  // 中心距 4.8m：OBB 间隙 4.8-(2.3+2.3)=+0.2m 未碰；视觉间隙 4.8-(2.41+2.41)=-0.02m 已重叠
  const rear = mk('rear', 32.8, 'approach');
  rear.speed = 1.0; rear.cruise = 2.5;
  s.vehicles.set(front.id, front); s.vehicles.set(rear.id, rear);
  s._constrain(rear, [front, rear]);
  assert.equal(rear.cruise, 0, '视觉间隙不足时应停车（即使 OBB 未碰）');
});

test('P2-01: 闸口屏显示余位（空闲/识别/收费态），并随 space_update 刷新', () => {
  const writes = {};
  const d = new GateDisplay((gate, lines, opts) => { writes[gate] = { lines, opts }; });
  d.setFree({ total: 79, A: 20, B: 20, C: 19, D: 20 });
  d.reset();
  assert.ok(writes.in.lines.includes('余位 79'));
  assert.ok(writes.out.lines.includes('余位 79'));
  d.apply({ type: 'plate_recognized', gate: 'in', plate: '京A12345', need_manual: false });
  assert.ok(writes.in.lines.includes('余位 79'));
  assert.ok(writes.in.lines.includes('京A12345'));
  d.apply({ type: 'space_update', free: { total: 78, A: 19, B: 20, C: 19, D: 20 } });
  assert.ok(writes.in.lines.includes('余位 78'));
  assert.ok(writes.out.lines.includes('余位 78'));
  d.apply({ type: 'fee_quote', gate: 'out', plate: '京A12345', amount: 12.5, minutes: 61 });
  assert.ok(writes.out.lines.includes('停车 61 分钟'));
  assert.ok(writes.out.lines.includes('余位 78'));
  d.apply({ type: 'payment_completed', gate: 'out', plate: '京A12345', amount: 12.5 });
  assert.deepEqual(writes.out.lines, ['京A12345', '停车 61 分钟', '已缴 ¥12.50', '请通行', '余位 78']);
});

test('P2-02: fee_quote/payment_completed 出口照片经 token loader 显示并复用', async () => {
  const writes = [];
  const photo = { width: 64, height: 32, marker: 'exit-photo' };
  const d = new GateDisplay((gate, lines, opts) => writes.push({ gate, lines, opts }),
    { loadImage: async (url) => { assert.ok(url.startsWith('/media/camera/'),
        `photo url should map to /media: ${url}`); return photo; } });
  d.setFree({ total: 80 });
  d.apply({ type: 'fee_quote', gate: 'out', plate: '京A12345', minutes: 5, amount: 5,
    exit_photo: 'E:\\data\\photos\\camera\\cam_x.png' });
  await new Promise((r) => setTimeout(r, 5));
  const fee = writes.filter((w) => w.gate === 'out').at(-1);
  assert.equal(fee.opts.photo, photo);
  assert.ok(fee.lines.includes('余位 80'));
  d.apply({ type: 'payment_completed', gate: 'out', plate: '京A12345', amount: 5 });
  await new Promise((r) => setTimeout(r, 5));
  const paid = writes.filter((w) => w.gate === 'out').at(-1);
  assert.equal(paid.opts.photo, photo, 'payment_completed 应复用 fee_quote 照片');
  assert.ok(paid.lines.includes('已缴 ¥5.00'));
  assert.ok(paid.lines.includes('余位 80'));
  assert.equal(d.quotes.out.exit_photo, '/media/camera/cam_x.png');
});
