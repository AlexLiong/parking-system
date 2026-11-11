// 实时监控视图：总览卡片 + 闸口/摄像头状态 + 分区余位 + WS 实时事件流 + 人工干预。
// 只读展示 + 管理动作（均需登录令牌）；业务真理在后端，本端不自行决定开闸。
import {
  el, clear, toast, promptBox, confirmBox, fmtTime, fmtMoney, clockNow,
  loadErrorCard, store,
  syncBusinessClock,
  barrierState, barrierIsOpen, incidentKind, plateClass, escapeHtml, mediaUrl,
} from '../util.js';
import { Api } from '../api.js';
import { RealtimeClient } from '../ws.js';
import { normalizeEvent, eventView, unknownSummary } from '../event-view.js';
import {
  entryValueFromRecord, buildCorrectionPayload, validateCorrection, settlementSummary,
} from '../exit-correction.js';

const KIND_META = {
  plate_recognized: { cls: 'rec', label: '车牌识别' },
  fee_quote: { cls: 'fee', label: '出口报价' },
  barrier_cmd: { cls: 'bar', label: '道闸指令' },
  barrier_state: { cls: 'bar', label: '道闸状态' },
  space_update: { cls: 'sp', label: '余位更新' },
  space_assigned: { cls: 'sp', label: '车位分配' },
  layout_changed: { cls: 'sp', label: '布局变更' },
  incident: { cls: 'inc', label: '异常' },
  incident_resolved: { cls: 'sp', label: '异常处理' },
  demo_reset: { cls: 'bar', label: '演示复位' },
  pending_created: { cls: 'inc', label: '待人工确认' },
  pending_resolved: { cls: 'sp', label: '人工确认结果' },
  manual_authorized: { cls: 'sp', label: '人工授权进场' },
  exit_authorized: { cls: 'sp', label: '人工授权出场' },
  entry_denied: { cls: 'inc', label: '拒绝进场' },
  payment_completed: { cls: 'fee', label: '缴费完成' },
  arrival_retry_requested: { cls: 'inc', label: '待办释放重试' },
};

