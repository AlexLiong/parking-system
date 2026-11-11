// HUD 覆盖层（ui/HudOverlay.js）：把 WS 事件与仿真状态映射到 index.html 的悬浮面板
//（连接状态 / 时钟 / 余位屏 / 识别·费用弹牌 / 事件日志 / 车辆统计）。纯 DOM，无框架。
import { normalizeEvent, eventSummaryHtml, esc } from './EventFormatter.js';
const $ = (id) => document.getElementById(id);

export class HudOverlay {
  constructor() {
    this.conn = $('conn');
    this.clock = $('clock');
    this.freeTotal = $('free-total');
    this.freeZones = $('free-zones');
    this.recommend = $('recommend');
    this.popups = $('popups');
    this.events = $('events');
    this.stat = $('sim-stat');
    this.loading = $('loading');
    this._clockTimer = null;
    this._serverClockOffsetMs = 0;
    this._timezone = 'Asia/Shanghai';
    this._lastScenarioKey = '';
  }

  hideLoading() { if (this.loading) this.loading.classList.add('hide'); }
  showLoading() { if (this.loading) this.loading.classList.remove('hide'); }

  setConn(online) {
    if (!this.conn) return;
    this.conn.textContent = online ? '● 已连接' : '○ 连接中…';
    this.conn.className = 'pill ' + (online ? 'pill-on' : 'pill-off');
  }

  startClock(readOverview = null) {
    const sync = async () => {
      try {
        if (!readOverview) return;
        const data = await readOverview();
        const serverMs = Date.parse(data.server_time);
        if (Number.isFinite(serverMs)) this._serverClockOffsetMs = serverMs - Date.now();
        if (data.timezone) this._timezone = data.timezone;
      } catch (_) { /* Preview mode may have no backend. */ }
    };
    const tick = () => {
      if (this.clock) {
        this.clock.textContent = this.nowLabel();
      }
    };
    sync();
    tick();
    this._clockTimer = setInterval(tick, 1000);
    this._clockSyncTimer = setInterval(sync, 60000);
  }

  setSimulation(seconds, paused, speed) {
    if (!this.simClock) {
      this.simClock = document.createElement("span");
      this.simClock.id = "sim-clock";
      this.simClock.className = "pill";
      this.clock?.parentElement?.appendChild(this.simClock);
    }
    const value = `仿真 ${seconds.toFixed(1)}s · ${paused ? "已暂停" : `${speed.toFixed(2)}×`}`;
    if (this.simClock.textContent !== value) this.simClock.textContent = value;
  }

  nowLabel() {
    const value = new Date(Date.now() + this._serverClockOffsetMs);
    return value.toLocaleTimeString('zh-CN', { hour12: false, timeZone: this._timezone });
  }

  /** 余位屏：free={total,A,B,C,D}，recommend={blue,green} 或字符串。 */
  setFree(free, recommend) {
    if (this.freeTotal) {
      this.freeTotal.textContent = (free && free.total != null) ? free.total : '--';
    }
    if (this.freeZones) {
      const z = free || {};
      this.freeZones.innerHTML = ['A', 'B', 'C', 'D']
        .map((k) => `<div class="z"><span>${k}区</span><b>${z[k] ?? '-'}</b></div>`)
        .join('');
    }
    if (this.recommend) {
      let txt = '推荐：--';
      if (recommend) {
        if (typeof recommend === 'string') txt = `推荐：${recommend}`;
        else txt = `推荐 蓝:${recommend.blue || '-'} 绿:${recommend.green || '-'}`;
      }
      this.recommend.textContent = txt;
    }
  }

  setStat(counts) {
    if (this.stat) {
      this.stat.textContent = `车辆 ${counts.total} · 在场 ${counts.onLot} · 行驶 ${counts.moving} · 排队 ${counts.queued || 0} · 待派 ${counts.pendingSpawn || 0}`;
    }
  }

