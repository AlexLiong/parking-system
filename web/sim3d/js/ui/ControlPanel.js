// 控制面板（ui/ControlPanel.js）：把 index.html 底部面板的按钮/滑块/下拉绑定到
// 仿真上下文（ScenarioPlayer / TrafficScheduler / CameraRig / SimApi / HudOverlay）。
// 手动派车走 qa-backend 的 dispatch-plan（plate 图/plate_type/vehicle_kind/duration）。
import {
  PLATE_TYPES, validatePlate, normalizeDispatchPlan, durationToSeconds,
  vehicleKindLabel,
} from './DispatchPolicy.js';
import { loadAuthImage } from './PlateImage.js';
import { normalizeError } from './EventFormatter.js';

const $ = (id) => document.getElementById(id);

export class ControlPanel {
  /**
   * @param {object} ctx {scenario, scheduler, rig, simApi, hud, setPaused, getPaused, setSpeed, onReset}
   */
  constructor(ctx) {
    this.ctx = ctx;
    this._dispatching = false;
    this._resumeScenarioPlaying = false; // 暂停前剧本是否在播放
    this._followId = 'auto';
    this._followMissingNotified = false;
    this._bind();
    this._syncTimer = setInterval(() => this.syncScenarioControls(), 300);
    this.syncScenarioControls();
  }

