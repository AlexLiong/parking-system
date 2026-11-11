// 管理端 REST 客户端：统一鉴权头、错误归一化、401 自动登出。
// API 同源，绝对路径 /api/...（管理端挂载于 /admin/，故不能用相对路径）。
import { store } from './util.js';
import { reportExportUrl } from './report-view.js';

const BASE = '';
let _onUnauthorized = null;
export function setUnauthorizedHandler(fn) { _onUnauthorized = fn; }

export class ApiError extends Error {
  constructor(status, message, detail, code = '') {
    super(message);
    this.status = status;
    this.detail = detail || {};
    this.code = code || payloadCode(detail);
  }
}

function payloadCode(detail) {
  return (detail && (detail.error || detail.reason_code || detail.kind)) || '';
}

async function request(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  let body = opts.body;
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const token = store.getToken();
  if (token && !headers.Authorization) headers.Authorization = `Bearer ${token}`;

  let res;
  try {
    res = await fetch(BASE + path, { ...opts, headers, body });
  } catch (netErr) {
    throw new ApiError(0, `网络错误：无法连接服务（${netErr.message || netErr}）`, {});
  }

  if (res.status === 401 && path !== '/api/auth/login' && _onUnauthorized) {
    // 令牌失效/过期：清理并交由 app 跳转登录
    store.clear();
    _onUnauthorized();
  }

  const ct = res.headers.get('content-type') || '';
  let payload = null;
  if (ct.includes('application/json')) {
    try { payload = await res.json(); } catch (_) { payload = null; }
  } else {
    try { payload = await res.text(); } catch (_) { payload = null; }
  }

  if (!res.ok) {
    const detail = payload && payload.detail;
    const detailMessage = typeof detail === 'string' ? detail : detail?.reason;
    const msg = (payload && (payload.message || detailMessage || payload.error)) ||
      `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, typeof msg === 'string' ? msg : JSON.stringify(msg),
      detail || {}, (payload && payload.error) || '');
  }
  return payload;
}

// FormData 上传（识别图片）：不带 Content-Type，让浏览器自动设 multipart 边界
function upload(path, formData) {
  const token = store.getToken();
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  return fetch(BASE + path, { method: 'POST', headers, body: formData })
    .then(async (res) => {
      const ct = res.headers.get('content-type') || '';
      const payload = ct.includes('application/json') ? await res.json() : await res.text();
      if (!res.ok) {
        if (res.status === 401 && _onUnauthorized) { store.clear(); _onUnauthorized(); }
        const msg = (payload && (payload.message || payload.error)) || `${res.status}`;
        throw new ApiError(res.status, msg, (payload && payload.detail) || {});
      }
      return payload;
    });
}

const get = (p) => request(p, { method: 'GET' });
const post = (p, body) => request(p, { method: 'POST', body });
const put = (p, body) => request(p, { method: 'PUT', body });
const del = (p) => request(p, { method: 'DELETE' });

export const Api = {
  barrierAction: (gate, action, extra = {}) => {
    const path = ['close', 'stop'].includes(action) ? `/api/manual/${action}-barrier` :
      `/api/barrier/${gate}/${action}`;
    return post(path, { gate, reason: 'admin_manual', ...extra });
  },
  barrierActions: () => get('/api/barrier/actions'),
  exportBarrierActions: () => get('/api/barrier/actions/export'),
  // ---- 认证 ----
  login: (username, password) => post('/api/auth/login', { username, password }),
  me: () => get('/api/auth/me'),
  logout: () => post('/api/auth/logout', {}),

  // ---- 总览 / 车位 / 布局 ----
  recognitionGallery:()=>get('/api/recognize/gallery'),
  findCar: (plate) => get('/api/find-car?plate='+encodeURIComponent(plate)),
  overview: () => get('/api/overview'),
  snapshot: () => get('/api/state/snapshot'),
  events: (since, limit = 500) => get(`/api/events?since=${since}&limit=${limit}`),
  recentEvents: () => get('/api/events?last=200'),
  resetDemo: (request_id) => post('/api/demo/reset', {
    request_id, mode: 'soft', new_session: true }),
  forceReset: (request_id) => post('/api/demo/reset', {
    request_id, force: true, new_session: true, reason: 'admin_force_reset' }),
  spaces: (zone) => get('/api/spaces' + (zone ? `?zone=${encodeURIComponent(zone)}` : '')),
  layout: () => get('/api/layout'),
  applyLayout: (zones) => post('/api/admin/layout', { zones }),
  exportLayout: () => get('/api/admin/layout/export'),
  importLayout: (payload) => post('/api/admin/layout/import', payload),

  // ---- 演示剧本（管理端编辑；3D 端播放时自行锁播放）----
  scenario: () => get('/api/admin/scenario'),
  saveScenario: (scenario) => put('/api/admin/scenario', scenario),
  resetScenario: () => post('/api/admin/scenario/reset', {}),

  // ---- 记录 / 报价 / 缴费 ----
  records: (params) => {
    const sp = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== '') sp.append(k, String(v));
    });
    const q = sp.toString();
    return get('/api/records' + (q ? `?${q}` : ''));
  },
  record: (id) => get(`/api/records/${id}`),
  quote: (plate) => get(`/api/quote?plate=${encodeURIComponent(plate)}`),
  pay: (record_id, method = 'mock_scan', amount_override = null) =>
    post('/api/pay', { record_id, method, amount_override }),

  // ---- 统计 / 报表 ----
  stats: (period = 'day', anchor = null) =>
    get(`/api/stats?period=${period}` + (anchor ? `&anchor=${encodeURIComponent(anchor)}` : '')),
  reportPreview: (period = 'day', anchor = null) =>
    get(`/api/reports/preview?period=${period}` + (anchor ? `&anchor=${encodeURIComponent(anchor)}` : '')),
  reportPdfUrl: (period = 'day', anchor = null, lang = 'zh') =>
    reportExportUrl('pdf', period, anchor, lang),
  reportCsvUrl: (period = 'day', anchor = null, lang = 'zh') =>
    reportExportUrl('csv', period, anchor, lang),
  reportUrl: (period = 'day', anchor = null, lang = 'zh') =>
    reportExportUrl('pdf', period, anchor, lang),

  // ---- 费率版本 ----
  feeRules: () => get('/api/fee-rules'),
  currentFeeRule: () => get('/api/fee-rules/current'),
  updateCurrentFeeRule: (body) => put('/api/fee-rules/current', body),
  addFeeRule: (body) => post('/api/fee-rules', body),
  replaceFeeRule: (id, body) => put(`/api/fee-rules/${id}`, body),

  // ---- 异常 / 日志 ----
  incidents: (status = null, limit = 100) =>
    get('/api/incidents' + `?limit=${limit}` + (status ? `&status=${encodeURIComponent(status)}` : '')),
  resolveIncident: (id, resolution) => post(`/api/incidents/${id}/resolve`, { resolution }),
  recognitionLogs: (limit = 50) => get(`/api/recognition-logs?limit=${limit}`),
  correctRecognition: (id, plate) => post(`/api/recognition-logs/${id}/correct`, { plate }),
  oplogs: (limit = 100) => get(`/api/oplogs?limit=${limit}`),

  // ---- 名单 ----
  lists: (list_type = null) =>
    get('/api/lists' + (list_type ? `?list_type=${encodeURIComponent(list_type)}` : '')),
  upsertList: (body) => post('/api/lists', body),
  removeList: (plate) => del(`/api/lists/${encodeURIComponent(plate)}`),

  // ---- 人工干预 / 摄像头 ----
  manualOpen: (gate, reason = 'admin') => post('/api/manual/open-barrier', { gate, reason }),
  authorizeEntry: (payload) => post('/api/manual/authorize-entry', payload),
  authorizeExit: (payload) => post('/api/manual/authorize-exit', payload),
  manualPending: (status = 'pending') => get(`/api/manual/pending?status=${encodeURIComponent(status)}`),
  manualConfirm: (id, plate, options = {}) => post('/api/manual/confirm', { id, plate, ...options }),
  correctExit: (payload) => post('/api/manual/correct-exit', payload),
  manualReject: (id, reason = '') => post('/api/manual/reject', { id, reason }),
  manualRegister: (plate, color = 'blue') => post('/api/manual/register', { plate, color }),
  manualExit: (plate, reason = '', options = {}) => post('/api/manual/exit', { plate, reason, request_id:crypto.randomUUID(), ...options }),
  cameraInject: (gate, reason = 'manual') => post(`/api/camera/${gate}/inject`, { reason }),
  cameraRecover: (gate, resolution = '摄像头已恢复') =>
    post(`/api/camera/${gate}/recover`, { reason: 'manual', resolution }),

  // ---- 车牌图片识别（导入图片识别，FR-1.3）----
  // file: File 对象（上传）；或 imagePath: 服务器图库相对/绝对路径
  recognize: ({ file = null, imagePath = null, gate = 'in' }) => {
    if (file) {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('gate', gate);
      return upload('/api/recognize', fd);
    }
    const sp = new URLSearchParams({ gate });
    if (imagePath) sp.append('image_path', imagePath);
    return request(`/api/recognize?${sp.toString()}`, { method: 'POST' });
  },
};

export { request, upload };
