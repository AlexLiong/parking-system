// 与后端的通信层：REST（仿真事件上报 / 布局 / 剧本）+ WebSocket（实时下行）。
// 业务真理在后端：前端只上报"物理事实"（到达/通过/杆区占用/动画回执），
// 绝不自行决定开闸（§2.2、§6.2）。
import { API_BASE } from './config.js';

// 演示终端会话令牌（登录后持有，供缴费/摄像头注入/人工放行等管理动作）
let _token = null;
export function setToken(t) { _token = t || null; }
export function getToken() { return _token; }

async function jfetch(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (_token) headers.Authorization = `Bearer ${_token}`;
  const res = await fetch(API_BASE + path, { ...opts, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = JSON.stringify(await res.json()); } catch (_) { /* ignore */ }
    const error = new Error(`${res.status} ${path}: ${detail}`); error.status = res.status; throw error;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

/** multipart（相机截图识别）：不设置 Content-Type，交给浏览器带 boundary。 */
async function jfetchForm(path, formData) {
  const headers = {};
  if (_token) headers.Authorization = `Bearer ${_token}`;
  const res = await fetch(API_BASE + path, { method: 'POST', body: formData, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = JSON.stringify(await res.json()); } catch (_) { /* ignore */ }
    const error = new Error(`${res.status} ${path}: ${detail}`); error.status = res.status; throw error;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

export const Rest = {
  simulationControl: (state) => jfetch('/api/simulation/control', {method:'POST', body:JSON.stringify(state)}),
  getLayout: () => jfetch('/api/layout'),
  getScenario: () => jfetch('/sim/scenario'),
  // —— 仿真事件上报（HTTP → EventBus）——
  arrived: (vehicle_id, gate, image_path, sim_ts) =>
    jfetch('/sim/events/arrived', { method: 'POST',
      body: JSON.stringify({ vehicle_id, gate, image_path, sim_ts }) }),
  /**
   * 3D 闸口相机截图识别（主项目接入；qa-backend 提供 POST /sim/events/arrived-frame）。
   * multipart: vehicle_id/gate/expected_plate/sim_ts/image；失败时由调用方回退 Rest.arrived 旧图库模式。
   */
  arrivedFrame: ({ vehicle_id, gate, expected_plate = null, sim_ts = null, image }) => {
    const fd = new FormData();
    fd.append('vehicle_id', String(vehicle_id));
    fd.append('gate', gate);
    if (expected_plate) fd.append('expected_plate', String(expected_plate));
    if (sim_ts != null) fd.append('sim_ts', String(sim_ts));
    const name = `sim_${gate}_${String(vehicle_id).replace(/[^A-Za-z0-9_-]/g, '_')}.png`;
    fd.append('image', image, name);
    return jfetchForm('/sim/events/arrived-frame', fd);
  },
  passed: (vehicle_id, gate) =>
    jfetch('/sim/events/passed', { method: 'POST',
      body: JSON.stringify({ vehicle_id, gate }) }),
  left: (vehicle_id, gate) =>
    jfetch('/sim/events/left', { method: 'POST',
      body: JSON.stringify({ vehicle_id, gate }) }),
  zone: (zone_id, occupied, vehicle_id, owner_id = null, session_id = null) =>
    jfetch('/sim/events/zone', { method: 'POST',
      body: JSON.stringify({ zone_id, occupied, vehicle_id, owner_id, session_id }) }),
  barrierAck: (gate, state, cmd_id = null, arm_position = null) =>
    jfetch('/sim/events/barrier_ack', { method: 'POST',
      body: JSON.stringify({ gate, state, cmd_id, arm_position }) }),

  closeBarrier: (gate) => jfetch('/api/manual/close-barrier', {method:'POST',body:JSON.stringify({gate,reason:'scenario_occupied_safety_probe'})}),
  barrierFault: (gate, action = 'inject', mode = 'ack_timeout') =>
    jfetch(`/api/barrier/${gate}/fault`, { method: 'POST', body: JSON.stringify({ action, mode }) }),
  // —— 只读快照 ——
  spaces: (zone) => jfetch('/api/spaces' + (zone ? `?zone=${zone}` : '')),
  overview: () => jfetch('/api/overview'),
  snapshot: () => jfetch('/api/state/snapshot'),
  events: (since) => jfetch(`/api/events?since=${encodeURIComponent(since)}`),
  resetDemo: (request_id, force = false) => jfetch('/api/demo/reset', { method: 'POST',
    body: JSON.stringify({ request_id, mode: 'soft', new_session: true, force: !!force }) }),
  claimDispatcher: (session_id, owner_id) => jfetch('/sim/dispatcher', {method:'POST', body:JSON.stringify({session_id,owner_id})}),
  reportRuntime: (session_id, owner_id, vehicles, scheduler = null) => jfetch('/sim/state', {
    method: 'POST', body: JSON.stringify({ session_id, owner_id, vehicles, scheduler }) }),
  // —— 手动派车（qa-backend：plate-image/dispatch-plan；token 由 jfetch 自动带）——
  simPlateImage: (body = {}) => jfetch('/api/sim/plate-image', {
    method: 'POST', body: JSON.stringify(body) }),
  simDispatchPlan: (body = {}) => jfetch('/api/sim/dispatch-plan', {
    method: 'POST', body: JSON.stringify(body) }),
  /** 黑名单处置物理闸杆：open/close（后端按契约放行/关闭；失败时前端物理兜底）。 */
  blacklistGate: (body = {}) => jfetch('/api/sim/blacklist-gate', {
    method: 'POST', body: JSON.stringify(body) }),
  /** 黑名单 3D 实际路径回报（reversed / entered_then_left）：只写事件/审计。 */
  blacklistResolution: (body = {}) => jfetch('/api/sim/blacklist-resolution', {
    method: 'POST', body: JSON.stringify(body) }),
  adminScenario: () => jfetch('/api/admin/scenario'),
  saveAdminScenario: (body) => jfetch('/api/admin/scenario', {
    method: 'PUT', body: JSON.stringify(body) }),

  // —— 演示终端管理动作（需登录令牌；业务真理仍在后端）——
  login: (username, password) =>
    jfetch('/api/auth/login', { method: 'POST',
      body: JSON.stringify({ username, password }) }),
  pay: (record_id, method = 'qr', amount_override = null) =>
    jfetch('/api/pay', { method: 'POST',
      body: JSON.stringify({ record_id, method, amount_override }) }),
  cameraInject: (gate, reason = 'sim_fault') =>
    jfetch(`/api/camera/${gate}/inject`, { method: 'POST',
      body: JSON.stringify({ reason }) }),
  cameraRecover: (gate, resolution = 'sim_recovered') =>
    jfetch(`/api/camera/${gate}/recover`, { method: 'POST',
      body: JSON.stringify({ resolution }) }),
  manualRegister: (plate, color = 'blue') =>
    jfetch('/api/manual/register', { method: 'POST',
      body: JSON.stringify({ plate, color }) }),
  manualExit: (plate, reason = 'sim') =>
    jfetch('/api/manual/exit', { method: 'POST',
      body: JSON.stringify({ plate, reason }) }),
  manualOpen: (gate, reason = 'sim') =>
    jfetch('/api/manual/open-barrier', { method: 'POST',
      body: JSON.stringify({ gate, reason }) }),
};

/**
 * 实时下行 WebSocket 客户端：自动重连 + 心跳；把消息分发给注册的 handler。
 * 断连期间后端会缓冲事件，重连后以 {type:'sync', events:[...]} 一次性追平。
 */
export class RealtimeClient {
  constructor(onMessage, onStatus) {
    this.onMessage = onMessage;
    this.onStatus = onStatus || (() => {});
    this.ws = null;
    this._closed = false;
    this._retry = 0;
    this._hb = null;
  }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws`;
    this.ws = new WebSocket(url, _token ? [`token.${_token}`] : []);
    this.ws.onopen = () => {
      this._retry = 0;
      this.onStatus(true);
      this._startHeartbeat();
    };
    this.ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === 'sync' && Array.isArray(msg.events)) {
        msg.events.forEach((e) => this.onMessage(e));
      } else {
        this.onMessage(msg);
      }
    };
    this.ws.onclose = () => {
      this.onStatus(false);
      this._stopHeartbeat();
      if (!this._closed) {
        const delay = Math.min(4000, 400 * 2 ** this._retry++);
        setTimeout(() => this.connect(), delay);
      }
    };
    this.ws.onerror = () => { try { this.ws.close(); } catch (_) { /* ignore */ } };
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  // 道闸动画完成回执：既走 WS，也兜底走 REST（双通道，防丢）
  ack(gate, state) {
    this.send({ type: 'barrier_ack', gate, state });
    Rest.barrierAck(gate, state).catch(() => {});
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    this._hb = setInterval(() => this.send({ type: 'ping' }), 5000);
  }
  _stopHeartbeat() { if (this._hb) clearInterval(this._hb); this._hb = null; }

  close() { this._closed = true; this._stopHeartbeat(); try { this.ws.close(); } catch (_) {} }
}
