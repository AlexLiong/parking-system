// Node 冒烟：报表预览明细层级/完整记录过滤。
import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizePreview, summaryRows, periodAnchorLabel, weekToMonday, quarterStart, buildAnchor, reportLang, reportExportUrl } from '../js/report-view.js';

const vehicle = (status = 'out') => ({
  plate: '粤F4K1CZ', entry_time: '2026-11-01T10:00:00+08:00',
  exit_time: '2026-11-01T11:00:00+08:00', status, amount: 5,
});

test('day/week preview exposes daily vehicles', () => {
  const day = normalizePreview({ period: 'day', level: 'vehicle_day', by_day: [{ date: '2026-11-01', vehicles: [vehicle()] }] });
  assert.equal(day.period, 'day');
  assert.equal(day.level, 'vehicle_day');
  assert.equal(day.vehicles.length, 1);
  const week = normalizePreview({ period: 'week', level: 'day', by_day: [
    { date: '2026-11-01', vehicles: [vehicle()] },
    { date: '2026-11-02', vehicles: [vehicle()] },
  ] });
  assert.equal(week.byDay.length, 2);
  assert.equal(week.vehicles.length, 2);
});

test('month/quarter/year use grouped summaries', () => {
  const month = normalizePreview({ period: 'month', weeks: [{ week: 1 }] });
  assert.equal(month.weeks.length, 1);
  const quarter = normalizePreview({ period: 'quarter', months: [{ month: 11 }] });
  assert.equal(quarter.months.length, 1);
  const year = normalizePreview({ period: 'year', months: [{ month: 1 }, { month: 2 }] });
  assert.equal(year.months.length, 2);
});

test('only complete records are exposed', () => {
  const p = normalizePreview({ period: 'day', by_day: [{ vehicles: [
    vehicle(), vehicle('in'), { ...vehicle(), exit_time: null },
  ] }] });
  assert.equal(p.vehicles.length, 1);
});

test('summary rows are fixed labels', () => {
  assert.deepEqual(summaryRows({ count_in: 1, revenue: 5 }).map((r) => r[0]), [
    '入场车次', '出场车次', '营收', '平均时长', '峰值占用',
  ]);
});

test('anchor controls convert week/quarter to backend anchor dates', () => {
  assert.equal(periodAnchorLabel('week'), '基准周');
  assert.equal(periodAnchorLabel('quarter'), '基准季度');
  assert.equal(weekToMonday('2026-W45'), '2026-11-02');
  assert.equal(quarterStart(2026, 4), '2026-10-01');
  assert.equal(buildAnchor('day', { date: '2026-11-04' }), '2026-11-04');
  assert.equal(buildAnchor('week', { week: '2026-W45' }), '2026-11-02');
  assert.equal(buildAnchor('month', { month: '2026-11' }), '2026-11-01');
  assert.equal(buildAnchor('quarter', { quarterYear: 2026, quarter: 2 }), '2026-04-01');
  assert.equal(buildAnchor('year', { year: '2026' }), '2026-01-01');
});

test('report export urls carry zh/en lang', () => {
  assert.equal(reportLang('en'), 'en');
  assert.equal(reportLang('en-US'), 'en');
  assert.equal(reportLang('zh-CN'), 'zh');
  assert.equal(reportLang(undefined), 'zh');
  const enCsv = reportExportUrl('csv', 'week', '2026-11-02', 'en');
  const zhPdf = reportExportUrl('pdf', 'year', '2026-01-01', 'zh');
  assert.match(enCsv, /^\/api\/reports\/export\.csv\?/);
  assert.match(enCsv, /period=week/);
  assert.match(enCsv, /anchor=2026-11-02/);
  assert.match(enCsv, /lang=en/);
  assert.match(zhPdf, /^\/api\/reports\/export\?/);
  assert.match(zhPdf, /lang=zh/);
  assert.doesNotMatch(zhPdf, /lang=en/);
});
