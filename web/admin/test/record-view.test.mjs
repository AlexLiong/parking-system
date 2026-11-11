// Node 冒烟：记录详情固定计费字段/支付方式/历史费率/未知字段折叠。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  paymentMethodLabel, billingRows, rateVersionInfo,
} from '../js/record-view.js';

test('payment methods are translated', () => {
  assert.equal(paymentMethodLabel('auto'), '自动扣费');
  assert.equal(paymentMethodLabel('mock_scan'), '扫码');
  assert.equal(paymentMethodLabel('cash'), '现金');
  assert.equal(paymentMethodLabel('free'), '免费');
  assert.equal(paymentMethodLabel('monthly'), '月租');
  assert.equal(paymentMethodLabel('unknown_x'), 'unknown_x');
});

test('billing rows use fixed Chinese labels and order', () => {
  const r = billingRows({
    minutes: 33, free_minutes_applied: 15, billable_minutes: 18,
    first_hour_price: 5, unit_minutes: 30, unit_price: 2,
    billable_units: 0, daily_cap_applied: false, new_energy_discount: 0.9,
    amount_due: 5, detail: '首小时计费',
  });
  assert.deepEqual(r.rows.map((x) => x.label), [
    '停放时长', '免费时长', '计费时长', '首小时', '续时单位',
    '续时单价', '续时单位数', '单日封顶', '新能源折扣', '应收',
  ]);
  assert.equal(r.rows[0].value, '33 分钟');
  assert.equal(r.rows[7].value, '未封顶');
  assert.equal(r.rows[8].value, '9.0 折');
  assert.equal(r.rows[9].value, '¥5.00');
  assert.equal(r.detail, '首小时计费');
  const withRule = billingRows({ amount_due: 0 }, {}, { unit_minutes: 30 });
  assert.equal(withRule.rows.find((x) => x.label === '续时单位').value, '30 分钟');
});

test('unknown fields are flattened and not raw JSON', () => {
  const r = billingRows({ minutes: 1, weird: { nested: 3 }, list: [1, 2] });
  assert.deepEqual(r.unknown, [['list[0]', '1'], ['list[1]', '2'], ['weird.nested', '3']]);
});

test('current vs historical fee rule annotation', () => {
  assert.equal(rateVersionInfo({ id: 2 }, { id: 2 }).note, '');
  const old = rateVersionInfo({ id: 1, effective_from: '2026-01-01' }, { id: 2 });
  assert.equal(old.version, '#1');
  assert.equal(old.differs, true);
  assert.equal(old.note, '历史费率（与当前不同）');
  assert.equal(rateVersionInfo(null, { id: 2 }).version, '—');
});
