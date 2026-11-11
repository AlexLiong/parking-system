// 统计报表视图：总览图表 + 报表预览（day/week/month/quarter/year）+ CSV/PDF 导出。
import { el, clear, toast, modal, fmtTime, fmtMoney, fmtMinutes, num, loadErrorCard } from '../util.js';
import { Api } from '../api.js';
import { barChart, lineChart, PALETTE } from '../charts.js';
import {
  normalizePreview, summaryRows, REPORT_PERIODS, periodAnchorLabel,
  buildAnchor, dateParts, isoWeekValue, reportLang,
} from '../report-view.js';
import { locale } from '../i18n.js';
import { paymentMethodLabel } from '../record-view.js';

export default {
  title: '统计报表',
  icon: '📊',
  async render(root) {
    const periodSel = el('select', {}, REPORT_PERIODS.map(([v, t]) => el('option', { value: v, text: `${t}统计` })));
    periodSel.value = 'day';
    const anchorCtl = makeAnchorControl('day');

    const cards = el('div', { class: 'grid cols-5' });
    const barCanvas = el('canvas', {});
    const lineCanvas = el('canvas', {});
    const legend = el('div', { class: 'legend' });
    const rangeInfo = el('div', { class: 'small muted mb', text: '' });
    let lastStats = null;

    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '统计报表' }),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn primary', text: '预览报表',
        onclick: () => openPreview(periodSel.value, anchorCtl.getAnchor()) }),
    ]));

    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'filters' }, [
        el('div', { class: 'field' }, [el('label', { text: '统计周期' }), periodSel]),
        anchorCtl.el,
        el('button', { class: 'btn primary', style: { marginBottom: '0' }, text: '生成', onclick: load }),
      ]),
    ]));

    root.appendChild(cards);
    root.appendChild(el('div', { class: 'card' }, [
      el('h3', { text: '每日进出场' }), rangeInfo,
      el('div', { class: 'chart-box' }, [barCanvas]),
      legend,
    ]));
    root.appendChild(el('div', { class: 'card' }, [
      el('h3', { text: '每日营收（¥）' }),
      el('div', { class: 'chart-box' }, [lineCanvas]),
    ]));

    periodSel.addEventListener('change', () => anchorCtl.setPeriod(periodSel.value));

    async function load() {
      clear(cards);
      for (let i = 0; i < 5; i++) cards.appendChild(el('div', { class: 'stat' }, [el('div', { class: 'spinner' })]));
      try {
        const anchor = anchorCtl.getAnchor();
        const s = await Api.stats(periodSel.value, anchor || null);
        lastStats = s;
        renderCards(cards, s);
        renderCharts(s, barCanvas, lineCanvas, legend);
        rangeInfo.textContent = `周期：${s.period} · 区间 ${fmtTime(s.start, { withSec: false })} ~ ${fmtTime(s.end, { withSec: false })}（停车场本地时间） · 生成于 ${fmtTime(s.generated_at)}`;
      } catch (e) {
        clear(cards);
        cards.appendChild(loadErrorCard(e.message || '加载失败', () => load(), { title: '统计数据加载失败' }));
      }
    }

    await load();
    const onResize = () => { if (lastStats) renderCharts(lastStats, barCanvas, lineCanvas, legend); };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  },
};

