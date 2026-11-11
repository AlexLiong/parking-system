// Node 冒烟：i18n 字典/动态模式翻译（不依赖浏览器 DOM）。
// 运行：node --experimental-default-type=module web/admin/test/i18n.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';

// i18n.js 读取 localStorage 决定 locale；在 import 前注入 en。
const memory = new Map([['parking.admin.locale', 'en']]);
globalThis.localStorage = {
  getItem: (k) => memory.get(k) ?? null,
  setItem: (k, v) => memory.set(k, String(v)),
  removeItem: (k) => memory.delete(k),
};
const { locale, t, translateText, _phraseCount } = await import('../js/i18n.js');

test('en locale loaded', () => {
  assert.equal(locale, 'en');
  assert.ok(_phraseCount >= 150, `phrase map too small: ${_phraseCount}`);
});

test('navigation keys translated', () => {
  assert.equal(t('monitor'), 'Live monitor');
  assert.equal(t('records'), 'Parking records');
  assert.equal(t('retry'), 'Retry');
});

test('static page labels translated', () => {
  assert.equal(translateText('实时监控'), 'Live monitor');
  assert.equal(translateText('停车记录'), 'Parking records');
  assert.equal(translateText('在场'), 'On site');
  assert.equal(translateText('待缴费'), 'Payment due');
  assert.equal(translateText('异常处理'), 'Incidents');
  assert.equal(translateText('车位布局'), 'Parking layout');
});

test('dynamic counts/status patterns translated', () => {
  assert.equal(translateText('共 12 条 · 第 1/2 页'), '12 records · page 1/2');
  assert.equal(translateText('A 区'), 'Zone A');
  assert.equal(translateText('空闲 / 共 20'), 'Free / 20');
  assert.equal(translateText('占用 5（25%）'), 'Occupied 5 (25%)');
  assert.equal(translateText('加载失败：网络错误'), 'Load failed: Network error');
  assert.equal(translateText('5 分钟'), '5 min');
});

test('whitespace is preserved', () => {
  assert.equal(translateText(' 实时监控 '), ' Live monitor ');
});