export default {
  title: '实时监控',
  icon: '📡',
  async render(root, ctx) {
    let timer = null;
    let ws = null;

    // ---------- 概览卡片区 ----------
    const cFree = statCard('accent', '空闲车位', '—', '');
    const cOpen = statCard('brand', '在场车辆', '—', '');
    const cInc = statCard('danger', '未闭环异常', '—', '');
    const cVer = statCard('', '布局版本', '—', '');
    const statsRow = el('div', { class: 'grid cols-4' }, [cFree.node, cOpen.node, cInc.node, cVer.node]);

    // ---------- 闸口 / 摄像头 ----------
    const devBody = el('div', {});
    const devCard = el('div', { class: 'card' }, [
      el('h3', {}, ['闸口 / 摄像头', el('span', { class: 'spacer' }),
        el('span', { class: 'small muted', id: 'dev-ts', text: '' })]),
      devBody,
    ]);

    // ---------- 分区余位 ----------
    const zoneBody = el('div', { class: 'zone-grid' });
    const recommend = el('div', { class: 'small muted mt', text: '推荐车位：—' });
    const zoneCard = el('div', { class: 'card' }, [
      el('h3', { text: '分区余位' }), zoneBody, recommend,
    ]);

    // ---------- 实时事件流 ----------
    const feed = el('div', { class: 'feed' });
    const connDot = el('span', { class: 'dot off' });
    const feedCard = el('div', { class: 'card' }, [
      el('h3', {}, ['实时事件流', el('span', { class: 'spacer' }),
        el('span', { class: 'row', style: { gap: '6px' } }, [connDot,
          el('span', { class: 'small muted', id: 'conn-txt', text: '连接中' }),
          el('button', { class: 'btn sm ghost', text: '清空', onclick: () => clear(feed) })])]),
      feed,
    ]);

    // ---------- 待人工确认 / 设备控制 / 演示重置 ----------
    const pendingBody = el('div', {});
    // 手工确认输入保护：轮询/WS 到达时保留草稿值、焦点、选区与 IME composition。
    const pendingDrafts = new Map();
    const pendingCorrections = new Map();
    let pendingSignature = '';
    let pendingComposing = false;
    pendingBody.addEventListener('compositionstart', () => { pendingComposing = true; });
    pendingBody.addEventListener('compositionend', () => {
      pendingComposing = false;
      queueMicrotask(() => loadPending());
    });
    const pendingCard = el('div', { id: 'pending-card', class: 'card', hidden: true }, [
      el('h3', { text: '待人工确认' }), pendingBody,
    ]);
    const deviceControlCard = buildDeviceControlCard(async () => {
      await loadOverview();
      await loadPending();
    });
    const resetCard = buildResetCard(async () => {
      await loadOverview();
      await loadPending();
    });

    // ---------- 识别输入 ----------
    const recogImg = el('img', {
      class: 'recog-img', hidden: true,
      alt: '识别输入图片',
    });
    const recogMeta = el('div', { class: 'small recog-meta', text: '尚未收到识别事件' });
    const recogGate = el('span', { class: 'badge brand recog-badge', text: '识别输入' });
    const recogCard = el('div', { class: 'card recog-card' }, [
      el('h3', {}, ['识别输入', el('span', { class: 'spacer' }), recogGate]),
      recogImg,
      recogMeta,
    ]);

    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '实时监控' }),
      el('span', { class: 'sub', text: '总览 · 设备 · 实时事件' }),
    ]));
    root.appendChild(statsRow);
    const overviewError = el('div', {});
    root.appendChild(overviewError);
    root.appendChild(el('div', { class: 'grid cols-2' }, [devCard, zoneCard]));
    root.appendChild(deviceControlCard);
    root.appendChild(recogCard);
    root.appendChild(pendingCard);
    if ((store.getUser()?.role || 'admin') === 'admin') root.appendChild(resetCard);
    root.appendChild(feedCard);

    // ---------- 数据加载 ----------
    async function loadOverview() {
      try {
        const o = await Api.overview();
        syncBusinessClock(o.server_time, o.timezone);
        clear(overviewError);
        cFree.set(o.free_total, `${Object.keys(o.free_by_zone || {}).length} 个分区`);
        cOpen.set(o.open_records, '当前在场');
        cInc.set(o.open_incidents, o.open_incidents > 0 ? '需处理' : '运行正常');
        cVer.set('v' + (o.layout_version ?? 0), fmtTime(o.server_time, { utcTag: true }));
        renderDevices(devBody, o);
        renderZones(zoneBody, o);
        const rec = o.recommend || {};
        recommend.textContent = `推荐车位：蓝牌 ${rec.blue || '—'} · 绿牌(新能源) ${rec.green || '—'}`;
      } catch (e) {
        clear(overviewError);
        overviewError.appendChild(loadErrorCard(e.message || '加载失败', () => loadOverview(),
          { title: '监控数据加载失败' }));
        cFree.set('—', '加载失败');
      }
    }

    async function loadPending() {
      // IME composition 期间绝不重建 DOM（中文/日文输入最容易在 composition 中被刷新打断）。
      if (pendingComposing) return;
      try {
        const response = await Api.manualPending();
        const items = response.items || [];
        pendingCard.hidden = !items.length;
        const signature = JSON.stringify(items.map((it) => [
          it.id, it.status, it.gate, it.reason, it.source_plate, it.message,
          it.image_path, it.candidates, it.attempts,
        ]));
        const active = document.activeElement;
        const focus = active && pendingBody.contains(active) && active.matches?.('input')
          ? { id: active.dataset.pendingId, start: active.selectionStart, end: active.selectionEnd }
          : null;
        // 数据未变时不做无意义重建；用户正在输入时尤其不能清掉输入/焦点。
        if (signature === pendingSignature) return;
        pendingSignature = signature;
        renderPending(pendingBody, items, loadPending, { drafts: pendingDrafts, focus, corrections: pendingCorrections });
      } catch (e) {
        clear(pendingBody);
        pendingCard.hidden = false;
        pendingBody.appendChild(loadErrorCard(e.message || '待办加载失败', () => loadPending(),
          { title: '待确认列表加载失败' }));
      }
    }

    function renderRecognitionInput(raw) {
      const m = normalizeEvent(raw);
      if (!m || m.type !== 'plate_recognized') return;
      const gate = m.gate === 'out' ? '出口' : '入口';
      const conf = Number(m.conf != null ? m.conf : (m.overall_conf || 0));
      const url = m.image_url || mediaUrl(m.image_path);
      recogGate.textContent = `${gate}识别输入`;
      recogGate.className = `badge ${gate === '出口' ? 'warn' : 'brand'} recog-badge`;
      recogMeta.innerHTML = `车牌 <b>${escapeHtml(m.plate || '未识别')}</b> · 置信度 ${conf.toFixed(2)}`
        + (m.need_manual ? ' · <span class="badge warn">待人工确认</span>' : '')
        + (m.message ? `<br>${escapeHtml(m.message)}` : '');
      if (url) {
        recogImg.hidden = false;
        recogImg.onerror = () => { recogImg.hidden = true; };
        recogImg.src = url;
      } else {
        recogImg.hidden = true;
      }
    }

    await loadOverview();
    await loadPending();
    timer = setInterval(() => { loadOverview(); loadPending(); }, 4000);

    // ---------- WS ----------
    ws = new RealtimeClient(
      (raw) => {
        const m = normalizeEvent(raw);
        pushFeed(feed, m);
        if (m.type === 'plate_recognized') renderRecognitionInput(m);
        if (m.type === 'pending_created' || m.type === 'pending_resolved') loadPending();
        if (m.type === 'manual_authorized' || m.type === 'exit_authorized' || m.type === 'entry_denied') {
          loadOverview();
          loadPending();
        }
        if (m.type === 'demo_reset') {
          recogImg.hidden = true;
          recogMeta.textContent = '尚未收到识别事件';
          recogGate.textContent = '识别输入';
          recogGate.className = 'badge brand recog-badge';
        }
      },
      (online) => {
        connDot.className = `dot ${online ? 'on' : 'off'}`;
        const t = feedCard.querySelector('#conn-txt');
        if (t) t.textContent = online ? '实时在线' : '连接断开';
      },
      async () => {
        await loadOverview();
        await loadPending();
        const history = await Api.recentEvents();
        clear(feed);
        let lastRecognition = null;
        for (const item of history.items || []) {
          const event = normalizeEvent(item.data);
          pushFeed(feed, event);
          if (event?.type === 'demo_reset') lastRecognition = null;
          else if (event?.type === 'plate_recognized') lastRecognition = event;
        }
        if (lastRecognition) renderRecognitionInput(lastRecognition);
        else {
          recogImg.hidden = true;
          recogMeta.textContent = '尚未收到识别事件';
          recogGate.textContent = '识别输入';
          recogGate.className = 'badge brand recog-badge';
        }
      },
    );
    ws.connect().catch((e) => toast(`状态同步失败：${e.message || e}`, 'danger'));

    return () => { if (timer) clearInterval(timer); if (ws) ws.close(); };
  },
};