// 周期基准控件：day=日期；week=周；month=月份；quarter=年+Q；year=年份。
function makeAnchorControl(initialPeriod = 'day', initialAnchor = '') {
  const wrap = el('div', { class: 'field report-anchor' });
  const label = el('label', { text: periodAnchorLabel(initialPeriod) });
  const fields = el('div', { class: 'anchor-fields' });
  wrap.append(label, fields);
  let period = initialPeriod;
  let refs = {};

  function render(p, preset = '') {
    period = p;
    label.textContent = periodAnchorLabel(p);
    clear(fields);
    refs = {};
    const parts = dateParts(new Date());
    const base = preset || '';
    if (p === 'day') {
      const value = /^\d{4}-\d{2}-\d{2}$/.test(base) ? base : parts.date;
      const input = el('input', { type: 'date', value });
      refs.date = input; fields.appendChild(input);
    } else if (p === 'week') {
      let value = parts.week;
      if (base) {
        const d = new Date(`${base}T00:00:00`);
        if (!Number.isNaN(d.getTime())) value = isoWeekValue(d);
      }
      const input = el('input', { type: 'week', value });
      refs.week = input; fields.appendChild(input);
    } else if (p === 'month') {
      const value = /^\d{4}-\d{2}$/.test(base) ? base : (/^\d{4}-\d{2}-\d{2}$/.test(base) ? base.slice(0, 7) : parts.month);
      const input = el('input', { type: 'month', value });
      refs.month = input; fields.appendChild(input);
    } else if (p === 'quarter') {
      const year = /^\d{4}/.test(base) ? base.slice(0, 4) : parts.quarterYear;
      const q = /^\d{4}-\d{2}-\d{2}$/.test(base)
        ? String(Math.floor((Number(base.slice(5, 7)) - 1) / 3) + 1) : parts.quarter;
      const yearIn = el('input', { type: 'number', min: '2000', max: '2100', value: year, style: { width: '92px' } });
      const qSel = el('select', {}, ['1', '2', '3', '4'].map((x) => el('option', { value: x, text: `Q${x}` })));
      qSel.value = q;
      refs.quarterYear = yearIn; refs.quarter = qSel;
      fields.append(el('div', { class: 'anchor-inline' }, [yearIn, el('span', { class: 'small muted', text: '年' }), qSel]));
    } else if (p === 'year') {
      const value = /^\d{4}/.test(base) ? base.slice(0, 4) : parts.year;
      const input = el('input', { type: 'number', min: '2000', max: '2100', value, style: { width: '110px' } });
      refs.year = input; fields.appendChild(input);
    }
  }

  function getAnchor() {
    return buildAnchor(period, {
      date: refs.date?.value,
      week: refs.week?.value,
      month: refs.month?.value,
      quarterYear: refs.quarterYear?.value,
      quarter: refs.quarter?.value,
      year: refs.year?.value,
    });
  }

  render(initialPeriod, initialAnchor);
  return {
    el: wrap,
    getAnchor,
    setPeriod(p) { render(p); },
  };
}

function renderCards(cards, s) {
  clear(cards);
  const defs = [
    ['accent', '入场车次', num(s.count_in), ''],
    ['brand', '出场车次', num(s.count_out), ''],
    ['ok', '营收', fmtMoney(s.revenue), ''],
    ['', '平均时长', fmtMinutes(s.avg_minutes), ''],
    ['warn', '峰值占用', num(s.peak_occupancy), '车位'],
  ];
  defs.forEach(([accent, label, value, foot]) => {
    cards.appendChild(el('div', { class: `stat ${accent}` }, [
      el('div', { class: 'label', text: label }),
      el('div', { class: 'value', text: value }),
      foot ? el('div', { class: 'foot', text: foot }) : null,
    ]));
  });
}

function renderCharts(s, barCanvas, lineCanvas, legend) {
  const by = s.by_day || [];
  const labels = by.map((d) => String(d.date || '').slice(5));
  const inVals = by.map((d) => Number(d.count_in) || 0);
  const outVals = by.map((d) => Number(d.count_out) || 0);
  const revVals = by.map((d) => Number(d.revenue) || 0);
  if (!by.length) {
    labels.push(String(s.anchor || '当日'));
    inVals.push(Number(s.count_in) || 0);
    outVals.push(Number(s.count_out) || 0);
    revVals.push(Number(s.revenue) || 0);
  }
  barChart(barCanvas, {
    labels,
    series: [
      { name: '入场', values: inVals, color: PALETTE[0] },
      { name: '出场', values: outVals, color: PALETTE[1] },
    ],
    height: 260,
  });
  lineChart(lineCanvas, {
    labels,
    series: [{ name: '营收', values: revVals, color: PALETTE[2], fill: true }],
    height: 220,
    yFormat: (v) => `¥${Math.round(v)}`,
  });
  clear(legend);
  [['入场', PALETTE[0]], ['出场', PALETTE[1]]].forEach(([name, color]) => {
    legend.appendChild(el('span', { class: 'li' }, [
      el('span', { class: 'sw', style: { background: color } }), name,
    ]));
  });
}

