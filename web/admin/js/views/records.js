// 停车记录视图：条件检索（车牌/时段/状态）+ 分页表格 + 记录详情（照片/裁剪图/缴费明细）。
// 支持管理端结算缴费（POST /api/pay，幂等）与按车牌实时报价（GET /api/quote）。
import {
  el, clear, toast, modal, confirmBox, fmtTime, fmtMoney, fmtMinutes,
  recordStatus, plateClass, mediaUrl, dismissModals, loadErrorCard,
} from '../util.js';
import { Api } from '../api.js';
import { paymentMethodLabel, billingRows, rateVersionInfo } from '../record-view.js';

const PAGE_SIZE = 20;

export default {
  title: '停车记录',
  icon: '🧾',
  render(root, ctx) {
    const state = { plate: '', start: '', end: '', status: '', page: 1, total: 0 };

    const fPlate = el('input', { placeholder: '车牌（模糊）' });
    const fStart = el('input', { type: 'datetime-local' });
    const fEnd = el('input', { type: 'datetime-local' });
    const fStatus = el('select', {}, [
      el('option', { value: '', text: '全部状态' }),
      el('option', { value: 'in', text: '在场' }),
      el('option', { value: 'pending', text: '待缴费' }),
      el('option', { value: 'out', text: '已离场' }),
      el('option', { value: 'anomaly', text: '异常' }),
    ]);

    const tbody = el('tbody', {});
    const pagerInfo = el('span', {});

    async function load() {
      clear(tbody);
      tbody.appendChild(el('tr', {}, [el('td', { colspan: '9' }, [
        el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…'])])]));
      try {
        const r = await Api.records({
          plate: state.plate, start: state.start, end: state.end,
          status: state.status, page: state.page, page_size: PAGE_SIZE,
        });
        state.total = r.total || 0;
        renderRows(tbody, r.items || [], openDetail);
        renderPager();
      } catch (e) {
        clear(tbody);
        tbody.appendChild(el('tr', {}, [el('td', { colspan: '9' }, [
          loadErrorCard(e.message || '加载失败', () => load(), { title: '停车记录加载失败' })])]));
      }
    }

    function renderPager() {
      clear(pagerInfo);
      const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
      pagerInfo.append(`共 ${state.total} 条 · 第 ${state.page}/${pages} 页`);
    }

    const searchBtn = el('button', { class: 'btn primary', text: '查询', onclick: () => {
      state.plate = fPlate.value.trim();
      state.start = toIso(fStart.value);
      state.end = toIso(fEnd.value);
      state.status = fStatus.value;
      state.page = 1; load();
    } });
    const resetBtn = el('button', { class: 'btn ghost', text: '重置', onclick: () => {
      fPlate.value = ''; fStart.value = ''; fEnd.value = ''; fStatus.value = '';
      Object.assign(state, { plate: '', start: '', end: '', status: '', page: 1 }); load();
    } });

    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '停车记录' }),
      el('span', { class: 'sub', text: '进出场明细 · 缴费信息 · 车牌照片' }),
    ]));
    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'filters' }, [
        field('车牌', fPlate), field('入场起', fStart), field('入场止', fEnd), field('状态', fStatus),
        el('div', { class: 'btn-row' }, [searchBtn, resetBtn]),
      ]),
    ]));

    const table = el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'ID' }), el('th', { text: '车牌' }), el('th', { text: '车位' }),
        el('th', { text: '入场(本地)' }), el('th', { text: '出场(本地)' }),
        el('th', { text: '状态' }), el('th', { text: '费率版本' }),
        el('th', { text: '来源' }), el('th', { text: '操作' }),
      ])]),
      tbody,
    ]);
    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'table-wrap' }, [table]),
      el('div', { class: 'pager' }, [
        pagerInfo,
        el('button', { class: 'btn sm', text: '上一页', onclick: () => { if (state.page > 1) { state.page--; load(); } } }),
        el('button', { class: 'btn sm', text: '下一页', onclick: () => {
          if (state.page * PAGE_SIZE < state.total) { state.page++; load(); }
        } }),
      ]),
    ]));

    load();
    return () => {};
  },
};

function field(label, node) { return el('div', { class: 'field' }, [el('label', { text: label }), node]); }
function toIso(v) { return v ? `${v}:00` : ''; }  // 后端按停车场本地时区解释