// ----------------------------- 子构件 -----------------------------
function statCard(accent, label, value, foot) {
  const v = el('div', { class: 'value', text: value });
  const f = el('div', { class: 'foot', text: foot });
  const node = el('div', { class: `stat ${accent}` }, [
    el('div', { class: 'label', text: label }), v, f,
  ]);
  return { node, set: (val, footTxt) => { v.textContent = val; if (footTxt !== undefined) f.textContent = footTxt; } };
}

function badge(cls, text) { return el('span', { class: `badge ${cls}`, text }); }

function renderDevices(body, o) {
  clear(body);
  const bar = o.barriers || {};
  const cam = o.camera || {};
  const row = (name, inState, outState, renderState) => el('div', { class: 'row', style: { justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--line-2)' } }, [
    el('span', { class: 'small', text: name }),
    el('span', { class: 'row', style: { gap: '8px' } }, [
      el('span', { class: 'small muted', text: '入口' }), renderState(inState),
      el('span', { class: 'small muted', text: '出口' }), renderState(outState),
    ]),
  ]);
  body.appendChild(row('道闸状态', bar.in, bar.out, (s) => {
    const k = String(s || '').toLowerCase();
    const cls = k === 'fault' ? 'danger' : barrierIsOpen(s) ? 'ok' : 'muted';
    return badge(cls, barrierState(s));
  }));
  body.appendChild(row('摄像头', cam.in, cam.out, (up) => {
    // watchdog.status 返回 bool（true=在线）或对象
    const online = up === true || (up && up.online) || (up && up.status === 'ok');
    return badge(online ? 'ok' : 'danger', online ? '在线' : '故障');
  }));
}

function renderZones(body, o) {
  clear(body);
  const free = o.free_by_zone || {};
  const total = o.zones_total || {};
  const keys = Object.keys(total).length ? Object.keys(total) : Object.keys(free);
  if (!keys.length) { body.appendChild(el('div', { class: 'small muted', text: '暂无分区数据' })); return; }
  keys.sort().forEach((z) => {
    const f = free[z] ?? 0, t = total[z] ?? 0;
    body.appendChild(el('div', { class: 'zone-cell' }, [
      el('div', { class: 'z', text: `${z} 区` }),
      el('div', { class: 'n', text: String(f) }),
      el('div', { class: 'd', text: `空闲 / 共 ${t}` }),
    ]));
  });
}

