// 实时事件归一化/摘要（纯函数）：HUD 事件列表的摘要行与详情字段。
// 输入可以是原始 WS 事件对象，或旧代码的字符串日志；输出统一结构，便于点击/键盘展开。

const TYPE_LABELS = {
  plate_recognized: '车牌识别', recognition: '车牌识别',
  fee_quote: '出口报价', payment_completed: '缴费完成', pay: '缴费',
  barrier_cmd: '道闸指令', barrier_state: '道闸状态', barrier: '道闸',
  camera_frame: '相机截图', camera_frame_sent: '相机截图', camera: '相机',
  space_update: '余位更新', space_assigned: '车位分配', assigned: '车位分配',
  parked: '入库', left: '离场', exit_start: '开始出场', passed: '过闸',
  arrived: '到达', auto_exit: '自动出场',
  incident: '异常事件', incident_resolved: '异常处理', pending_created: '待人工确认',
  pending_resolved: '人工确认结果', layout_changed: '布局变更', warn: '警告',
  error: '错误', scenario: '剧本', manual_dispatch: '手动派车', auto_exit: '自动出场',
  info: '信息', log: '信息', spawn: '派车', queue_hold: '排队',
  entry_denied: '入口拒绝', entry_denied_departed: '拒绝车辆离场', entry_authorized: '入口授权',
  exit_authorized: '出口授权', manual_authorized: '人工授权',
  blacklist: '黑名单拒绝', duplicate_entry: '重复车牌', duplicate_plate: '重复车牌',
  denied: '拒绝', stuck: '卡住恢复', recovery: '恢复', demo_reset: '清场重置',
  blacklist_resolution: '黑名单路径回报', blacklist_retry: '黑名单后车重试',
  blacklist_retry_authorized: '黑名单后车已授权', arrival_retry_requested: '入口重抓拍请求',
  blacklist_gate: '黑名单闸杆',
};

  const TYPE_CLASS = {
  plate_recognized: 'evt-plate', fee_quote: 'evt-fee', payment_completed: 'evt-ok',
  barrier_cmd: 'evt-barrier', barrier_state: 'evt-barrier', incident: 'evt-warn',
  warn: 'evt-warn', error: 'evt-warn', manual_dispatch: 'evt-manual',
  auto_exit: 'evt-ok', parked: 'evt-ok', left: 'evt-ok', camera_frame: 'evt-camera',
  camera_frame_sent: 'evt-camera', entry_denied: 'evt-warn', blacklist: 'evt-warn',
  duplicate_entry: 'evt-warn', duplicate_plate: 'evt-warn',
  entry_authorized: 'evt-ok', exit_authorized: 'evt-ok', manual_authorized: 'evt-ok',
};

const LABELS = {
  gate: '门', plate: '车牌', amount: '金额', minutes: '时长',
  reason: '原因', record_id: '记录', image_url: '图片', image_path: '图片路径',
  plate_crop_url: '车牌裁剪', expected_plate: '期望车牌', conf: '置信度',
  cmd: '指令', state: '状态', arm_position: '杆位', kind: '类型',
  space: '车位', space_no: '车位', mode: '模式', status: '状态',
  capture_ms: '捕获耗时', width: '宽', height: '高', detail: '详情',
  message: '说明', source_plate: '识别车牌', pending_id: '待确认', plate_type: '车牌类型',
  duration: '时长', vehicle_kind: '车型', url: 'URL', retried: '重试', authorized: '已授权',
  failed: '失败', pending_ids: '待办', retried_vehicle_ids: '重试车辆',
  entryGateAtSim: '入口过闸', parkedAtSim: '入库时刻', entryTravel: '入场行驶',
  exitTravelEst: '出场行驶估计', scheduledExitAt: '计划出口', autoExitAt: '自动出场时刻',
  gateToGateActual: '入口→出口实测',
};