  /**
   * 实时事件行：摘要行 + 原生 <details> 可展开详情（点击/键盘均可）。
   * event({type, plate, gate, amount, reason, image_url, ...})；旧 log(text) 仍兼容。
   */
  event(input, extra = {}) {
    if (!this.events) return null;
    const ev = normalizeEvent(input, { ...extra, time: extra.time || this.nowLabel() });
    const row = document.createElement('details');
    row.className = `event-row ${ev.cls || ''}`.trim();
    const summary = document.createElement('summary');
    summary.title = ev.summary;
    summary.innerHTML = `<span class="t">${esc(ev.time)}</span>${eventSummaryHtml(ev)}`;
    // 显式键盘展开：原生 summary 在部分浏览器/输入法下对 Enter/Space 不一致。
    summary.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        row.open = !row.open;
      }
    });
    row.appendChild(summary);
    if (ev.detail.length) {
      const box = document.createElement('div');
      box.className = 'evt-detail';
      for (const f of ev.detail) {
        const line = document.createElement('div');
        line.className = 'evt-field';
        const label = document.createElement('span');
        label.className = 'evt-key';
        label.textContent = f.label;
        const value = document.createElement('span');
        value.className = 'evt-val';
        if (f.image && /^(https?:\/\/|\/)/.test(String(f.value))) {
          const a = document.createElement('a');
          a.href = f.value; a.target = '_blank'; a.rel = 'noopener';
          a.textContent = f.value;
          value.appendChild(a);
          const img = document.createElement('img');
          img.className = 'evt-img'; img.loading = 'lazy'; img.alt = f.label;
          img.src = f.value; img.onerror = () => { img.style.display = 'none'; };
          value.appendChild(img);
        } else {
          value.textContent = String(f.value);
        }
        line.appendChild(label); line.appendChild(value);
        box.appendChild(line);
      }
      row.appendChild(box);
    }
    // 用户停在顶部时保持“最新可见”；主动上滚查看历史时不打断。
    const nearTop = !this.events.scrollTop || this.events.scrollTop < 6;
    this.events.prepend(row);
    if (nearTop) this.events.scrollTop = 0;
    while (this.events.children.length > 80) this.events.removeChild(this.events.lastChild);
    return row;
  }

  /** 旧接口：文本日志 → 单行摘要事件；cls 兼容 warn/ok/fee。 */
  log(text, cls = '', extra = {}) {
    return this.event({ type: extra.type || (cls === 'warn' ? 'warn' : 'info'), message: String(text) },
      { ...extra, time: extra.time || this.nowLabel() });
  }

  /** 剧本进度（紧凑，不抢场景）：名称/描述/当前时间/完成车次/下一事件。 */
  setScenarioProgress(info = null) {
    const box = $('scenario-progress');
    if (!box) return;
    box.hidden = !info;
    if (!info) return;
    const name = info.name || '未加载剧本';
    const total = Number(info.totalDuration) || 0;
    const rawClock = Number(info.clock) || 0;
    const clock = total > 0 ? Math.min(rawClock, total) : rawClock;
    const done = Number(info.completedVehicles) || 0;
    const count = Number(info.totalVehicles) || 0;
    const entered = Number(info.enteredVehicles ?? info.spawnedVehicles) || 0;
    const exited = Number(info.exitedVehicles ?? info.leftVehicles) || 0;
    const forecast = info.nextForecast || '—';
    const key = `${name}|${clock.toFixed(1)}|${done}/${count}|${entered}|${exited}|${info.nextEvent || ''}|${forecast}|${info.playing}|${info.finished}|${info.preloadError || ''}`;
    if (key === this._lastScenarioKey) return;
    this._lastScenarioKey = key;
    const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };
    set('scn-name', name);
    set('scn-state', info.preloadError ? '车牌预取失败'
      : info.finished ? '已完成'
        : info.playing ? '播放中'
          : ((Number(info.clock) || 0) > 1e-6 || (Number(info.idx) || 0) > 0 ? '已暂停' : '待播放'));
    set('scn-desc', String(info.description || '').replace(/\*\*/g, ''));
    set('scn-time', `${clock.toFixed(1)}s / ${total.toFixed(1)}s`);
    set('scn-count', info.preloadError
      ? `预取 ${Number(info.preloadLoaded) || 0}/${Number(info.preloadTotal) || count} 失败`
      : `已入场 ${entered} · 已出场 ${exited} · 总 ${count}`);
    set('scn-next-event', info.preloadError
      ? `错误：${info.preloadError}`
      : `下一事件：${info.nextEvent || '—'}`);
    set('scn-next-forecast', info.preloadError
      ? '预计出场/缴费：—'
      : `预计出场/缴费：${forecast && forecast !== '—' ? forecast : '—'}`);
    const bar = $('scn-bar');
    if (bar) bar.style.width = `${total > 0 ? Math.max(0, Math.min(100, clock / total * 100)) : 0}%`;
  }

  /** 弹牌（识别 / 费用 / 警告），若干秒后自动淡出移除。 */
  popup(html, cls = '', ttl = 5200) {
    if (!this.popups) return;
    const el = document.createElement('div');
    el.className = 'popup ' + cls;
    el.innerHTML = html;
    this.popups.prepend(el);
    while (this.popups.children.length > 4) this.popups.removeChild(this.popups.lastChild);
    setTimeout(() => {
      el.style.transition = 'opacity .5s'; el.style.opacity = '0';
      setTimeout(() => el.remove(), 520);
    }, ttl);
  }

  /**
   * 识别输入图片卡片（主项目：3D 闸口相机截图）：展示本次识别实际使用的图片、
   * 车牌裁剪图、置信度、期望 3D 车牌与匹配结果。与 3D 车身车牌渲染区分。
   * @param {object} data plate_recognized / camera_frame WS 消息
   * @param {string|null} token 3D 端演示/登录令牌；/datasets、/media 需鉴权时用 fetch 取 blob
   */
  setRecognitionInput(data, token = null) {
    const card = $('recognition-input');
    if (!card) return;
    if (!data) { card.hidden = true; return; }
    card.hidden = false;
    const gate = data.gate === 'out' ? '出口' : '入口';
    const pending = data.pending === true || data.plate === '识别中…';
    const plate = data.plate || '未识别';
    const conf = Number(data.conf != null ? data.conf : (data.overall_conf || 0));
    const url = data.image_url || this._inferImageUrl(data.image_path);
    const source = data.image_source || this._inferImageSource(url);
    const synthetic = data.image_synthetic === true ? '是'
      : data.image_synthetic === false ? '否' : '未知';
    const expected = data.expected_plate || '';
    const matched = data.matched != null ? data.matched
      : (expected && plate && !pending ? this._canonPlate(expected) === this._canonPlate(plate) : null);
    const matchText = expected
      ? (matched === true ? '<span style="color:#5ee08a">✓ 一致</span>'
        : matched === false ? '<span style="color:#ff8a8a">✗ 不一致</span>' : '待识别')
      : '—';
    const gateEl = $('rec-input-gate');
    if (gateEl) {
      gateEl.textContent = `${gate}识别输入`;
      gateEl.className = `recog-badge${data.gate === 'out' ? ' out' : ''}`;
    }
    const srcEl = $('rec-input-source');
    if (srcEl) srcEl.textContent = source || '来源未知';
    const meta = $('rec-input-meta');
    if (meta) {
      const confText = (pending || !Number.isFinite(conf) || conf <= 0) ? '—' : conf.toFixed(2);
      meta.innerHTML = `车牌 <b>${this._esc(plate)}</b> · 置信度 ${confText}`
        + `<br>期望 3D 车牌：<b>${this._esc(expected || '—')}</b> · 匹配：${matchText}`
        + `<br>来源：${this._esc(source || '未知')} · 合成：${synthetic}`
        + (data.frame_size ? ` · 截图 ${this._esc(data.frame_size)}` : '')
        + (data.camera_capture_ms != null ? ` · 捕获 ${Number(data.camera_capture_ms).toFixed(0)} ms` : '')
        + (data.need_manual ? '<br><span style="color:#ffd479">待人工确认</span>' : '')
        + (data.message ? `<br>${this._esc(data.message)}` : '');
    }
    const img = $('rec-input-img');
    const crop = $('rec-input-crop');
    if (crop) {
      if (data.plate_crop_url) {
        crop.hidden = false;
        this._loadRecognitionImage(data.plate_crop_url, token, crop);
      } else {
        crop.hidden = true; crop.removeAttribute('src');
      }
    }
    this._loadRecognitionImage(url, token, img);
  }

  _canonPlate(s) {
    return String(s || '').replace(/[·\s]/g, '').toUpperCase();
  }

  _inferImageUrl(raw) {
    if (!raw) return '';
    const s = String(raw).replace(/\\/g, '/');
    if (/^https?:\/\//i.test(s)) return s;
    if (s.startsWith('/datasets/') || s.startsWith('/media/')) return s;
    let i = s.indexOf('/datasets/');
    if (i >= 0) return s.slice(i);
    i = s.indexOf('/photos/');
    if (i >= 0) return '/media/' + s.slice(i + '/photos/'.length);
    if (s.startsWith('datasets/')) return '/' + s;
    if (s.startsWith('crops/') || s.startsWith('uploads/')) return '/media/' + s;
    return s;
  }

  _inferImageSource(url) {
    if (!url) return '';
    if (String(url).includes('/datasets/demo-lpr/')) return 'demo-lpr 合成演示样例';
    if (String(url).includes('/media/frames/')) return '3D 闸口相机截图（仿真）';
    if (String(url).startsWith('/datasets/')) return 'datasets 演示图库';
    if (String(url).includes('/media/uploads/')) return '上传图片';
    if (String(url).startsWith('/media/')) return '服务端照片';
    return '未知来源';
  }

  async _loadRecognitionImage(url, token, img) {
    if (!img) return;
    if (!url) { img.hidden = true; img.removeAttribute('src'); return; }
    img.hidden = false;
    if (this._recogObjectUrl) { URL.revokeObjectURL(this._recogObjectUrl); this._recogObjectUrl = null; }
    try {
      // /datasets、/media 受后端鉴权保护；<img> 无法带 Authorization，改用 fetch+blob。
      if (token && url.startsWith('/')) {
        const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` }, cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        this._recogObjectUrl = URL.createObjectURL(blob);
        img.src = this._recogObjectUrl;
      } else {
        img.src = url;
      }
    } catch (_) {
      // 回退直连（同源 cookie 有效时仍可显示）；失败则隐藏图片但保留文字说明。
      img.onerror = () => { img.hidden = true; };
      img.src = url;
    }
  }

  popupRecognized({ plate, color, conf, need_manual, gate, message }) {
    const dir = gate === 'out' ? '出口' : '入口';
    if (need_manual) {
      const detail = message ? this._esc(message) : '请人工确认';
      this.popup(`⚠ ${dir}${detail}<br><small>${this._esc(plate || '无车牌')} · 置信度 ${Number(conf || 0).toFixed(2)}</small>`, 'warn', 7000);
    } else {
      const c = color === 'green' ? '绿牌' : '蓝牌';
      this.popup(`🔎 ${dir}识别 <b>${this._esc(plate || '')}</b><br><small>${c} · 置信度 ${(conf || 0).toFixed(2)}</small>`, 'ok');
    }
  }

  popupFee({ plate, amount, minutes }) {
    this.popup(`💰 出口应缴 <b>¥${Number(amount || 0).toFixed(2)}</b><br><small>${this._esc(plate || '')} · 停车 ${minutes ?? '-'} 分钟</small>`, 'fee', 9000);
  }

  _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
}