function pushFeed(feed, raw) {
  const m = normalizeEvent(raw);
  if (!m || m.type === 'welcome') return;  // 连接欢迎帧不是业务事件
  const view = eventView(m);
  const meta = KIND_META[m.type] || { cls: '', label: view.label };
  const details = view.fields.length ? el('details', { class: 'feed-details' }, [
    el('summary', { text: '详情' }),
    el('div', { class: 'feed-fields' }, view.fields.slice(0, 20).flatMap(([key, value]) => [
      el('span', { class: 'feed-key', text: key }),
      el('span', { class: 'feed-value', text: value }),
    ])),
  ]) : null;
  const item = el('div', { class: `feed-item ${meta.cls}` }, [
    el('span', { class: 'ts', text: clockNow() }),
    el('span', { class: 'kind', text: meta.label }),
    el('div', { class: 'feed-main' }, [
      el('div', { class: 'small', html: describeMsg(m) }),
      details,
    ]),
  ]);
  feed.prepend(item);
  while (feed.children.length > 80) feed.removeChild(feed.lastChild);
}

function describeMsg(raw) {
  const m = normalizeEvent(raw);
  const e = escapeHtml;
  const fields = eventView(m).fields;
  const byKey = Object.fromEntries(fields);
  const errorCode = byKey.code || (m.error && (m.error.code || m.error));
  const errorMessage = byKey.message || m.message;
  const errorPlate = byKey.plate || m.plate;
  switch (m.type) {
    case 'plate_recognized':
      return `<span class="tag-plate ${plateClass(m.color)}">${e(m.plate || '未识别')}</span> ` +
        `[${e(m.gate)}] 置信 ${Number(m.conf || 0).toFixed(2)}` +
        (m.need_manual ? ' <span class="badge warn">待人工</span>' : '') +
        (m.message ? ` · ${e(m.message)}` : '') +
        (m.reason ? ` <code>${e(m.reason)}</code>` : '');
    case 'fee_quote':
      return `<b>${e(m.plate || '')}</b> 应缴 ${fmtMoney(m.amount)}（${Math.round(m.minutes || 0)} 分钟）`;
    case 'barrier_cmd':
      return `[${e(m.gate)}] ${e(m.cmd)}${m.reason ? ' · ' + e(m.reason) : ''}`;
    case 'barrier_state':
      return `[${e(m.gate)}] → ${e(barrierState(m.state))}`;
    case 'space_update':
      return `空闲 ${m.free?.total ?? '—'} · 推荐 ${e(m.recommend || '—')}`;
    case 'space_assigned':
      return `<b>${e(m.plate || m.vehicle_id || '')}</b> → ${e(m.space_no || '')}（${e(m.zone || '')}区）`;
    case 'layout_changed':
      return `v${m.version} ${e((m.diff && m.diff.summary) || '')}`;
    case 'incident':
      return `<span class="badge danger">${e(incidentKind(m.kind))}</span> ${e(m.gate || '')} #${m.incident_id ?? ''}`;
    case 'incident_resolved':
      return `#${m.incident_id ?? ''} ${e(incidentKind(m.kind))} 已处理`;
    case 'demo_reset':
      return `已关闭会话 ${e(m.closed_session || '')}，释放 ${m.reset_report?.released_spaces ?? 0} 个车位`;
    case 'pending_created':
      return `#${m.pending_id ?? m.id ?? ''} [${e(m.gate)}] ${e(m.source_plate || '未识别')} · ${e(m.reason || '')}`;
    case 'pending_resolved':
      return `#${m.pending_id ?? m.id ?? ''} [${e(m.gate)}] ${e(m.plate || '')} · ${e(m.status || '')}`;
    case 'manual_authorized':
      return `人工授权进场 ${e(m.plate || '')} → ${e(m.space_no || '')}（${e(m.gate || 'in')}）`;
    case 'exit_authorized':
      return `人工授权出场 ${e(m.plate || '')} · ${e(m.action || 'charge')}`
        + (m.amount_paid != null ? ` · ¥${Number(m.amount_paid).toFixed(2)}` : '');
    case 'entry_denied':
      return `拒绝入场 ${e(m.plate || '')} · ${e(m.reason || m.message || '')}`;
    case 'arrival_retry_requested':
      return `待办释放，允许重试 · ${e(m.reason || m.message || '')}`;
    case 'payment_completed':
      return `<b>${e(m.plate || '')}</b> 缴费 ${m.method ? e(m.method) + ' · ' : ''}${fmtMoney(m.amount ?? m.amount_paid)}`;
    default: {
      // 未知/错误事件：只显示可读类型、code/message/detail/plate，不输出裸 JSON。
      const label = eventView(m).label;
      if (errorCode || errorMessage || errorPlate) {
        return `<span class="badge danger">错误</span> ${e(label)}`
          + (errorCode ? ` · <code>${e(String(errorCode))}</code>` : '')
          + (errorMessage ? ` · ${e(String(errorMessage))}` : '')
          + (errorPlate ? ` · ${e(String(errorPlate))}` : '');
      }
      const summary = unknownSummary(m);
      return `<span class="muted">${e(label)}</span>${summary ? ` · ${e(summary)}` : ''}`;
    }
  }
}

