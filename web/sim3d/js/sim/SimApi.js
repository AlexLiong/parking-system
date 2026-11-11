// 仿真端 API 门面（sim/SimApi.js）：统一封装 REST 上报 / 管理动作 / WebSocket 下行 /
// 演示终端会话。上层（main / ScenarioPlayer / ControlPanel）只依赖此门面，
// 不直接触碰传输细节（低耦合）。
import { Rest, getToken, setToken } from '../api.js';
import { SyncClient } from '../../../common/SyncClient.js';

export class SimApi {
  constructor(onMessage, onStatus, onSnapshot) {
    this.rest = Rest;
    this.ownerId = sessionStorage.getItem('sim-owner-id') || crypto.randomUUID();
    sessionStorage.setItem('sim-owner-id', this.ownerId);
    this.rt = new SyncClient({ token: getToken, snapshot: Rest.snapshot,
      events: Rest.events, onEvent: onMessage, onStatus, onSnapshot });
    this.authed = false;
  }

  /** 演示终端自动登录（获取令牌，供缴费/摄像头注入/人工放行）。离线失败静默降级。 */
  async ensureSession() {
    try {
      const existing = localStorage.getItem('parking.admin.token');
      let r;
      if (existing) { setToken(existing); await Rest.overview(); r = {token:existing}; }
      else {
        const response = await fetch('/api/auth/demo-session',{method:'POST'});
        if (!response.ok) throw new Error('请先登录管理端');
        r = await response.json();
      }
      setToken(r.token);
      this.authed = true;
      return true;
    } catch (_) {
      this.authed = false;
      return false;
    }
  }

  connect() { return this.rt.start(); }
  close() { this.rt.close(); }
  ack(gate, state, cmd_id = null, arm_position = null) {
    this.rt.send({ type: 'barrier_ack', gate, state, cmd_id, arm_position });
    Rest.barrierAck(gate, state, cmd_id, arm_position).catch(() => {});
  }
  async resetDemo(force = true) {
    const result = await Rest.resetDemo(crypto.randomUUID(), force);
    await this.rt.applySnapshot(result.snapshot);
    return result;
  }
  claimDispatcher() { return Rest.claimDispatcher(this.rt.sessionId,this.ownerId); }
  reportRuntime(vehicles, scheduler = null) {
    const sessionId = this.rt.sessionId;
    if (!sessionId) return Promise.resolve();
    return Rest.reportRuntime(sessionId, this.ownerId, vehicles, scheduler);
  }

  // —— 管理动作（需会话）——
  pay(recordId) { return Rest.pay(recordId, 'mock_scan'); }
  /** 手动派车：后端生成 plate 图/车型/停车时长。 */
  dispatchPlan(payload) { return Rest.simDispatchPlan(payload); }
  plateImage(payload) { return Rest.simPlateImage(payload); }
  camInject(gate) { return Rest.cameraInject(gate, 'sim_manual'); }
  camRecover(gate) { return Rest.cameraRecover(gate, 'sim_manual'); }
  manualRegister(plate, color = 'blue') { return Rest.manualRegister(plate, color); }
  manualExit(plate) { return Rest.manualExit(plate, 'sim_manual'); }
  manualOpen(gate) { return Rest.manualOpen(gate, 'sim_manual'); }

  // —— 只读快照 ——
  layout() { return Rest.getLayout(); }
  scenario() { return Rest.getScenario(); }
  spaces() { return Rest.spaces(); }
  overview() { return Rest.overview(); }
}
