// 管理端事件流规范化：解包 WS/events 的 data|payload 包装，统一错误字段，
// 为未知事件生成可读摘要与字段列表（绝不把原始 JSON 直接塞进事件流）。
const LABELS = {
  plate_recognized: '车牌识别', fee_quote: '出口报价', barrier_cmd: '道闸指令',
  barrier_state: '道闸状态', space_update: '余位更新', space_assigned: '车位分配',
  layout_changed: '布局变更', incident: '异常', incident_resolved: '异常处理',
  demo_reset: '演示复位', pending_created: '待人工确认', pending_resolved: '人工确认结果',
  manual_authorized: '人工授权进场', exit_authorized: '人工授权出场', entry_denied: '拒绝进场',
  payment_completed: '缴费完成', arrival_retry_requested: '待办释放重试',
};

// 解包 data/payload；若外层已有 type 但 data 里是业务字段，也合并进来。
export function normalizeEvent(raw) {
  if (!raw || typeof raw !== 'object') return raw || {};
  let event = raw;
  for (const key of ['data', 'payload']) {
    const inner = event[key];
    if (inner && typeof inner === 'object' && (inner.type || inner.event_type)) {
      event = { ...event, ...inner };
    }
  }
  return event;
}

function primitive(value) {
  if (value == null || value === '') return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return '';
}

export function eventFields(raw) {
  const m = normalizeEvent(raw);
  const fields = [];
  const seen = new Set();
  const add = (key, value) => {
    const text = primitive(value);
    if (!text || seen.has(key)) return;
    seen.add(key);
    fields.push([key, text]);
  };
  const error = (m.error && typeof m.error === 'object') ? m.error : {};
  const detailObject = (m.detail && typeof m.detail === 'object') ? m.detail
    : (error.detail && typeof error.detail === 'object' ? error.detail : null);
  add('code', m.code || m.error_code || error.code || (typeof m.error === 'string' ? m.error : ''));
  add('message', m.message || error.message || m.detail?.message || '');
  add('detail', primitive(m.detail) || primitive(error.detail) || '');
  add('plate', m.plate || error.plate || m.detail?.plate || detailObject?.plate || '');
  add('gate', m.gate);
  add('state', m.state || m.status);
  add('action', m.action);
  add('reason', m.reason);
  add('vehicle_id', m.vehicle_id);
  add('record_id', m.record_id);
  add('space_no', m.space_no);
  add('amount', m.amount ?? m.amount_due ?? m.amount_paid);
  add('seq', m.seq);
  // detail 对象扁平化为可读字段，不展示原始 JSON。
  if (detailObject) {
    for (const [key, value] of Object.entries(detailObject)) {
      if (key === 'message' || key === 'plate') continue;
      add(`detail.${key}`, value);
    }
  }
  if (m.data && typeof m.data === 'object' && m.data !== m && !m.data.type) {
    for (const [key, value] of Object.entries(m.data)) add(`data.${key}`, value);
  }
  if (m.payload && typeof m.payload === 'object' && m.payload !== m && !m.payload.type) {
    for (const [key, value] of Object.entries(m.payload)) add(`payload.${key}`, value);
  }
  return fields;
}

export function eventLabel(raw) {
  const m = normalizeEvent(raw);
  return LABELS[m.type || m.event_type] || String(m.type || m.event_type || '事件');
}

// 未知事件：只给类型/关键字段，不输出 JSON。
export function unknownSummary(raw) {
  const m = normalizeEvent(raw);
  const bits = [];
  const error = (m.error && typeof m.error === 'object') ? m.error : {};
  for (const value of [m.plate, m.gate, m.code || m.error_code || error.code,
    m.message || error.message, m.action, m.reason]) {
    const text = primitive(value);
    if (text) bits.push(text);
  }
  return bits.length ? bits.join(' · ') : '无可展开字段';
}

export function eventView(raw) {
  const m = normalizeEvent(raw);
  return {
    event: m,
    type: String(m.type || m.event_type || 'unknown'),
    label: eventLabel(m),
    fields: eventFields(m),
    summary: unknownSummary(m),
  };
}
