// R-04 视觉边界偏移回归测试（只改视觉 model.position；OBB/路线/车位中心不变）
// 运行：node --test web/sim3d/test/repair-bc.test.js
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as THREE from 'three';
import { Vehicle } from '../js/entities/Vehicle.js';
import { Collider } from '../js/entities/Collider.js';
import { TrafficScheduler } from '../js/sim/TrafficScheduler.js';
import { RoutePlanner } from '../js/sim/RoutePlanner.js';

const manifest = JSON.parse(fs.readFileSync(new URL('../../../../assets_3d/layout/lot_v2.json', import.meta.url)));

function scheduler() {
  const scene = {
    scene: new THREE.Scene(), sceneCfg: {}, gatesCfg: {},
    gates: { in: { isOpen: false }, out: { isOpen: true } },
    assets: { async cloneModel() { return new THREE.Group(); } },
    markers: { setOccupied() {} }, staticColliders: [],
  };
  const planner = new RoutePlanner(manifest, RoutePlanner.spacesFromManifest(manifest));
  return new TrafficScheduler({ scene, planner, manifest, onEvent() {} });
}

test('R-04: 视觉偏移只移动 model.position，pos/poseBox/dims 不变', async () => {
  const assets = {
    async cloneModel() {
      const g = new THREE.Group();
      g.add(new THREE.Mesh(new THREE.BoxGeometry(1.8, 1.5, 4.6)));
      return g;
    },
  };
  const v = new Vehicle('r04', assets, { family: 'sedan', file: 'stub.glb' });
  await v.load(new THREE.Scene());
  v.placeAt(-34.5, 0, Math.PI);
  const pos0 = { ...v.pos };
  const pose0 = v.poseBox().clone();
  const dims0 = { ...v.dims };
  v.setVisualOffset(0.12, 0);
  assert.equal(v.pos.x, pos0.x);
  assert.equal(v.pos.z, pos0.z);
  assert.deepEqual(v.dims, dims0);
  assert.equal(v.model.position.x, -34.5 + 0.12);
  assert.ok(v.poseBox().equals(pose0), 'OBB poseBox 不得被视觉偏移改变');
  assert.ok(v.box.min.x > -35.85, '视觉模型应仍位于墙内缘内侧');
  v.setVisualOffset(0, 0);
  assert.equal(v.model.position.x, -34.5);
});

test('R-04: 8 个贴墙边界车位识别为 ±0.12m，内部车位不偏移', () => {
  const s = scheduler();
  const boundary = {
    'A-01': +0.12, 'A-11': +0.12, 'B-01': +0.12, 'B-11': +0.12,
    'C-10': -0.12, 'C-20': -0.12, 'D-10': -0.12, 'D-20': -0.12,
  };
  for (const [no, expected] of Object.entries(boundary)) {
    assert.equal(s._boundaryVisualShift(no), expected, no);
  }
  assert.equal(s._boundaryVisualShift('A-02'), 0);
  assert.equal(s._boundaryVisualShift('A-10'), 0);
  assert.equal(s._boundaryVisualShift('C-01'), 0);
  assert.equal(s._boundaryVisualShift('D-09'), 0);
});

test('R-04: parked 边界应用视觉偏移，非 parked/出场自动归零', async () => {
  const s = scheduler();
  const assets = { async cloneModel() { return new THREE.Group(); } };
  const v = new Vehicle('r04b', assets, { family: 'sedan', file: 'stub.glb' });
  await v.load(s.scene.scene);
  v.meta = { spaceNo: 'A-01' };
  v.state = 'parked';
  assert.equal(s._applyParkedVisualOffset(v), 0.12);
  assert.equal(v.visualOffset.x, 0.12);
  // 最后倒车/入库扫掠靠近边界位时同样偏移（视觉-only，不改 pos/OBB）
  v.state = 'to_space';
  v.placeAt(-34.0, -25.0, Math.PI);
  assert.equal(s._syncBoundaryVisualOffset(v), 0.12);
  assert.equal(v.pos.x, -34.0);
  // 驶离边界位足够远后归零
  v.state = 'to_exit';
  v.placeAt(0, 0, 0);
  assert.equal(s._syncBoundaryVisualOffset(v), 0);
  assert.equal(v.visualOffset.x, 0);
  assert.deepEqual(v.pos, { x: 0, z: 0 });
  // OBB dims 仍为碰撞尺寸
  assert.deepEqual(v.dims, Collider.dimsOf('sedan'));
});