function renderRows(tbody, items, openDetail) {
  clear(tbody);
  if (!items.length) {
    tbody.appendChild(el('tr', {}, [el('td', { colspan: '9' }, [
      el('div', { class: 'empty', text: '无匹配记录' })])]));
    return;
  }
  items.forEach((it) => {
    const canPay = it.status === 'in' || it.status === 'pending';
    tbody.appendChild(el('tr', { class: 'clickable' }, [
      el('td', { class: 'mono small', text: `#${it.id}` }),
      el('td', {}, [el('span', { class: `tag-plate ${plateClass(it.plate_color)}`, text: it.plate || '—' })]),
      el('td', { text: it.space_no || '—' }),
      el('td', { class: 'small', text: fmtTime(it.entry_time) }),
      el('td', { class: 'small', text: fmtTime(it.exit_time) }),
      el('td', {}, [statusBadge(it.status)]),
      el('td', { class: 'small', text: it.fee_rule_id ? `#${it.fee_rule_id}` : '—' }),
      el('td', {}, [it.manual_entry ? el('span', { class: 'badge info', text: '人工' }) : el('span', { class: 'muted small', text: '自动' })]),
      el('td', {}, [el('div', { class: 'btn-row', style: { gap: '6px' } }, [
        el('button', { class: 'btn sm', text: '详情', onclick: (e) => { e.stopPropagation(); openDetail(it.id); } }),
        canPay ? el('button', { class: 'btn sm primary', text: '报价', onclick: (e) => { e.stopPropagation(); openQuote(it.plate, it.id); } }) : null,
      ])]),
    ]));
  });
  // 整行点击也打开详情
  Array.from(tbody.children).forEach((tr, i) => {
    tr.addEventListener('click', () => openDetail(items[i].id));
  });
}

function statusBadge(s) {
  const cls = s === 'out' ? 'muted' : s === 'in' ? 'ok' : s === 'pending' ? 'warn' : 'danger';
  return el('span', { class: `badge ${cls}`, text: recordStatus(s) });
}

