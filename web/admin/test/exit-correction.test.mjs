// Node 冒烟：出口异常纠正请求/校验。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  entryValueFromRecord, buildCorrectionPayload, validateCorrection, settlementSummary,
} from '../js/exit-correction.js';

test('record entry time converts to sim seconds/minutes', () => {
  assert.equal(entryValueFromRecord({ entry_sim_sec: 1980 }, 'min'), '33');
  assert.equal(entryValueFromRecord({ entry_sim_sec: 1980 }, 'sec'), '1980');
  assert.equal(entryValueFromRecord({}, 'min'), '');
});

test('correction with open record sends auto + record_id + entry_sim_min', () => {
  const p = buildCorrectionPayload({ recordId: 12, plate: '粤F4K1CZ', unit: 'min', value: '33', requestId: 'r1' });
  assert.equal(p.record_id, 12);
  assert.equal(p.action, 'auto');
  assert.equal(p.entry_sim_min, 33);
  assert.equal(p.entry_sim_sec, undefined);
  assert.equal(p.request_id, 'r1');
});

test('no record uses free/void and requires reason', () => {
  assert.match(validateCorrection({ plate: '粤F4K1CZ', action: 'auto' }).message, /免费或作废/);
  assert.equal(validateCorrection({ plate: '粤F4K1CZ', action: 'free', reason: '测试' }), null);
  const p = buildCorrectionPayload({ plate: '粤F4K1CZ', action: 'void', reason: '录错' });
  assert.equal(p.action, 'void');
  assert.equal(p.record_id, undefined);
  assert.equal(p.reason, '录错');
});

test('settlement summary never silently hides 0 amount', () => {
  assert.deepEqual(settlementSummary({ action: 'auto', amount_paid: 0 }), {
    label: '已收费 ¥0.00', detail: '记录已 out，出口已开',
  });
  assert.equal(settlementSummary({ action: 'void' }).label, '已作废');
  assert.equal(settlementSummary({ action: 'free' }).label, '已免费放行');
});
