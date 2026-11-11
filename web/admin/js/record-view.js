// 记录详情计费明细的纯数据映射（固定中文字段，未知字段折叠）。
export const PAYMENT_METHOD_LABELS = {
  auto: '自动扣费',
  mock_scan: '扫码',
  cash: '现金',
  free: '免费',
  monthly: '月租',
};

export function paymentMethodLabel(method) {
  const key = String(method || '').trim();
  if (!key) return '—';
  return PAYMENT_METHOD_LABELS[key] || key;
}

const money = (v) => `¥${Number(v || 0).toFixed(2)}`;
const minutes = (v) => `${Number(v || 0)} 分钟`;
const capLabel = (v) => {
  if (typeof v === 'boolean') return v ? '已封顶' : '未封顶';
  const n = Number(v);
  return Number.isFinite(n) ? money(n) : String(v ?? '—');
};
const discountLabel = (v) => {
  const n = Number(v);
  return Number.isFinite(n) ? `${(n * 10).toFixed(1)} 折` : String(v ?? '—');
};

const KNOWN = [
  ['minutes', '停放时长', minutes],
  ['free_minutes_applied', '免费时长', minutes],
  ['billable_minutes', '计费时长', minutes],
  ['first_hour_price', '首小时', money],
  ['unit_minutes', '续时单位', minutes],
  ['unit_price', '续时单价', money],
  ['billable_units', '续时单位数', (v) => String(v ?? '0')],
  ['daily_cap_applied', '单日封顶', capLabel],
  ['new_energy_discount', '新能源折扣', discountLabel],
  ['amount_due', '应收', money],
];

function flattenUnknown(value, prefix, out) {
  if (value == null) return;
  if (typeof value === 'object') {
    if (Array.isArray(value)) {
      value.forEach((item, index) => flattenUnknown(item, `${prefix}[${index}]`, out));
    } else {
      Object.keys(value).sort().forEach((key) => flattenUnknown(value[key], `${prefix}.${key}`, out));
    }
    return;
  }
  out.push([prefix, String(value)]);
}

export function billingRows(breakdown, payment = {}, rule = {}) {
  const source = (breakdown && typeof breakdown === 'object' && !Array.isArray(breakdown))
    ? { ...breakdown } : {};
  if (source.amount_due == null && payment.amount_due != null) source.amount_due = payment.amount_due;
  if (source.minutes == null && payment.minutes != null) source.minutes = payment.minutes;
  // 记录快照可能省略 unit_minutes；从该记录费率版本补齐展示值。
  if (source.unit_minutes == null && rule.unit_minutes != null) source.unit_minutes = rule.unit_minutes;
  const rows = [];
  const used = new Set(['detail']);
  for (const [key, label, format] of KNOWN) {
    if (source[key] == null) continue;
    used.add(key);
    rows.push({ key, label, value: format(source[key]) });
  }
  const unknown = [];
  Object.keys(source).sort().forEach((key) => {
    if (used.has(key)) return;
    flattenUnknown(source[key], key, unknown);
  });
  return {
    rows,
    unknown,
    detail: typeof source.detail === 'string' ? source.detail : '',
  };
}

export function rateVersionInfo(recordRule, currentRule) {
  if (!recordRule) return { version: '—', differs: false, note: '' };
  const id = recordRule.id != null ? `#${recordRule.id}` : '—';
  const differs = !!currentRule && String(recordRule.id) !== String(currentRule.id);
  return {
    version: id,
    differs,
    effectiveFrom: recordRule.effective_from || '',
    note: differs ? '历史费率（与当前不同）' : '',
  };
}