function renderPending(body, items, refresh, { drafts = new Map(), focus = null, corrections = new Map() } = {}) {
  clear(body);
  if (!items.length) return;
  items.forEach((item) => {
    const inputId = String(item.id);
    const saved = corrections.get(inputId) || {};
    const image = item.image_path ? el('img', {
      src: mediaUrl(item.image_path), alt: '待确认车辆原图', loading: 'lazy',
      style: { maxWidth: '220px', maxHeight: '130px', objectFit: 'contain' },
      onerror: (event) => { event.target.style.display = 'none'; },
    }) : null;
    const plateInput = el('input', {
      value: drafts.has(inputId) ? drafts.get(inputId) : (item.source_plate || ''),
      placeholder: '输入 7/8 位车牌',
      style: { width: '160px', maxWidth: '100%' },
      dataset: { pendingId: inputId },
    });
    const isExit = item.gate === 'out';
    const noRecord = item.reason === 'exit_without_record';
    const choices = el('div', { class: 'btn-row mt' }, (item.candidates || []).map((plate) =>
      el('button', { class: 'btn sm ghost', text: plate,
        onclick: () => {
          plateInput.value = plate;
          drafts.set(inputId, plate);
          if (!correctPlate.value.trim()) correctPlate.value = plate;
          plateInput.focus();
          // 程序赋值不会派发 input；候选选择必须主动重算报价。
          queueMicrotask(refreshQuote);
        } })));
    const messageLine = el('div', { class: 'small mt', style: { overflowWrap: 'anywhere' },
      text: [item.message || item.reason, item.attempts != null ? `重试 ${item.attempts}` : '']
        .filter(Boolean).join(' · ') });
    const feeBox = isExit
      ? el('div', { class: 'small pending-fee mt', role: 'status', text: '正常费用：查询中…' })
      : null;

    // ---- 出口异常纠正 UI（仅出口待办展示；正常记录直接“确认”自动结算） ----
    const correctionBox = el('div', { id: `exit-correction-${inputId}`, class: 'exit-correction mt', hidden: true });
    const recordSel = el('select', { id: `correction-record-${inputId}`, 'aria-label': '选择当前开放记录' }, [
      el('option', { value: '', text: '选择当前开放记录' }),
    ]);
    const correctPlate = el('input', {
      id: `correction-plate-${inputId}`,
      value: saved.plate || item.source_plate || '', maxlength: '8', autocomplete: 'off',
      'aria-label': '正确车牌', placeholder: '正确车牌',
    });
    const unitSel = el('select', { id: `correction-unit-${inputId}`, 'aria-label': '入场时间单位' }, [
      el('option', { value: 'min', text: '分钟' }),
      el('option', { value: 'sec', text: '秒' }),
    ]);
    unitSel.value = saved.unit || 'min';
    const entryValue = el('input', {
      id: `correction-entry-${inputId}`,
      type: 'number', min: '0', step: '0.01', value: saved.value || '',
      'aria-label': '正确入场时间', placeholder: '入场时间',
    });
    const reasonInput = el('input', {
      value: saved.reason || '', maxlength: '200', 'aria-label': '免费或作废原因', placeholder: '免费/作废原因',
    });
    const correctionStatus = el('div', { class: 'small mt', role: 'status', 'aria-live': 'polite' });
    const correctionAuto = el('button', { id: `correction-auto-${inputId}`, class: 'btn sm ok', type: 'button', text: '确认纠正' });
    const correctionFree = el('button', { id: `correction-free-${inputId}`, class: 'btn sm warn', type: 'button', text: '免费放行' });
    const correctionVoid = el('button', { id: `correction-void-${inputId}`, class: 'btn sm danger', type: 'button', text: '作废' });
    let openRecords = [];
    let openLoaded = false;
    let correctionVisible = saved.visible === true || noRecord;
    let quoteSeq = 0;
    let quoteTimer = null;

    const saveCorrection = () => corrections.set(inputId, {
      visible: correctionVisible,
      recordId: recordSel.value,
      plate: correctPlate.value,
      value: entryValue.value,
      unit: unitSel.value,
      reason: reasonInput.value,
    });

    function selectedRecord() {
      return openRecords.find((r) => String(r.id) === String(recordSel.value)) || null;
    }

    function applyRecordDefaults() {
      const record = selectedRecord();
      if (!record) { saveCorrection(); return; }
      correctPlate.value = record.plate || correctPlate.value;
      entryValue.value = entryValueFromRecord(record, unitSel.value) || entryValue.value;
      saveCorrection();
    }

    function renderRecordOptions() {
      clear(recordSel);
      recordSel.appendChild(el('option', { value: '', text: '选择当前开放记录' }));
      openRecords.forEach((record) => {
        recordSel.appendChild(el('option', {
          value: String(record.id),
          text: `${record.plate || '—'} · ${record.space_no || '—'}${record.entry_sim_sec != null ? ` · sim ${Number(record.entry_sim_sec).toFixed(0)}s` : ''}`,
          dataset: { plate: record.plate || '', entry: record.entry_sim_sec == null ? '' : String(record.entry_sim_sec) },
        }));
      });
      if (saved.recordId && openRecords.some((r) => String(r.id) === String(saved.recordId))) {
        recordSel.value = String(saved.recordId);
      } else {
        const wanted = String(plateInput.value || '').trim().toUpperCase();
        const match = openRecords.find((r) => String(r.plate || '').toUpperCase() === wanted);
        if (match) recordSel.value = String(match.id);
      }
      if (saved.value) entryValue.value = saved.value;
      if (saved.unit) unitSel.value = saved.unit;
      if (!correctPlate.value.trim() && recordSel.value) correctPlate.value = selectedRecord()?.plate || '';
      if (!saved.value) applyRecordDefaults();
      else saveCorrection();
    }

    async function loadOpenRecords() {
      if (!isExit) return;
      if (openLoaded) return;
      openLoaded = true;
      try {
        const result = await Api.records({ status: 'in', page_size: 100 });
        openRecords = Array.isArray(result?.items) ? result.items : [];
        renderRecordOptions();
        correctionAuto.disabled = !openRecords.length;
        if (!openRecords.length) correctionStatus.textContent = '当前无开放记录，可使用免费放行或作废';
      } catch (e) {
        correctionStatus.textContent = `开放记录加载失败：${e.message || e}`;
      }
    }

    function showCorrection(show = true) {
      correctionVisible = show;
      correctionBox.hidden = !show;
      saveCorrection();
      if (show) loadOpenRecords();
    }

    async function refreshQuote() {
      if (!feeBox) return;
      const plate = plateInput.value.trim().toUpperCase();
      if (!plate) { feeBox.textContent = '正常费用：—'; return; }
      const seq = ++quoteSeq;
      try {
        const quote = await Api.quote(plate);
        if (seq !== quoteSeq) return;
        const amount = Number(quote.amount_due ?? quote.amount ?? 0);
        feeBox.textContent = `正常费用：¥${amount.toFixed(2)}`
          + (quote.minutes != null ? ` · ${Number(quote.minutes).toFixed(0)} 分钟` : '');
      } catch (_) {
        if (seq !== quoteSeq) return;
        feeBox.textContent = '正常费用：—';
        if (isExit) showCorrection(true);
      }
    }

    async function submitCorrection(action) {
      const record = selectedRecord();
      const recordId = action === 'auto' ? (record?.id ?? null) : null;
      const problem = validateCorrection({
        recordId, plate: correctPlate.value, action,
        unit: unitSel.value, value: entryValue.value, reason: reasonInput.value,
      });
      if (problem) {
        correctionStatus.className = 'small mt danger';
        correctionStatus.textContent = problem.message;
        (problem.field === 'reason' ? reasonInput : problem.field === 'entry' ? entryValue : correctPlate).focus();
        return;
      }
      const payload = buildCorrectionPayload({
        recordId, plate: correctPlate.value, action,
        unit: unitSel.value, value: entryValue.value,
        reason: reasonInput.value, requestId: crypto.randomUUID(),
      });
      try {
        const result = await Api.correctExit(payload);
        const summary = settlementSummary(result?.settlement || {});
        correctionStatus.className = 'small mt ok';
        correctionStatus.textContent = `${summary.label} · ${summary.detail}`;
        corrections.delete(inputId);
        // correct-exit 已结算记录；同步关闭对应待办，避免待办卡片残留。
        try { await Api.manualReject(item.id, 'correct_exit'); } catch (_) { /* 待办可能已由后端关闭 */ }
        toast(`${summary.label}，${summary.detail}`, 'ok', 6000);
        await refresh();
      } catch (e) {
        const code = e.code || e.detail?.code || '';
        correctionStatus.className = 'small mt danger';
        correctionStatus.textContent = `纠正失败：${code ? `[${code}] ` : ''}${e.message || e}`;
        if (code === 'no_parking_record') {
          correctionStatus.textContent += '；未找到记录，请选择开放记录或免费/作废';
        }
      }
    }

    async function fallbackCorrection(action) {
      if (!reasonInput.value.trim()) {
        const reason = await promptBox(`填写${action === 'free' ? '免费' : '作废'}原因`, { title: '出口异常纠正' });
        if (!reason) return;
        reasonInput.value = reason;
      }
      await submitCorrection(action);
    }

    plateInput.addEventListener('input', () => {
      drafts.set(inputId, plateInput.value);
      if (!correctPlate.value.trim()) correctPlate.value = plateInput.value;
      if (!feeBox) return;
      clearTimeout(quoteTimer);
      refreshQuote();
      quoteTimer = setTimeout(refreshQuote, 350);
    });
    recordSel.addEventListener('change', () => { applyRecordDefaults(); saveCorrection(); });
    unitSel.addEventListener('change', () => { applyRecordDefaults(); saveCorrection(); });
    correctPlate.addEventListener('input', saveCorrection);
    entryValue.addEventListener('input', saveCorrection);
    reasonInput.addEventListener('input', saveCorrection);
    correctionAuto.addEventListener('click', () => submitCorrection('auto'));
    correctionFree.addEventListener('click', () => fallbackCorrection('free'));
    correctionVoid.addEventListener('click', () => fallbackCorrection('void'));

    if (feeBox) queueMicrotask(refreshQuote);
    correctionBox.append(
      el('div', { class: 'small muted', text: '入口车牌/时间有误或找不到记录时，选择当前开放记录并纠正；找不到记录可免费/作废。' }),
      el('div', { class: 'grid cols-3 mt' }, [
        el('div', { class: 'field' }, [el('label', { text: '当前开放记录' }), recordSel]),
        el('div', { class: 'field' }, [el('label', { text: '正确车牌' }), correctPlate]),
        el('div', { class: 'field' }, [
          el('label', { text: '正确入场时间' }),
          el('div', { class: 'row', style: { gap: '6px' } }, [entryValue, unitSel]),
        ]),
      ]),
      el('div', { class: 'field' }, [el('label', { text: '免费/作废原因' }), reasonInput]),
      el('div', { class: 'btn-row mt' }, [correctionAuto, correctionFree, correctionVoid]),
      correctionStatus,
    );

    const confirm = async () => {
      if (isExit && (noRecord || correctionVisible)) {
        showCorrection(true);
        correctionStatus.className = 'small mt';
        correctionStatus.textContent = '请选择当前开放记录并确认纠正，或使用免费/作废';
        return;
      }
      try {
        const result = await Api.manualConfirm(item.id, plateInput.value, {});
        if (isExit) {
          const summary = settlementSummary(result?.settlement || {});
          toast(`${summary.label}，${summary.detail}`, 'ok', 6000);
        } else {
          toast('确认成功', 'ok');
        }
        drafts.delete(inputId);
        await refresh();
      } catch (e) {
        toast(`确认失败：${e.message || e}`, 'danger', 5000);
      }
    };
    const actionButtons = [
      plateInput,
      el('button', { class: 'btn sm ok', type: 'button', text: '确认', onclick: confirm }),
      el('button', { class: 'btn sm danger', type: 'button', text: '拒绝', onclick: async () => {
        try {
          await Api.manualReject(item.id, '管理员拒绝');
          drafts.delete(inputId);
          toast('已拒绝，已释放停止线，允许重试', 'ok');
          await refresh();
        } catch (e) { toast(`拒绝失败：${e.message || e}`, 'danger', 5000); }
      } }),
    ];

    body.appendChild(el('div', { class: 'feed-item inc', style: { display: 'block' } }, [
      el('div', { class: 'kind', text: `#${item.id} ${isExit ? '出口' : '入口'}` }),
      messageLine,
      image,
      feeBox,
      choices,
      el('div', { class: 'btn-row mt' }, actionButtons),
      correctionBox,
    ]));
    if (isExit) {
      if (correctionVisible) showCorrection(true);
      else if (noRecord) showCorrection(true);
    }
  });
  // 数据更新导致重绘后，恢复仍在编辑的输入值/焦点/选区。
  if (focus?.id) {
    const again = body.querySelector(`input[data-pending-id="${CSS.escape(String(focus.id))}"]`);
    if (again) {
      again.focus();
      try { again.setSelectionRange(focus.start ?? again.value.length, focus.end ?? again.value.length); }
      catch (_) { /* 非文本控件忽略 */ }
    }
  }
}