  _bind() {
    const { scenario, scheduler, rig, simApi, hud, onReset } = this.ctx;

    const play = $('btn-play');
    if (play) play.onclick = async () => {
      if (this._dispatching) return;
      try {
        if (scenario?.finished) { await this._restartScenario(); return; }
        await this.ctx.setPaused(false);
        scenario.play();
        this.syncScenarioControls();
      } catch (e) { hud.log(`恢复失败 ${e}`, 'warn'); }
    };

    const pause = $('btn-pause');
    if (pause) pause.onclick = async () => {
      try {
        if (this.ctx.getPaused()) {
          await this.ctx.setPaused(false);
          // 只恢复“暂停前确实在播放”的剧本；never-played/finished 只恢复仿真，不启动/不重播。
          if (this._resumeScenarioPlaying) scenario?.play?.();
          this._resumeScenarioPlaying = false;
          hud.log('仿真继续');
        } else {
          this._resumeScenarioPlaying = !!scenario?.playing;
          await this.ctx.setPaused(true);
          scenario?.pause?.();
          hud.log('仿真已暂停（可拖拽视角）');
        }
        this.syncScenarioControls();
      } catch (e) { hud.log(`暂停失败 ${e}`, 'warn'); }
    };

    const reset = $('btn-reset');
    if (reset) reset.onclick = async () => {
      reset.disabled = true;
      try {
        if (!simApi.resetDemo) throw new Error('预览模式不支持重置');
        await simApi.resetDemo(true);
        this._resumeScenarioPlaying = false;
        if (onReset) onReset();
        scheduler?.clearAll?.();
        scenario?.reset?.();
        this.syncScenarioControls();
        hud.log('演示已重置（车辆/道闸/时钟/剧本）', 'ok');
      } catch (e) {
        hud.log(`重置失败：${e.message || e}`, 'warn');
      } finally {
        reset.disabled = false;
      }
    };

    const speed = $('speed'), speedVal = $('speed-val');
    if (speed) {
      speed.oninput = () => {
        if (speedVal) speedVal.textContent = `${parseFloat(speed.value).toFixed(2)}×`;
      };
      speed.onchange = async () => {
        const v = parseFloat(speed.value);
        try {
          await this.ctx.setSpeed(v);
          const actual = Number(this.ctx.getSpeed?.() ?? v);
          speed.value = String(actual);
          if (speedVal) speedVal.textContent = `${actual.toFixed(2)}×`;
        } catch (e) {
          const err = normalizeError(e);
          hud.event({ type: 'error', code: err.code || 'speed_failed', message: err.message });
          if (speedVal) speedVal.textContent = `${Number(this.ctx.getSpeed?.() ?? v).toFixed(2)}×`;
        }
      };
    }

    const preset = $('camera-preset');
    const applyPreset = (name) => {
      const target = name === 'follow' ? scheduler?.followCandidate?.() : null;
      rig?.setPreset?.(name, target);
      if (preset) preset.value = name;
      if (name === 'lprIn' || name === 'lprOut') {
        hud.log(`相机视角：${name === 'lprIn' ? '入口' : '出口'} LPR 相机（3D 闸口视角）`, 'ok');
      }
    };
    if (preset) preset.onchange = () => applyPreset(preset.value);

    // 任意跟随：自动 + 当前每辆 vehicles；选择不被 300ms sync 重置。
    const follow = $('follow-vehicle');
    if (follow) follow.onchange = () => {
      this._followId = follow.value || 'auto';
      this._followMissingNotified = false;
      if (this._followId === 'auto') {
        applyPreset('follow');
        hud.log('跟随模式：自动');
      } else {
        const v = scheduler?.vehicles?.get?.(this._followId);
        if (v) {
          rig?.setFollowTarget?.(v);
          if (preset) preset.value = 'follow';
          hud.log(`跟随车辆：${v.meta?.plate || v.vid || v.id}`);
        }
      }
      this._syncFollowOptions();
    };
    this._syncFollowOptions();

    const quickViews = [
      ['btn-cam-overview', 'overview', '全景'],
      ['btn-cam-lpr-in', 'lprIn', '入口相机'],
      ['btn-cam-lpr-out', 'lprOut', '出口相机'],
    ];
    for (const [id, name, label] of quickViews) {
      const btn = $(id);
      if (!btn) continue;
      btn.onclick = () => {
        applyPreset(name);
        hud.log(`切换视角：${label}`);
      };
    }

    // ---------------- 手动派车 messagebox ----------------
    const modal = $('manual-modal');
    const openDispatch = $('btn-open-dispatch');
    const closeDispatch = $('btn-close-dispatch');
    const cancelDispatch = $('btn-cancel-dispatch');
    const closeModal = () => {
      if (!modal) return;
      modal.hidden = true;
      const active = document.activeElement;
      if (active && modal.contains(active)) active.blur();
    };
    const openModal = () => {
      if (!modal) return;
      modal.hidden = false;
      setStatus('');
      plateInput?.focus({ preventScroll: true });
    };
    if (openDispatch) openDispatch.onclick = openModal;
    if (closeDispatch) closeDispatch.onclick = closeModal;
    if (cancelDispatch) cancelDispatch.onclick = closeModal;
    if (modal) {
      modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && modal && !modal.hidden) closeModal();
    });

    const plateInput = $('manual-plate');
    const typeSelect = $('manual-type');
    const durationInput = $('manual-duration');
    const unitSelect = $('manual-duration-unit');
    const dispatchBtn = $('btn-dispatch');
    const status = $('manual-status');
    const setStatus = (text, kind = '') => {
      if (!status) return;
      status.textContent = text || '';
      status.className = `manual-status ${kind}`.trim();
    };
    if (plateInput) {
      plateInput.addEventListener('input', () => {
        const clean = plateInput.value.toUpperCase().replace(/[·.\-\s]/g, '');
        if (plateInput.value !== clean) plateInput.value = clean;
      });
      plateInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') dispatchBtn?.click();
      });
    }
    const dispatch = async () => {
      if (this._dispatching) return;
      const v = validatePlate(plateInput?.value);
      if (!v.ok) { setStatus(v.error, 'warn'); plateInput?.focus(); return; }
      const plateType = typeSelect?.value || 'blue';
      const durationSec = durationToSeconds(durationInput?.value, unitSelect?.value);
      if (!durationSec) { setStatus('请输入有效的停车时长（>0）', 'warn'); return; }
      // 前端前置拒绝：同车牌仍在场/排队时直接提示，不重复请求后端。
      const duplicate = [...(scheduler?.vehicles?.values?.() || [])]
        .some((x) => String(x.meta?.plate || '').replace(/[·\s]/g, '').toUpperCase() === v.plate);
      if (duplicate) {
        const message = `车牌 ${v.plate} 已在场/排队，不能重复派车`;
        setStatus(`派车失败 [duplicate_plate]：${message}`, 'warn');
        hud.event({ type: 'error', code: 'duplicate_plate', message, plate: v.plate });
        return;
      }
      this._dispatching = true;
      if (dispatchBtn) dispatchBtn.disabled = true;
      setStatus('正在请求后端派车方案…', 'busy');
      try {
        if (!simApi?.dispatchPlan) throw new Error('后端未提供 dispatch-plan 接口');
        const raw = await simApi.dispatchPlan({
          plate: v.plate, plate_type: plateType, duration_sec: durationSec,
        });
        let plan = normalizeDispatchPlan(raw, { plate: v.plate, plateType, durationSec });
        if (!plan.imageUrl && simApi?.plateImage) {
          try {
            const imgResp = await simApi.plateImage({ plate: plan.plate, plate_type: plan.plateType });
            plan = normalizeDispatchPlan(imgResp, {
              plate: plan.plate, plateType: plan.plateType, durationSec: plan.durationSec || durationSec,
            });
          } catch (e) {
            setStatus(`车牌图获取失败：${e.message || e}`, 'warn');
          }
        }
        if (!plan.imageUrl) {
          throw new Error('后端未返回 plate 图 URL（dispatch-plan/plate-image）');
        }
        setStatus(`正在加载车牌图 ${plan.plate}…`, 'busy');
        const image = await loadAuthImage(plan.imageUrl);
        const vid = plan.vehicleId || `M${Date.now().toString(36)}${Math.floor(Math.random() * 90 + 10)}`;
        const vehicle = await scheduler.spawnEntry({
          kind: plan.kind,
          model: plan.model,
          plate: plan.plate,
          expectedPlate: plan.plate,
          plateType: plan.plateType,
          duration: plan.durationSec || durationSec,
          vid,
          imageUrl: plan.imageUrl,
        });
        if (!vehicle) throw new Error('派车被调度队列拒绝');
        if (image) vehicle.setPlateImage?.(image, plan.plate, plan.color);
        else vehicle.setPlate?.(plan.plate, plan.color);
        setStatus(`已派车 ${plan.plate} · ${PLATE_TYPES[plan.plateType]?.label || plan.plateType} · ${vehicleKindLabel(plan.vehicleKind)} · 停车 ${plan.durationSec || durationSec}s（仿真）后自动出场`, 'ok');
        hud.event({ type: 'manual_dispatch', plate: plan.plate, plate_type: plan.plateType,
          vehicle_kind: plan.vehicleKind, duration_sec: plan.durationSec || durationSec,
          image_url: plan.imageUrl }, { time: hud.nowLabel() });
        setTimeout(closeModal, 650);
      } catch (e) {
        const err = normalizeError(e, {
          code: e?.status === 409 ? 'duplicate_entry' : (e?.status ? `http_${e.status}` : ''),
          message: String(e?.message || e), plate: v.plate,
        });
        const detail = err.detail ? ` · ${err.detail}` : '';
        const text = `派车失败${err.code ? ` [${err.code}]` : ''}：${err.message}${detail}`;
        setStatus(text, 'warn');
        hud.event({ type: 'error', code: err.code, message: err.message,
          detail: err.detail || null, plate: err.plate || v.plate });
      } finally {
        this._dispatching = false;
        if (dispatchBtn) dispatchBtn.disabled = false;
      }
    };
    if (dispatchBtn) dispatchBtn.onclick = dispatch;

    const bindCam = (faultId, recoverId, gate) => {
      const fault = $(faultId), recover = $(recoverId);
      const label = gate === 'out' ? '出口' : '入口';
      if (fault) fault.onclick = () => {
        simApi.camInject(gate).then(() => hud.log(
          `已注入${label}摄像头故障${gate === 'out' ? '，需人工抬杆放行' : ''}`, 'warn'))
          .catch((e) => hud.log(`注入失败(未登录?) ${e}`, 'warn'));
      };
      if (recover) recover.onclick = () => {
        simApi.camRecover(gate).then(() => hud.log(`${label}摄像头已恢复`, 'ok'))
          .catch((e) => hud.log(`恢复失败(未登录?) ${e}`, 'warn'));
      };
    };
    bindCam('btn-cam-fault', 'btn-cam-recover', 'in');
    bindCam('btn-cam-fault-out', 'btn-cam-recover-out', 'out');
  }

  /** 播放按钮：初始可播放；播放中/未完成禁用；完成后“重播”。 */
  /** 跟随车辆下拉：自动 + 每辆 vehicles；保持用户选择，目标离场时提示并保持最后位置。 */
  _syncFollowOptions() {
    const { scheduler, rig, hud } = this.ctx;
    const sel = $('follow-vehicle');
    if (!sel) return;
    const vehicles = [...(scheduler?.vehicles?.values?.() || [])];
    const stateLabel = (s) => ({
      approach: '驶入', wait_entry: '等待入场', hold_entry: '外场等待', to_space: '入库中',
      parked: '已停放', to_exit: '出库中', wait_exit: '等待出场', leaving: '离场中',
      gone: '已离场', denied_reverse: '拒绝倒车', denied_forward: '拒绝绕行',
    }[s] || s || '未知');
    const rows = vehicles.map((v) => ({
      id: String(v.id),
      label: `${v.meta?.plate || v.vid || v.id} · ${stateLabel(v.state)}`,
    }));
    const selected = this._followId || 'auto';
    const missing = selected !== 'auto' && !rows.some((r) => r.id === selected);
    if (missing) rows.push({ id: selected, label: '目标车辆已离场（保持最后位置）', missing: true });
    const key = rows.map((r) => `${r.id}:${r.label}`).join('|');
    if (key !== this._followOptionsKey) {
      this._followOptionsKey = key;
      sel.innerHTML = '<option value="auto">自动</option>'
        + rows.map((r) => `<option value="${r.id.replace(/"/g, '&quot;')}">${r.label}</option>`).join('');
    }
    sel.value = selected !== 'auto' && (rows.some((r) => r.id === selected)) ? selected : 'auto';
    if (missing) {
      if (rig?.mode === 'follow' || rig?.followTarget) rig?.holdFollowPosition?.();
      if (!this._followMissingNotified) {
        this._followMissingNotified = true;
        hud?.log?.('目标车辆已离场，跟随保持最后位置', 'warn');
      }
    } else {
      this._followMissingNotified = false;
    }
  }

  syncScenarioControls() {
    const { scenario } = this.ctx;
    this._syncFollowOptions();
    const play = $('btn-play'), pause = $('btn-pause');
    if (!scenario || !play) return;
    if (!scenario.loaded || !(scenario.steps?.length)) {
      play.textContent = '▶ 播放剧本';
      play.disabled = true;
      if (pause) { pause.textContent = '⏸ 暂停'; pause.disabled = true; }
      return;
    }
    if (!scenario.ready || scenario.preloadError) {
      play.textContent = '⛔ 车牌图预取失败';
      play.disabled = true;
      if (pause) { pause.textContent = '⏸ 暂停'; pause.disabled = true; }
      return;
    }
    const started = (scenario.clock || 0) > 1e-6 || (scenario.idx || 0) > 0;
    const finished = !!scenario.finished;
    const playing = !!scenario.playing;
    if (finished) {
      play.textContent = '↻ 重播剧本';
      play.disabled = false;
    } else if (playing) {
      play.textContent = '▶ 播放中…';
      play.disabled = true;
    } else if (started) {
      play.textContent = '▶ 播放剧本';
      play.disabled = true;
    } else {
      play.textContent = '▶ 播放剧本';
      play.disabled = false;
    }
    if (pause) {
      const paused = !!this.ctx.getPaused?.();
      pause.textContent = paused ? '▶ 继续' : '⏸ 暂停';
      // 剧本加载/预取完成后暂停始终可用；finished 后也能暂停冻结/继续恢复。
      pause.disabled = false;
    }
  }

  async _restartScenario() {
    const { scenario, simApi, scheduler, hud, onReset } = this.ctx;
    const play = $('btn-play');
    if (play) play.disabled = true;
    try {
      if (simApi?.resetDemo) await simApi.resetDemo();
      else scheduler?.clearAll?.();
      this._resumeScenarioPlaying = false;
      onReset?.();
      scenario?.reset?.();
      await this.ctx.setPaused(false);
      scenario?.play?.();
      hud.event({ type: 'scenario', message: `重播剧本：${scenario?.name || ''}` }, { time: hud.nowLabel() });
    } catch (e) {
      hud.log(`重播失败：${e.message || e}`, 'warn');
    } finally {
      this.syncScenarioControls();
    }
  }
}

