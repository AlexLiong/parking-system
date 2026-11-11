// Node 冒烟：演示剧本纯数据模型（不依赖 DOM）。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  normalizeScenario, validateScenario, emptyVehicle, TYPE_VALUES, sortVehicles, expectedTimes,
} from '../js/scenario-model.js';

const base = () => ({
  name: 'Demo', description: 'd', seed: 7,
  vehicles: [
    { vid: 'E1', t: 0, plate: '湘N7D071', plate_type: 'blue', duration: 20 },
    { vid: 'E2', t: 10, plate: '沪UD76840', plate_type: 'green', duration: 15 },
  ],
});

test('scenario model normalizes backend wrapper and aliases', () => {
  const s = normalizeScenario({ scenario: { name: 'X', seed: '3', vehicles: [
    { vehicle_id: 'A', t: '2', plate: '湘N7D071', type: 'yellow', duration: '9' },
  ] } });
  assert.equal(s.name, 'X');
  assert.equal(s.seed, 3);
  assert.equal(s.vehicles[0].vid, 'A');
  assert.equal(s.vehicles[0].plate_type, 'yellow');
  assert.equal(s.vehicles[0].duration, 9);
});

test('valid scenario passes; types include four values', () => {
  assert.deepEqual(TYPE_VALUES.sort(), ['blue', 'green', 'green_large', 'yellow'].sort());
  assert.equal(validateScenario(base()), null);
});

test('duplicate vid rejected', () => {
  const s = base();
  s.vehicles[1].vid = 'E1';
  const p = validateScenario(s);
  assert.equal(p.field, 'vid-1');
  assert.match(p.message, /vid 重复/);
});

test('invalid plate/time/duration rejected', () => {
  const badPlate = base(); badPlate.vehicles[0].plate = 'ABC';
  assert.equal(validateScenario(badPlate).field, 'plate-0');
  const badTime = base(); badTime.vehicles[1].t = -1;
  assert.equal(validateScenario(badTime).field, 't-1');
  const badDuration = base(); badDuration.vehicles[0].duration = 0;
  assert.equal(validateScenario(badDuration).field, 'duration-0');
});

test('empty vehicle and empty scenario rejected', () => {
  const e = emptyVehicle(2);
  assert.equal(e.vid, 'E3');
  assert.equal(validateScenario({ name: 'X', seed: 1, vehicles: [] }).field, 'vehicles');
});

test('stable t ordering and expected timeline', () => {
  const a = { vid: 'A', t: 20, duration: 10, plate: '湘N7D071' };
  const b = { vid: 'B', t: 5, duration: 30, plate: '沪UD76840' };
  const c = { vid: 'C', t: 20, duration: 5, plate: '京A12345' };
  const sorted = sortVehicles([a, b, c]);
  assert.deepEqual(sorted.map((v) => v.vid), ['B', 'A', 'C']);
  assert.deepEqual(expectedTimes(a), { entry: 20, exit: 30, pay: 30 });
  const normalized = normalizeScenario({ vehicles: [a, b, c] });
  assert.deepEqual(normalized.vehicles.map((v) => v.vid), ['B', 'A', 'C']);
});

test('duplicate plate overlapping window rejected, disjoint allowed', () => {
  const s = base();
  s.vehicles[1].plate = s.vehicles[0].plate;
  s.vehicles[1].plate_type = s.vehicles[0].plate_type;
  s.vehicles[1].t = 10; // [10,25) overlaps [0,20)
  const p = validateScenario(s);
  assert.equal(p.field, 'plate-1');
  assert.match(p.message, /时间窗重叠/);
  const ok = base();
  ok.vehicles[1].plate = ok.vehicles[0].plate;
  ok.vehicles[1].plate_type = ok.vehicles[0].plate_type;
  ok.vehicles[1].t = 20; // 与 [0,20) 边界相接，不重叠
  assert.equal(validateScenario(ok), null);
});