// ----------------------------- 报表预览 -----------------------------
function openPreview(initialPeriod, initialAnchor) {
  const body = el('div', {});
  const periodSel = el('select', {}, REPORT_PERIODS.map(([v, t]) => el('option', { value: v, text: `${t}统计` })));
  periodSel.value = initialPeriod || 'day';
  const anchorCtl = makeAnchorControl(periodSel.value, initialAnchor || '');
  const result = el('div', {});
  const status = el('div', { class: 'small muted mb' });
  const csvBtn = el('button', { class: 'btn', text: '导出 CSV', onclick: () => download('csv') });
  const pdfBtn = el('button', { class: 'btn', text: '导出 PDF', onclick: () => download('pdf') });
  body.appendChild(el('div', { class: 'filters' }, [
    el('div', { class: 'field' }, [el('label', { text: '周期' }), periodSel]),
    anchorCtl.el,
    el('button', { class: 'btn primary', style: { marginBottom: '0' }, text: '加载预览', onclick: load }),
  ]));
  body.appendChild(el('div', { class: 'btn-row mb' }, [csvBtn, pdfBtn]));
  body.appendChild(status);
  body.appendChild(result);
  modal({ title: '预览报表', wide: true, body, hideFooter: true });
  periodSel.addEventListener('change', () => anchorCtl.setPeriod(periodSel.value));
  load();

  async function load() {
    status.textContent = '加载中…';
    clear(result);
    try {
      const anchor = anchorCtl.getAnchor();
      const raw = await Api.reportPreview(periodSel.value, anchor || null);
      const preview = normalizePreview(raw);
      status.textContent = `${preview.period} · ${fmtTime(preview.start)} ~ ${fmtTime(preview.end)}，仅完整出场记录`;
      renderPreview(result, preview);
    } catch (e) {
      status.textContent = `预览失败：${e.message || e}`;
    }
  }

  function download(kind) {
    const period = periodSel.value;
    const anchor = anchorCtl.getAnchor();
    const lang = reportLang(locale);
    const url = kind === 'csv' ? Api.reportCsvUrl(period, anchor, lang) : Api.reportPdfUrl(period, anchor, lang);
    const a = el('a', { href: url, target: '_blank', rel: 'noopener' });
    document.body.appendChild(a); a.click(); a.remove();
    toast(kind === 'csv' ? '已请求导出 CSV' : '已请求导出 PDF', 'ok');
  }
}

function renderPreview(root, preview) {
  const cards = el('div', { class: 'grid cols-5' });
  summaryRows(preview.summary).forEach(([label, value]) => {
    cards.appendChild(el('div', { class: 'stat' }, [
      el('div', { class: 'label', text: label }),
      el('div', { class: 'value', style: { fontSize: '20px' }, text: formatSummary(label, value) }),
    ]));
  });
  root.appendChild(cards);

  if (preview.vehicles.length) {
    root.appendChild(el('h3', { class: 'mt', text: '车辆明细' }));
    root.appendChild(vehicleTable(preview.vehicles));
  }
  if (preview.period === 'week' && preview.byDay.length) {
    root.appendChild(el('h3', { class: 'mt', text: '每日汇总' }));
    root.appendChild(groupTable(preview.byDay, dailyColumns()));
  }
  if (preview.period === 'month' && preview.weeks.length) {
    root.appendChild(el('h3', { class: 'mt', text: '按周汇总' }));
    root.appendChild(groupTable(preview.weeks, weeklyColumns()));
  }
  if ((preview.period === 'quarter' || preview.period === 'year') && preview.months.length) {
    root.appendChild(el('h3', { class: 'mt', text: '按月汇总' }));
    root.appendChild(groupTable(preview.months, monthlyColumns()));
  }
  if (!preview.vehicles.length && !preview.byDay.length && !preview.weeks.length && !preview.months.length) {
    root.appendChild(el('div', { class: 'empty', text: '该周期暂无完整记录' }));
  }
}

const VEHICLE_COLUMNS = [
  { key: 'plate', label: '车牌', width: '14%', align: 'left' },
  { key: 'entry_time', label: '入场', width: '21%', align: 'left', type: 'datetime' },
  { key: 'exit_time', label: '出场', width: '21%', align: 'left', type: 'datetime' },
  { key: 'duration_minutes', label: '时长(分钟)', width: '10%', align: 'right' },
  { key: 'amount', label: '金额', width: '10%', align: 'right', type: 'money' },
  { key: 'payment_method', label: '支付方式', width: '13%', align: 'left', type: 'payment' },
  { key: 'space_no', label: '车位', width: '11%', align: 'left' },
];

