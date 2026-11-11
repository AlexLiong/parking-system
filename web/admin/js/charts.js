// 轻量 Canvas 图表：柱状（分组）/折线/环形。无第三方库，DPR 自适应，离线可用。
// 约定：所有函数接受 canvas 元素与数据配置，负责清空并重绘；数据为空时绘制占位。

const AXIS = '#c8cfdb';
const GRID = '#eef1f6';
const INK = '#5a6675';
const FONT = '11px -apple-system, "PingFang SC", system-ui, sans-serif';

function prepare(canvas, cssHeight) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.parentElement?.clientWidth || 600;
  const h = cssHeight || 240;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.height = `${h}px`;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.font = FONT;
  return { ctx, w, h };
}

function niceMax(v) {
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = 10 ** exp;
  const n = v / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * base;
}

function drawEmpty(ctx, w, h, text = '暂无数据') {
  ctx.fillStyle = '#8a94a3';
  ctx.font = '13px -apple-system, "PingFang SC", system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(text, w / 2, h / 2);
  ctx.textAlign = 'left';
}

function yTicks(maxV, count = 4) {
  const step = maxV / count;
  return Array.from({ length: count + 1 }, (_, i) => i * step);
}

/**
 * 分组柱状图。
 * data: { labels:[...], series:[{name, values:[...], color}], height, yFormat(v), valueLabels }
 */
export function barChart(canvas, data) {
  const { labels = [], series = [], height = 240, yFormat = (v) => String(Math.round(v)) } = data;
  const { ctx, w, h } = prepare(canvas, height);
  if (!labels.length || !series.length || series.every((s) => !s.values?.length)) {
    drawEmpty(ctx, w, h); return;
  }
  const padL = 46, padR = 12, padT = 14, padB = 34;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const maxV = niceMax(Math.max(1, ...series.flatMap((s) => s.values.map((v) => Number(v) || 0))));

  // 网格 + Y 轴刻度
  ctx.strokeStyle = GRID; ctx.fillStyle = INK; ctx.lineWidth = 1;
  yTicks(maxV).forEach((t) => {
    const y = padT + plotH - (t / maxV) * plotH;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.textAlign = 'right'; ctx.fillText(yFormat(t), padL - 6, y + 3);
  });
  // 轴线
  ctx.strokeStyle = AXIS;
  ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, padT + plotH);
  ctx.lineTo(w - padR, padT + plotH); ctx.stroke();

  const groupW = plotW / labels.length;
  const barGap = 4;
  const barW = Math.max(3, (groupW - barGap * (series.length + 1)) / series.length);
  ctx.textAlign = 'center';
  labels.forEach((lb, i) => {
    const gx = padL + i * groupW;
    series.forEach((s, si) => {
      const v = Number(s.values[i]) || 0;
      const bh = (v / maxV) * plotH;
      const x = gx + barGap + si * (barW + barGap);
      const y = padT + plotH - bh;
      ctx.fillStyle = s.color || '#2f6df6';
      const r = Math.min(3, barW / 2, bh);
      roundRectTop(ctx, x, y, barW, bh, bh > 0 ? r : 0);
      ctx.fill();
    });
    // X 轴标签（过多时抽稀）
    if (labels.length <= 16 || i % Math.ceil(labels.length / 16) === 0) {
      ctx.fillStyle = INK;
      ctx.fillText(String(lb), gx + groupW / 2, padT + plotH + 16);
    }
  });
  ctx.textAlign = 'left';
}

function roundRectTop(ctx, x, y, w, h, r) {
  if (h <= 0) { ctx.beginPath(); ctx.rect(x, y, w, 0); return; }
  r = Math.max(0, Math.min(r, h, w / 2));
  ctx.beginPath();
  ctx.moveTo(x, y + h);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h);
  ctx.closePath();
}

/**
 * 折线图（可多条 + 面积填充）。
 * data: { labels, series:[{name, values, color, fill}], height, yFormat }
 */
