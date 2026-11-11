// 异常面板视图：未闭环/已处理异常事件列表 + 处理（resolve）；第二标签页为操作日志（审计）。
// 异常来源：摄像头故障、识别失败、道闸故障、跟车闯入、无入场记录、车位对账等（FR-9.x）。
import { el, clear, toast, modal, fmtTime, incidentKind, loadErrorCard } from '../util.js';
import { Api } from '../api.js';
import { RealtimeClient } from '../ws.js';

export default {
  title: '异常处理',
  icon: '⚠️',
  render(root, ctx) {
    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '异常与日志' }),
      el('span', { class: 'sub', text: '异常事件闭环处理 · 操作审计' }),
    ]));

    const tabInc = el('button', { class: 'active', text: '异常事件' });
    const tabLog = el('button', { text: '操作日志' });
    const paneInc = el('div', {});
    const paneLog = el('div', { style: { display: 'none' } });
    root.appendChild(el('div', { class: 'tabs' }, [tabInc, tabLog]));
    root.appendChild(paneInc);
    root.appendChild(paneLog);

    let logLoaded = false;
    tabInc.onclick = () => { tabInc.classList.add('active'); tabLog.classList.remove('active'); paneInc.style.display = ''; paneLog.style.display = 'none'; };
    tabLog.onclick = async () => { tabLog.classList.add('active'); tabInc.classList.remove('active'); paneLog.style.display = ''; paneInc.style.display = 'none'; if (!logLoaded) { await renderOplogs(paneLog); logLoaded = true; } };

    const incidents = buildIncidents(paneInc);
    const ws = new RealtimeClient(m => {
      if (['incident', 'incident_resolved'].includes(m.type)) incidents.load();
      if (m.type === 'incident_resolved') logLoaded = false;
    }, () => {}, () => incidents.load());
    ws.connect().catch(() => {});
    const timer = setInterval(() => incidents.load(), 5000);
    return () => { clearInterval(timer); ws.close(); incidents.close(); };
  },
};

