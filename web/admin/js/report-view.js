// 报表预览的纯数据归一化：day/week/month/quarter/year 的明细层级。
export const REPORT_PERIODS = [
  ['day', '日'], ['week', '周'], ['month', '月'], ['quarter', '季'], ['year', '年'],
];

export function reportLevel(period) {
  return ['day', 'week', 'month', 'quarter', 'year'].includes(period) ? period : 'day';
}

export function normalizePreview(raw = {}) {
  const period = raw.period || raw.level || 'day';
  const level = raw.level || '';
  const byDay = Array.isArray(raw.by_day) ? raw.by_day : [];
  const weeks = Array.isArray(raw.weeks) ? raw.weeks : [];
  const months = Array.isArray(raw.months) ? raw.months : [];
  const directVehicles = Array.isArray(raw.vehicles) ? raw.vehicles : [];
  const dayVehicles = byDay.flatMap((day) => Array.isArray(day.vehicles) ? day.vehicles : []);
  const vehicles = (directVehicles.length ? directVehicles : dayVehicles)
    .filter((v) => !v.status || (v.status === 'out' && v.entry_time && v.exit_time));
  return {
    period: reportLevel(period),
    level,
    anchor: raw.anchor || '',
    start: raw.start || '',
    end: raw.end || '',
    summary: raw.summary || {},
    byDay,
    weeks,
    months,
    vehicles,
  };
}

export function vehicleField(vehicle, key) {
  if (!vehicle) return '—';
  const value = vehicle[key];
  return value == null || value === '' ? '—' : value;
}

export function summaryRows(summary = {}) {
  return [
    ['入场车次', summary.count_in ?? '—'],
    ['出场车次', summary.count_out ?? '—'],
    ['营收', summary.revenue ?? '—'],
    ['平均时长', summary.avg_minutes ?? '—'],
    ['峰值占用', summary.peak_occupancy ?? '—'],
  ];
}

// 基准控件：周期 -> 标签/默认值/传给后端的 anchor（day/week 用日期，month/quarter/year 用区间首日）。
export function periodAnchorLabel(period) {
  return ({ day: '基准日期', week: '基准周', month: '基准月份', quarter: '基准季度', year: '基准年份' })[period] || '基准日期';
}

const pad = (n) => String(n).padStart(2, '0');

export function isoWeekValue(date = new Date()) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - day);
  const isoYear = d.getUTCFullYear();
  const first = new Date(Date.UTC(isoYear, 0, 1));
  const week = Math.ceil((((d - first) / 86400000) + 1) / 7);
  return `${isoYear}-W${pad(week)}`;
}

export function weekToMonday(value) {
  const m = /^(\d{4})-W(\d{2})$/.exec(String(value || ''));
  if (!m) return '';
  const year = Number(m[1]);
  const week = Number(m[2]);
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const day = jan4.getUTCDay() || 7;
  const monday = new Date(jan4);
  monday.setUTCDate(jan4.getUTCDate() - (day - 1) + (week - 1) * 7);
  return monday.toISOString().slice(0, 10);
}

export function quarterStart(year, quarter) {
  const y = Number(year);
  const q = Number(quarter);
  if (!Number.isInteger(y) || !Number.isInteger(q) || q < 1 || q > 4) return '';
  return `${y}-${pad((q - 1) * 3 + 1)}-01`;
}

export function dateParts(date = new Date()) {
  return {
    date: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    week: isoWeekValue(date),
    month: `${date.getFullYear()}-${pad(date.getMonth() + 1)}`,
    quarterYear: String(date.getFullYear()),
    quarter: String(Math.floor(date.getMonth() / 3) + 1),
    year: String(date.getFullYear()),
  };
}

export function buildAnchor(period, values = {}) {
  if (period === 'day') return values.date || '';
  if (period === 'week') return weekToMonday(values.week);
  if (period === 'month') return values.month ? `${values.month}-01` : '';
  if (period === 'quarter') return quarterStart(values.quarterYear, values.quarter);
  if (period === 'year') return values.year ? `${values.year}-01-01` : '';
  return values.date || '';
}

// 报表导出语言：en -> en；其余（zh-CN/未设置）-> zh。
export function reportLang(localeValue) {
  return String(localeValue || '').toLowerCase().startsWith('en') ? 'en' : 'zh';
}

export function reportExportUrl(kind, period = 'day', anchor = null, lang = 'zh') {
  const path = kind === 'csv' ? '/api/reports/export.csv' : '/api/reports/export';
  const sp = new URLSearchParams();
  sp.set('period', period || 'day');
  if (anchor) sp.set('anchor', anchor);
  sp.set('lang', reportLang(lang));
  return `${path}?${sp.toString()}`;
}