function vehicleTable(items) {
  return tableShell(VEHICLE_COLUMNS, items, (v) => ({
    plate: el('span', { class: 'tag-plate', text: v.plate || '—' }),
    entry_time: dateTimeCell(v.entry_time),
    exit_time: dateTimeCell(v.exit_time),
    duration_minutes: display(v.duration_minutes),
    amount: v.amount != null ? fmtMoney(v.amount) : '—',
    payment_method: paymentMethodLabel(v.payment_method),
    space_no: display(v.space_no),
  }));
}

function dailyColumns() {
  return [
    { key: 'date', label: '日期', width: '24%', align: 'left', type: 'date' },
    { key: 'count_in', label: '入场', width: '14%', align: 'right' },
    { key: 'count_out', label: '出场', width: '14%', align: 'right' },
    { key: 'revenue', label: '营收', width: '18%', align: 'right', type: 'money' },
    { key: 'avg_minutes', label: '平均时长', width: '15%', align: 'right', type: 'minutes' },
    { key: 'peak_occupancy', label: '峰值占用', width: '15%', align: 'right' },
  ];
}

function weeklyColumns() {
  return [
    { key: 'week_start', label: '周开始', width: '22%', align: 'left', type: 'date' },
    { key: 'week_end', label: '周末', width: '22%', align: 'left', type: 'date' },
    { key: 'count_in', label: '入场', width: '15%', align: 'right' },
    { key: 'count_out', label: '出场', width: '15%', align: 'right' },
    { key: 'revenue', label: '营收', width: '26%', align: 'right', type: 'money' },
  ];
}

function monthlyColumns() {
  return [
    { key: 'month', label: '月份', width: '28%', align: 'left' },
    { key: 'count_in', label: '入场', width: '18%', align: 'right' },
    { key: 'count_out', label: '出场', width: '18%', align: 'right' },
    { key: 'revenue', label: '营收', width: '36%', align: 'right', type: 'money' },
  ];
}

function tableShell(columns, items, rowOf) {
  const table = el('table', { class: 'report-table' });
  const colgroup = el('colgroup');
  columns.forEach((c) => colgroup.appendChild(el('col', { style: { width: c.width } })));
  const thead = el('thead', {}, [el('tr', {}, columns.map((c) => el('th', {
    class: c.align === 'right' ? 'right' : 'left', text: c.label,
  })))]);
  const tbody = el('tbody', {}, items.map((item) => {
    const cells = rowOf(item);
    return el('tr', {}, columns.map((c) => el('td', {
      class: c.align === 'right' ? 'right' : 'left',
    }, [cells[c.key] ?? '—'])));
  }));
  table.append(colgroup, thead, tbody);
  return el('div', { class: 'table-wrap' }, [table]);
}

function groupTable(items, columns) {
  return tableShell(columns, items, (row) => {
    const out = {};
    columns.forEach((c) => {
      if (c.type === 'money') out[c.key] = row[c.key] != null ? fmtMoney(row[c.key]) : '—';
      else if (c.type === 'minutes') out[c.key] = row[c.key] != null ? fmtMinutes(row[c.key]) : '—';
      else if (c.type === 'date') out[c.key] = row[c.key] ? String(row[c.key]).slice(0, 10) : '—';
      else out[c.key] = display(row[c.key]);
    });
    return out;
  });
}

function dateTimeCell(value) {
  if (!value) return '—';
  const text = fmtTime(value);
  const idx = text.indexOf(' ');
  if (idx < 0) return el('span', { class: 'datetime', text });
  return el('span', { class: 'datetime' }, [text.slice(0, idx), el('br'), text.slice(idx + 1)]);
}

function display(value) {
  return value == null || value === '' ? '—' : String(value);
}

function formatSummary(label, value) {
  if (value == null || value === '—') return '—';
  if (label === '营收') return fmtMoney(value);
  if (label === '平均时长') return fmtMinutes(value);
  return String(value);
}