export function eventTypeLabel(type) {
  const k = String(type || '');
  return TYPE_LABELS[k] || (k ? `事件(${k})` : '事件');
}
export function eventClass(type) {
  return TYPE_CLASS[String(type || '')] || '';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function gateText(gate) {
  if (!gate) return '';
  return gate === 'in' ? '入口' : gate === 'out' ? '出口' : String(gate);
}

function fmtAmount(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `¥${n.toFixed(2)}` : '';
}

/** 递归解开 WS/HTTP 常见的 {data}/{payload}/{event}/{result} 包装，保留外层 type。 */
export function unwrapEvent(input) {
  let raw = input;
  let guard = 0;
  while (raw && typeof raw === 'object' && !Array.isArray(raw) && guard++ < 6) {
    const inner = raw.data ?? raw.payload ?? raw.event ?? raw.result;
    if (!inner || typeof inner !== 'object' || Array.isArray(inner)) break;
    // 内层若本身也有 type，优先保留内层；否则合并外层 type 等标量字段。
    raw = { ...raw, ...inner, type: inner.type || raw.type };
    delete raw.data; delete raw.payload; delete raw.event; delete raw.result;
  }
  return raw;
}

/** 对象/数组 detail → 可读多行文本；避免事件详情出现裸 JSON.stringify。 */
export function formatDetail(detail, maxItems = 12) {
  if (detail == null || detail === '') return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.slice(0, maxItems).map((item) => {
      if (item == null) return '';
      if (typeof item === 'string') return item;
      const loc = Array.isArray(item.loc) ? item.loc.join('.') : (item.field || item.key || '');
      const msg = item.msg || item.message || item.detail || '';
      return loc && msg ? `${loc}: ${msg}` : (msg || formatDetail(item, maxItems));
    }).filter(Boolean).join('; ');
  }
  if (typeof detail === 'object') {
    return Object.entries(detail).slice(0, maxItems)
      .filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `${k}=${typeof v === 'object' ? formatDetail(v, maxItems) : v}`)
      .join('; ');
  }
  return String(detail);
}

/**
 * 统一错误口径：{code,message,detail,plate}。
 * 兼容 {error:{code,message}}, FastAPI 422 detail[], HTTP statusText, Error 对象。
 */
export function normalizeError(input, fallback = {}) {
  const raw = unwrapEvent(input) || {};
  const err = raw.error && typeof raw.error === 'object' ? raw.error : {};
  const body = raw.body && typeof raw.body === 'object' ? raw.body : {};
  let code = raw.code || err.code || body.code || fallback.code
    || (raw.status ? `http_${raw.status}` : '');
  let message = raw.message || raw.detail?.message || err.message || body.message
    || fallback.message || (typeof raw === 'string' ? raw : '请求失败');
  let detail = raw.detail ?? err.detail ?? body.detail ?? fallback.detail ?? null;
  // api.js 的 Error.message 形如 `409 /api/...: {"error":...}`：解析出结构化 code/detail。
  const jsonAt = typeof message === 'string' ? message.search(/\{[\s\S]*\}\s*$/) : -1;
  if (jsonAt >= 0) {
    try {
      const parsed = JSON.parse(message.slice(jsonAt));
      if (parsed && typeof parsed === 'object') {
        code = parsed.code || parsed.error?.code || code;
        detail = parsed.detail ?? parsed.error?.detail ?? detail;
        if (parsed.message || parsed.error?.message) message = parsed.message || parsed.error.message;
        else message = message.slice(0, jsonAt).trim();
      }
    } catch (_) { /* keep raw message */ }
  }
  const plate = raw.plate || err.plate || body.plate || raw.expected_plate || fallback.plate || null;
  return { code: String(code || ''), message: String(message || '请求失败'),
    detail: formatDetail(detail), plate: plate ? String(plate) : null, raw };
}

/**
 * @param {object|string} input 事件对象或日志字符串
 * @returns {{type,label,cls,time,summary,detail,fields:Array<{label,value,url,image}>}}
 */