// ----------------------------- 设备控制（常显，仅物理设备） -----------------------------
function buildDeviceControlCard(refresh) {
  const card = el('div', { id: 'device-control-card', class: 'card device-control-card' }, [
    el('h3', { text: '设备控制' }),
  ]);
  const act = async (label, fn) => {
    try { await fn(); toast(`${label}成功`, 'ok'); await refresh?.(); }
    catch (e) { toast(`${label}失败：${e.message || e}`, 'danger', 5000); }
  };
  const gateLabel = (gate) => (gate === 'in' ? '入口' : '出口');
  const actionButton = (id, label, cls, fn) => el('button', {
    id, class: `btn sm ${cls || ''}`, type: 'button', text: label, onclick: () => act(label, fn),
  });
  const group = (kind, gate, buttons) => el('div', { class: 'device-gate' }, [
    el('span', { class: 'small muted', text: gateLabel(gate) }),
    ...buttons,
  ]);
  const section = (title, gates) => el('div', { class: 'device-row' }, [
    el('strong', { class: 'device-label', text: title }),
    ...gates,
  ]);

  card.appendChild(section('道闸', ['in', 'out'].map((gate) => group('barrier', gate, [
    actionButton(`device-barrier-raise-${gate}`, '抬杆', 'ok', () => Api.manualOpen(gate, 'admin_device')),
    actionButton(`device-barrier-lower-${gate}`, '落杆', '', () => Api.barrierAction(gate, 'close')),
    actionButton(`device-barrier-fault-${gate}`, '故障', 'warn', () => Api.barrierAction(gate, 'fault', { action: 'inject', mode: 'stuck_closed' })),
    actionButton(`device-barrier-recover-${gate}`, '恢复', 'ok', () => Api.barrierAction(gate, 'clear-fault')),
  ]))));
  card.appendChild(section('摄像头', ['in', 'out'].map((gate) => group('camera', gate, [
    actionButton(`device-camera-fault-${gate}`, '故障', 'warn', () => Api.cameraInject(gate, 'admin_device')),
    actionButton(`device-camera-recover-${gate}`, '恢复', 'ok', () => Api.cameraRecover(gate, '管理员恢复')),
  ]))));
  return card;
}

