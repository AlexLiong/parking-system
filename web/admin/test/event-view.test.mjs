// Node 冒烟：管理端事件流规范化（data/payload 解包、错误字段、未知事件不输出 JSON）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeEvent, eventFields, eventView, unknownSummary } from '../js/event-view.js';

test('unwraps data and payload envelopes', () => {
  assert.equal(normalizeEvent({ data: { type: 'plate_recognized', plate: '湘N7D071' } }).plate, '湘N7D071');
  assert.equal(normalizeEvent({ payload: { type: 'barrier_state', state: 'FAULT' } }).state, 'FAULT');
  assert.equal(normalizeEvent({ type: 'x', data: { plate: 'P' } }).data.plate, 'P');
});

test('unified error fields code/message/detail/plate', () => {
  const raw = { type: 'entry_denied', error: { code: 'blacklist', message: 'blocked', detail: { plate: '湘N7D071', reason: 'blacklist' } }, plate: '湘N7D071' };
  const fields = eventFields(raw);
  const map = Object.fromEntries(fields);
  assert.equal(map.code, 'blacklist');
  assert.equal(map.message, 'blocked');
  assert.equal(map.plate, '湘N7D071');
  assert.equal(map['detail.reason'], 'blacklist');
});

test('unknown event summary is readable and never raw JSON', () => {
  const raw = { type: 'some_new_event', plate: '湘N7D071', code: 'weird_code', detail: { foo: 'bar' } };
  const view = eventView(raw);
  assert.equal(view.label, 'some_new_event');
  assert.match(view.summary, /湘N7D071/);
  assert.match(view.summary, /weird_code/);
  assert.doesNotMatch(view.summary, /[{}]/);
  assert.ok(view.fields.some(([k]) => k === 'detail.foo'));
  assert.doesNotMatch(unknownSummary(raw), /{/);
});

test('known event label is readable', () => {
  assert.equal(eventView({ type: 'manual_authorized' }).label, '人工授权进场');
  assert.equal(eventView({ data: { type: 'exit_authorized' } }).label, '人工授权出场');
});