export function normalizeEvent(input, extra = {}) {
  const raw = unwrapEvent(input && typeof input === 'object' ? input : { message: String(input == null ? '' : input) }) || {};
  const type = String(extra.type || raw.type || 'info');
  const msg = raw.msg || raw.message || extra.message || '';
  const plate = raw.plate || raw.expected_plate || raw.source_plate || raw.meta?.plate || '';
  const gate = raw.gate || '';
  const amount = raw.amount ?? raw.fee ?? null;
  const minutes = raw.minutes ?? (raw.type === 'manual_dispatch' ? null : raw.duration);
  const durationSec = raw.duration_sec ?? null;
  const reason = raw.reason || raw.detail?.reason || raw.resolution || '';
  const label = eventTypeLabel(type);

  let summary = '';
  if (type === 'plate_recognized' || type === 'recognition') {
    summary = `${gateText(gate)}识别 ${plate || '—'}${raw.need_manual ? ' · 待人工确认' : ''}`;
  } else if (type === 'fee_quote') {
    summary = `${gateText(gate) || '出口'}报价 ${plate || ''} ${fmtAmount(amount)}`.trim();
  } else if (type === 'payment_completed') {
    summary = `${gateText(gate) || '出口'}已缴费 ${fmtAmount(amount)} ${plate || ''}`.trim();
  } else if (type === 'barrier_cmd') {
    summary = `${gateText(gate)}道闸指令 ${raw.cmd || raw.action || ''}${reason ? ' · ' + reason : ''}`;
  } else if (type === 'barrier_state') {
    summary = `${gateText(gate)}道闸状态 ${raw.state || ''}`.trim();
  } else if (type === 'camera_frame' || type === 'camera_frame_sent') {
    summary = `${gateText(gate)}相机截图 ${raw.width || ''}${raw.width && raw.height ? '×' : ''}${raw.height || ''}`.trim();
  } else if (type === 'space_update') {
    const f = raw.free || raw;
    const total = f.total ?? f.free_total ?? '';
    summary = `余位更新${total !== '' ? ' · 总余位 ' + total : ''}`;
  } else if (type === 'space_assigned' || type === 'assigned') {
    summary = `分配车位 ${raw.space_no || raw.space || ''} ${raw.id || raw.plate || ''}`.trim();
  } else if (type === 'parked') {
    summary = `已入库 ${plate || raw.id || ''}${raw.space ? ' @ ' + raw.space : ''}`.trim();
  } else if (type === 'left') {
    summary = `驶离 ${plate || raw.vid || raw.id || ''}`.trim();
  } else if (type === 'incident') {
    summary = `异常 ${raw.kind || ''}${gate ? ' / ' + gateText(gate) : ''}`
      + (raw.detail?.reason || raw.reason ? ` · ${raw.detail?.reason || raw.reason}` : '');
  } else if (type === 'entry_denied' || type === 'blacklist' || type === 'duplicate_entry'
    || type === 'duplicate_plate' || type === 'denied') {
    summary = `入口拒绝 ${plate || ''}`
      + `${raw.reason || raw.detail?.reason ? ' · ' + (raw.reason || raw.detail?.reason) : ''}`.trim();
  } else if (type === 'entry_authorized' || type === 'exit_authorized' || type === 'manual_authorized') {
    summary = `${type === 'entry_authorized' ? '入口授权' : type === 'exit_authorized' ? '出口授权' : '人工授权'}`
      + `${plate ? ' ' + plate : ''}${gate ? ' / ' + gateText(gate) : ''}`;
  } else if (type === 'blacklist_retry') {
    summary = `黑名单后车重试 ${raw.retried != null ? `重试 ${raw.retried}` : ''}`
      + `${raw.authorized != null ? ` · 授权 ${raw.authorized}` : ''}`
      + `${raw.expected_authorized ? ` · 期望牌授权 ${raw.expected_authorized}` : ''}`
      + `${raw.failed != null ? ` · 失败 ${raw.failed}` : ''}`;
  } else if (type === 'blacklist_retry_authorized') {
    summary = `黑名单后车已授权 ${plate || raw.id || ''}`.trim();
  } else if (type === 'arrival_retry_requested') {
    summary = `入口重抓拍请求 ${plate || raw.vehicle_id || ''}`
      + `${raw.reason ? ' · ' + raw.reason : ''}`;
  } else if (type === 'error' || raw.code) {
    const err = normalizeError(raw);
    summary = `错误 ${err.code ? '[' + err.code + '] ' : ''}${err.message}`;
  } else if (type === 'pending_created') {
    summary = `待人工确认 #${raw.pending_id || ''} ${gateText(gate)} ${raw.source_plate || plate || '未识别'}`.trim();
  } else if (type === 'manual_dispatch') {
    summary = `手动派车 ${plate || ''}${raw.plate_type ? ' · ' + raw.plate_type : ''}`.trim();
  } else if (type === 'auto_exit') {
    summary = `自动出场 ${plate || raw.vid || raw.id || ''}${raw.reason ? ' · ' + raw.reason : ''}`.trim();
  } else if (type === 'scenario' || type === 'scn') {
    summary = msg || raw.name || '剧本';
  } else {
    summary = msg || raw.summary || extra.summary || label;
  }
  // 文本已被截断时保留原文到详情
  const detailLines = [];
  if (msg && msg !== summary) detailLines.push({ label: '原文', value: msg });
  if (plate) detailLines.push({ label: '车牌', value: plate });
  if (gate) detailLines.push({ label: '门', value: gateText(gate) });
  if (amount != null && amount !== '') detailLines.push({ label: '金额', value: fmtAmount(amount) });
  if (minutes != null && minutes !== '') detailLines.push({ label: '时长', value: `${minutes} 分钟` });
  if (durationSec != null && durationSec !== '') detailLines.push({ label: '停车时长', value: `${durationSec} 仿真秒` });
  if (reason) detailLines.push({ label: '原因', value: reason });
  if (raw.record_id != null) detailLines.push({ label: '记录', value: raw.record_id });
  if (raw.code) detailLines.push({ label: '错误码', value: raw.code });
  if (raw.image_url) detailLines.push({ label: '图片', value: raw.image_url, url: raw.image_url, image: true });
  if (raw.plate_crop_url) detailLines.push({ label: '车牌裁剪', value: raw.plate_crop_url, url: raw.plate_crop_url, image: true });
  for (const k of ['expected_plate', 'conf', 'cmd', 'state', 'arm_position', 'kind', 'mode',
    'status', 'space_no', 'capture_ms', 'width', 'height', 'message', 'source_plate',
    'pending_id', 'plate_type', 'duration', 'vehicle_kind', 'source', 'candidates',
    'retried', 'authorized', 'failed', 'pending_ids', 'retried_vehicle_ids', 'expected_authorized',
    'entryGateAtSim', 'parkedAtSim', 'entryTravel', 'exitTravelEst', 'scheduledExitAt',
    'autoExitAt', 'gateToGateActual']) {
    const v = raw[k];
    if (v == null || v === '' || (k === 'message' && v === msg)) continue;
    if (detailLines.some((d) => d.label === LABELS[k] && String(d.value) === String(v))) continue;
    detailLines.push({ label: LABELS[k] || k, value: Array.isArray(v) ? v.join(', ') : v });
  }
  if (raw.detail != null && raw.detail !== '') {
    const formatted = formatDetail(raw.detail);
    if (formatted) detailLines.push({ label: '详情', value: formatted });
  }
  if (TYPE_LABELS[type] == null) detailLines.push({ label: '事件类型', value: type });
  const time = raw.time || raw.at || raw.ts || extra.time || '';
  return {
    type, label, cls: eventClass(type), time: String(time),
    gate: gateText(gate), plate: String(plate || ''), summary,
    detail: detailLines, fields: detailLines,
  };
}

/** 供 DOM 使用的安全 HTML（HUD 使用；单测测试文本摘要）。 */
export function eventSummaryHtml(ev) {
  return `<span class="evt-type ${esc(ev.cls)}">${esc(ev.label)}</span>`
    + `<span class="evt-summary">${esc(ev.summary)}</span>`;
}

export { esc };