// ----------------------------- 详情 -----------------------------
async function openDetail(id) {
  const body = el('div', {}, [el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…'])]);
  const close = modal({ title: `记录详情 #${id}`, wide: true, body, hideFooter: true });
  try {
    const [r, currentRule, allRules] = await Promise.all([
      Api.record(id),
      Api.currentFeeRule().catch(() => null),
      Api.feeRules().catch(() => ({ items: [] })),
    ]);
    clear(body);
    body.appendChild(detailContent(r, { currentRule, rules: allRules?.items || [] }));
  } catch (e) {
    clear(body);
    body.appendChild(el('div', { class: 'empty', text: `加载失败：${e.message || e}` }));
  }
  void close;
}

function detailContent(r, { currentRule = null, rules = [] } = {}) {
  const wrap = el('div', {});
  const pay = r.payment;
  const recordRule = rules.find((rule) => String(rule.id) === String(r.fee_rule_id))
    || (r.fee_rule_id ? { id: r.fee_rule_id } : null);
  const rate = rateVersionInfo(recordRule, currentRule);
  const rateValue = el('span', {}, [
    el('span', { text: rate.version }),
    rate.differs ? el('span', { class: 'badge warn', text: rate.note }) : null,
    recordRule?.effective_from
      ? el('span', { class: 'small muted', text: ` · 生效 ${fmtTime(recordRule.effective_from, { utcTag: true })}` })
      : null,
  ]);

  wrap.appendChild(el('div', { class: 'kv mb' }, [
    kv('车牌', el('span', { class: `tag-plate ${plateClass(r.plate_color)}`, text: r.plate || '—' })),
    kv('车牌颜色', r.plate_color || '—'),
    kv('车位', r.space_no || '—'),
    kv('状态', statusBadge(r.status)),
    kv('入场时间', fmtTime(r.entry_time, { utcTag: true })),
    kv('出场时间', fmtTime(r.exit_time, { utcTag: true })),
    kv('车辆ID', r.vehicle_id || '—'),
    kv('入场方式', r.temporary ? '无记录临时结算（时间补录或推定）' : r.manual_entry ? '人工录入' : '自动识别'),
    kv('费率版本', rateValue),
    kv('优惠资格', r.benefit_type === 'white' ? '白名单（本次免费）'
      : r.benefit_type === 'monthly' ? '月租（本次免费）' : '普通车辆'),
  ]));

  // 缴费信息
  if (pay) {
    wrap.appendChild(el('h3', { text: '缴费信息' }));
    wrap.appendChild(el('div', { class: 'kv mb' }, [
      kv('应付', el('b', { text: fmtMoney(pay.amount_due) })),
      kv('实付', el('b', { class: 'ok', text: fmtMoney(pay.amount_paid) })),
      kv('时长', fmtMinutes(pay.minutes)),
      kv('支付方式', paymentMethodLabel(pay.method)),
      kv('操作员', pay.operator || '—'),
      kv('支付时间', fmtTime(pay.paid_at, { utcTag: true })),
    ]));
    wrap.appendChild(breakdownBox(pay.breakdown, pay, recordRule));
  } else if (r.status === 'in' || r.status === 'pending') {
    wrap.appendChild(el('div', { class: 'btn-row mt' }, [
      el('button', { class: 'btn primary', text: r.benefit_type ? '确认免费放行' : '结算缴费', onclick: async () => {
        const ok = await confirmBox(`确认对记录 #${r.id}（${r.plate}）${r.benefit_type ? '免费放行' : '执行结算缴费'}？`,
          { title: r.benefit_type ? '免费放行' : '结算缴费' });
        if (!ok) return;
        try { const res = await Api.pay(r.id, r.benefit_type === 'monthly' ? 'monthly' : r.benefit_type ? 'free' : 'mock_scan'); toast(`结算成功 ${fmtMoney(res.amount_paid)}`, 'ok'); modalRefresh(r.id); }
        catch (e) { toast(`缴费失败：${e.message || e}`, 'danger'); }
      } }),
      el('span', { class: 'small muted', style: { alignSelf: 'center' }, text: '尚未缴费' }),
    ]));
  }

  // 照片
  wrap.appendChild(el('h3', { class: 'mt', text: '图像留存' }));
  wrap.appendChild(el('div', { class: 'photos' }, [
    photoBox(r.entry_plate_crop, '入场车牌裁剪'),
    photoBox(r.entry_photo, '入场抓拍'),
    photoBox(r.exit_plate_crop, '出场车牌裁剪'),
    photoBox(r.exit_photo, '出场抓拍'),
  ]));
  return wrap;
}

function modalRefresh(id) {
  // 简单刷新：关闭当前 modal 并重开详情
  dismissModals();
  openDetail(id);
}

function kv(k, v) {
  return [el('div', { class: 'k', text: k }),
    v instanceof Node ? el('div', { class: 'v' }, [v]) : el('div', { class: 'v', text: String(v ?? '—') })];
}

function breakdownBox(bd, pay = {}, rule = {}) {
  const box = el('div', { class: 'card', style: { background: '#fafbfe', marginBottom: '12px' } }, [
    el('h3', { text: '计费明细' }),
  ]);
  const view = billingRows(bd, pay, rule);
  const list = el('div', { class: 'kv' });
  view.rows.forEach((row) => {
    list.appendChild(el('div', { class: 'k', text: row.label }));
    list.appendChild(el('div', { class: 'v', text: row.value }));
  });
  if (!view.rows.length) {
    list.appendChild(el('div', { class: 'v muted', text: '该记录未返回计费字段' }));
  }
  box.appendChild(list);
  if (view.detail) {
    box.appendChild(el('div', { class: 'small muted mt', text: `摘要：${view.detail}` }));
  }
  if (view.unknown.length) {
    const detail = el('details', { class: 'mt' }, [el('summary', { text: '其他字段' })]);
    const extra = el('div', { class: 'kv' });
    view.unknown.forEach(([key, value]) => {
      extra.appendChild(el('div', { class: 'k mono small', text: key }));
      extra.appendChild(el('div', { class: 'v small', text: value }));
    });
    detail.appendChild(extra);
    box.appendChild(detail);
  }
  return box;
}

function photoBox(path, cap) {
  const url = mediaUrl(path);
  return el('div', { class: 'photo-box' }, [
    url
      ? el('img', { src: url, alt: cap, onerror: (e) => { e.target.replaceWith(el('div', { class: 'noimg', text: '无图像' })); } })
      : el('div', { class: 'noimg', text: '无图像' }),
    el('div', { class: 'cap', text: cap }),
  ]);
}

// ----------------------------- 报价 -----------------------------
async function openQuote(plate, recordId) {
  if (!plate) { toast('无车牌，无法报价', 'warn'); return; }
  const body = el('div', {}, [el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '报价中…'])]);
  modal({ title: `实时报价 · ${plate}`, body, okText: '结算缴费', onOk: async () => {
    try { const res = await Api.pay(recordId, 'mock_scan'); toast(`缴费成功 ${fmtMoney(res.amount_paid)}`, 'ok'); }
    catch (e) { toast(`缴费失败：${e.message || e}`, 'danger'); return false; }
  } });
  try {
    const q = await Api.quote(plate);
    clear(body);
    body.appendChild(el('div', { class: 'kv mb' }, [
      kv('车牌', el('span', { class: `tag-plate ${plateClass(q.plate_color)}`, text: q.plate || plate })),
      kv('入场', fmtTime(q.entry_time)),
      kv('出场/当前', fmtTime(q.exit_time)),
      kv('时长', fmtMinutes(q.minutes)),
      kv('车位', q.space_no || '—'),
      kv('应付', el('b', { class: 'value', style: { fontSize: '22px', color: 'var(--brand-ink)' }, text: fmtMoney(q.amount_due) })),
    ]));
    if (q.breakdown) body.appendChild(breakdownBox(q.breakdown, q));
    if (q.paid) body.appendChild(el('div', { class: 'badge ok', text: '该记录已缴费' }));
  } catch (e) {
    clear(body);
    body.appendChild(el('div', { class: 'empty', text: `报价失败：${e.message || e}` }));
  }
}