// ----------------------------- 演示重置（常驻紧凑入口） -----------------------------
function buildResetCard(refresh) {
  const report = el('div', { id: 'ops-reset-report', class: 'small muted', role: 'status', 'aria-live': 'polite' });
  const button = el('button', {
    id: 'ops-reset', class: 'btn sm danger', text: '重置', title: '强制重置当前演示场景',
    onclick: async () => {
      const ok = await confirmBox(
        '重置将清空当前场景、未完成的开放记录与人工动作，保留已完成历史；3D 端会同步复位。确认继续？',
        { title: '重置演示场景', danger: true, okText: '重置' });
      if (!ok) return;
      report.className = 'small muted';
      report.textContent = '正在重置…';
      try {
        const res = await Api.forceReset(crypto.randomUUID());
        const data = res?.reset_report || res?.report || {};
        const aborted = data.aborted_records ?? data.aborted ?? data.closed_records ?? 0;
        const pending = data.rejected_pending ?? data.pending ?? data.pending_actions ?? 0;
        const spaces = data.released_spaces ?? data.spaces ?? 0;
        report.className = 'small ok';
        report.textContent = `重置完成：作废 ${aborted} · 待处理 ${pending} · 释放车位 ${spaces}`;
        toast(report.textContent, 'ok', 6000);
        await refresh?.();
      } catch (e) {
        const detail = e?.detail;
        const reason = typeof detail === 'string' ? detail : (detail?.reason || detail?.code);
        report.className = 'small danger';
        report.textContent = `重置失败：${reason || e.message || e}`;
        toast(report.textContent, 'danger', 5000);
      }
    },
  });
  return el('div', { class: 'card compact-tools' }, [
    el('div', { class: 'row' }, [
      el('strong', { class: 'small', text: '演示控制' }),
      button,
      report,
    ]),
  ]);
}