export function lineChart(canvas, data) {
  const { labels = [], series = [], height = 240, yFormat = (v) => String(Math.round(v)) } = data;
  const { ctx, w, h } = prepare(canvas, height);
  if (!labels.length || !series.length) { drawEmpty(ctx, w, h); return; }
  const padL = 46, padR = 12, padT = 14, padB = 34;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const maxV = niceMax(Math.max(1, ...series.flatMap((s) => s.values.map((v) => Number(v) || 0))));

  ctx.strokeStyle = GRID; ctx.fillStyle = INK; ctx.lineWidth = 1;
  yTicks(maxV).forEach((t) => {
    const y = padT + plotH - (t / maxV) * plotH;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.textAlign = 'right'; ctx.fillText(yFormat(t), padL - 6, y + 3);
  });
  ctx.strokeStyle = AXIS;
  ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, padT + plotH);
  ctx.lineTo(w - padR, padT + plotH); ctx.stroke();

  const n = labels.length;
  const xAt = (i) => padL + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v) => padT + plotH - ((Number(v) || 0) / maxV) * plotH;

  series.forEach((s) => {
    const color = s.color || '#2f6df6';
    if (s.fill) {
      ctx.beginPath();
      ctx.moveTo(xAt(0), padT + plotH);
      s.values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
      ctx.lineTo(xAt(n - 1), padT + plotH);
      ctx.closePath();
      ctx.fillStyle = hexA(color, 0.12); ctx.fill();
    }
    ctx.beginPath();
    s.values.forEach((v, i) => (i ? ctx.lineTo(xAt(i), yAt(v)) : ctx.moveTo(xAt(i), yAt(v))));
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
    // 数据点
    ctx.fillStyle = color;
    s.values.forEach((v, i) => {
      ctx.beginPath(); ctx.arc(xAt(i), yAt(v), 2.4, 0, Math.PI * 2); ctx.fill();
    });
  });

  ctx.fillStyle = INK; ctx.textAlign = 'center';
  labels.forEach((lb, i) => {
    if (n <= 16 || i % Math.ceil(n / 16) === 0) {
      ctx.fillText(String(lb), xAt(i), padT + plotH + 16);
    }
  });
  ctx.textAlign = 'left';
}

/**
 * 环形图。data: { segments:[{label, value, color}], height, centerTop, centerBottom }
 */
export function donut(canvas, data) {
  const { segments = [], height = 220, centerTop = '', centerBottom = '' } = data;
  const { ctx, w, h } = prepare(canvas, height);
  const total = segments.reduce((a, s) => a + (Number(s.value) || 0), 0);
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 12, r = R * 0.62;
  if (total <= 0) { drawEmpty(ctx, w, h); return; }
  let start = -Math.PI / 2;
  segments.forEach((s) => {
    const val = Number(s.value) || 0;
    if (val <= 0) return;
    const ang = (val / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(cx, cy, R, start, start + ang);
    ctx.arc(cx, cy, r, start + ang, start, true);
    ctx.closePath();
    ctx.fillStyle = s.color || '#2f6df6';
    ctx.fill();
    start += ang;
  });
  // 中心文字
  ctx.fillStyle = '#1f2733'; ctx.textAlign = 'center';
  ctx.font = 'bold 22px -apple-system, "PingFang SC", system-ui, sans-serif';
  ctx.fillText(String(centerTop), cx, cy + 2);
  ctx.font = FONT; ctx.fillStyle = INK;
  ctx.fillText(String(centerBottom), cx, cy + 20);
  ctx.textAlign = 'left';
}

function hexA(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return `rgba(47,109,246,${a})`;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// 供视图生成图例 DOM
export const PALETTE = ['#2f6df6', '#17a673', '#e08a1e', '#d94848', '#2f9fd8', '#8b5cf6', '#0ea5a4'];