function buildIncidents(pane) {
  const statusSel = el('select', {'aria-label':'异常状态', style: { width: '140px' } }, [
    el('option', { value: 'open', text: '未闭环' }),
    el('option', { value: '', text: '全部' }),
    el('option', { value: 'resolved', text: '已处理' }),
  ]);
  const tbody = el('tbody', {});
  const card = el('div', { class: 'card' }, [
    el('h3', {}, ['异常事件', el('span', { class: 'spacer' }),
      el('span', { class: 'row', style: { gap: '8px' } }, [
        statusSel,
        el('button', { class: 'btn sm primary', text: '刷新', onclick: load }),
      ])]),
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: 'ID' }), el('th', { text: '时间(本地)' }), el('th', { text: '类型' }),
          el('th', { text: '闸口' }), el('th', { text: '详情' }), el('th', { text: '状态' }),
          el('th', { text: '处理' }), el('th', { text: '操作' }),
        ])]),
        tbody,
      ]),
    ]),
  ]);
  pane.appendChild(card);
  statusSel.onchange = load;

  let closed = false, loading = false, again = false;
  async function load() {
    if (closed) return;
    if (loading) { again = true; return; }
    loading = true;
    if (!tbody.children.length) {
    clear(tbody);
    tbody.appendChild(el('tr', {}, [el('td', { colspan: '8' }, [el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…'])])]));
    }
    try {
      const r = await Api.incidents(statusSel.value || null, 200);
      if (closed) return;
      const items = r.items || [];
      clear(tbody);
      if (!items.length) { tbody.appendChild(el('tr', {}, [el('td', { colspan: '8' }, [el('div', { class: 'empty', text: '无异常事件 🎉' })])])); return; }
      items.forEach((it) => {
        const open = it.status === 'open';
        tbody.appendChild(el('tr', {}, [
          el('td', { class: 'mono small', text: `#${it.id}` }),
          el('td', { class: 'small', text: fmtTime(it.ts) }),
          el('td', {}, [el('span', { class: `badge ${open ? 'danger' : 'muted'}`, text: incidentKind(it.kind) })]),
          el('td', { class: 'small', text: it.gate || '—' }),
          el('td', { class: 'small', text: summarize(it.detail) }),
          el('td', {}, [open ? el('span', { class: 'badge warn', text: '未闭环' }) : el('span', { class: 'badge ok', text: '已处理' })]),
          el('td', { class: 'small', text: it.resolution || (it.resolved_by ? `${it.resolved_by} @ ${fmtTime(it.resolved_at)}` : '—') }),
          el('td', {}, [open ? el('button', { class: 'btn sm ok', text: '处理', onclick: () => resolve(it) }) : el('span', { class: 'muted small', text: '—' })]),
        ]));
      });
    } catch (e) {
      if (closed) return;
      clear(tbody);
      tbody.appendChild(el('tr', {}, [el('td', { colspan: '8' }, [
        loadErrorCard(e.message || '加载失败', () => load(), { title: '异常事件加载失败' })])]));
    } finally {
      loading = false;
      if (again && !closed) { again = false; load(); }
    }
  }

  function resolve(it) {
    const ta = el('textarea', { placeholder: '处理说明（如：已现场复位道闸 / 已人工核放）' });
    ta.value = '';
    modal({
      title: `处理异常 #${it.id} · ${incidentKind(it.kind)}`, okText: '标记为已处理',
      body: el('div', {}, [
        el('div', { class: 'kv mb' }, [
          el('div', { class: 'k', text: '类型' }), el('div', { class: 'v', text: incidentKind(it.kind) }),
          el('div', { class: 'k', text: '闸口' }), el('div', { class: 'v', text: it.gate || '—' }),
          el('div', { class: 'k', text: '时间' }), el('div', { class: 'v', text: fmtTime(it.ts, { utcTag: true }) }),
          el('div', { class: 'k', text: '详情' }), el('div', { class: 'v', text: summarize(it.detail) }),
        ]),
        el('div', { class: 'field' }, [el('label', { text: '处理说明' }), ta]),
      ]),
      onOk: async () => {
        try {
          await Api.resolveIncident(it.id, ta.value.trim() || '管理员已处理');
          toast(`异常 #${it.id} 已闭环`, 'ok');
          load();
        } catch (e) { toast(`处理失败：${e.message || e}`, 'danger'); return false; }
      },
    });
  }

  load();
  return { load, close() { closed = true; } };
}

function summarize(detail) {
  if (detail == null) return '—';
  if (typeof detail === 'string') return detail;
  try {
    const s = JSON.stringify(detail);
    return s.length > 80 ? `${s.slice(0, 80)}…` : s;
  } catch (_) { return String(detail); }
}

async function renderOplogs(pane) {
  clear(pane);
  const tbody = el('tbody', {});
  pane.appendChild(el('div', { class: 'card' }, [
    el('h3', {}, ['操作日志（审计）', el('span', { class: 'spacer' }),
      el('button', { class: 'btn sm', text: '刷新', onclick: () => renderOplogs(pane) })]),
    el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: '时间(本地)' }), el('th', { text: '操作员' }),
          el('th', { text: '动作' }), el('th', { text: '对象' }), el('th', { text: '详情' }),
        ])]),
        tbody,
      ]),
    ]),
  ]));
  tbody.appendChild(el('tr', {}, [el('td', { colspan: '5' }, [el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…'])])]));
  try {
    const r = await Api.oplogs(200);
    const items = r.items || [];
    clear(tbody);
    if (!items.length) { tbody.appendChild(el('tr', {}, [el('td', { colspan: '5' }, [el('div', { class: 'empty', text: '暂无操作日志' })])])); return; }
    items.forEach((it) => tbody.appendChild(el('tr', {}, [
      el('td', { class: 'mono small', text: fmtTime(it.ts) }),
      el('td', { class: 'small', text: it.operator || '—' }),
      el('td', {}, [el('span', { class: 'badge brand', text: it.action || '—' })]),
      el('td', { class: 'small mono', text: it.target || '—' }),
      el('td', { class: 'small', text: summarize(it.detail) }),
    ])));
  } catch (e) {
    clear(tbody);
    tbody.appendChild(el('tr', {}, [el('td', { colspan: '5' }, [
      loadErrorCard(e.message || '加载失败', () => renderOplogs(pane), { title: '操作日志加载失败' })])]));
  }
}
