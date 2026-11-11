// 通用工具：DOM 构建、格式化、toast/modal、鉴权令牌存储、媒体 URL 归一化。
// 纯原生，无依赖。所有视图共用。
import {locale,t} from './i18n.js';
let domId=0;

// ----------------------------- DOM -----------------------------
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/**
 * 轻量 DOM 构建器。
 * el('div', {class:'card', onclick:fn, html:'<b>x</b>'}, [child, 'text'])
 * - attrs 中 class/id/value/type/placeholder/disabled/checked 等直接赋属性；
 * - on* 作为事件监听；html 设置 innerHTML（调用方负责转义）；其余 setAttribute；
 * - children 支持 元素 / 字符串 / 数组 / null。
 */
export function el(tag, attrs = {}, children = null) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'value') node.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') node[k] = !!v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  appendChildren(node, children);
  // Associate the existing sibling-label pattern without a global observer.
  node.querySelectorAll('label:not([for])').forEach(label=>{
    if(label.querySelector('input,select,textarea'))return;
    const control=label.parentElement.querySelector('input,select,textarea');
    if(control){if(!control.id)control.id=`field-${++domId}`;label.htmlFor=control.id;}
  });
  if(node.classList.contains('empty')){node.setAttribute('role','status');node.setAttribute('aria-live','polite');}
  return node;
}

function appendChildren(node, children) {
  if (children === null || children === undefined || children === false) return;
  if (Array.isArray(children)) { children.forEach((c) => appendChildren(node, c)); return; }
  if (children instanceof Node) { node.appendChild(children); return; }
  node.appendChild(document.createTextNode(String(children)));
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
export function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ----------------------------- 格式化 -----------------------------
export function fmtMoney(v, digits = 2) {
  const n = Number(v || 0);
  return new Intl.NumberFormat(locale,{style:'currency',currency:'CNY',minimumFractionDigits:digits,maximumFractionDigits:digits}).format(n);
}
export function num(v, digits = 0) {
  const n = Number(v || 0);
  return Number.isInteger(n) && digits === 0 ? String(n) : n.toFixed(digits);
}

let businessTimezone = 'Asia/Shanghai';
let serverClockOffsetMs = 0;

export function syncBusinessClock(iso, timezone) {
  if (timezone) {
    try {
      new Intl.DateTimeFormat('en-US', { timeZone: timezone });
      businessTimezone = timezone;
    } catch (_) { /* Keep the configured default if the server value is invalid. */ }
  }
  const serverMs = Date.parse(iso);
  if (Number.isFinite(serverMs)) serverClockOffsetMs = serverMs - Date.now();
}

/** Parse offset-aware API times and legacy naive UTC strings for display. */
export function fmtTime(iso, { withSec = true, utcTag = false } = {}) {
  if (!iso) return '—';
  const value = String(iso);
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  const out = new Intl.DateTimeFormat(locale==='en'?'en-GB':'sv-SE', {
    timeZone: businessTimezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', ...(withSec ? { second: '2-digit' } : {}),
    hour12: false,
  }).format(date);
  if (!utcTag) return out;
  const zoneName = new Intl.DateTimeFormat('en-US', {
    timeZone: businessTimezone, timeZoneName: 'shortOffset',
  }).formatToParts(date).find(part => part.type === 'timeZoneName')?.value || 'GMT';
  return `${out} ${zoneName.replace('GMT', 'UTC')}`;
}
export function fmtDate(iso) { return iso ? fmtTime(iso, { withSec: false }).slice(0, 10) : '—'; }

export function fmtMinutes(min) {
  const m = Math.max(0, Math.round(Number(min || 0)));
  const h = Math.floor(m / 60), r = m % 60;
  if (h <= 0) return `${r} 分钟`;
  return r ? `${h} 小时 ${r} 分` : `${h} 小时`;
}
export function clockNow() {
  return fmtTime(new Date(Date.now() + serverClockOffsetMs).toISOString()).slice(11);
}

// 业务枚举 → 中文
export const RECORD_STATUS = { in: '在场', out: '已离场', pending: '待缴费', anomaly: '异常', abnormal: '异常放行', aborted: '会话作废' };
// 后端 BarrierState 枚举值为大写（CLOSED/OPENING/KEEP_OPEN/CLOSING/FAULT），此处大小写不敏感映射
export const BARRIER_STATE = {
  closed: '关闭', open: '开启', opening: '开启中', keep_open: '保持开启',
  closing: '关闭中', fault: '故障', stopped: '已停杆',
};
export const LIST_TYPE = { white: '白名单', black: '黑名单', monthly: '月租' };
export const INCIDENT_KIND = {
  camera_fault: '摄像头故障', recognition_failed: '识别失败', barrier_fault: '道闸故障',
  tailgating: '跟车闯入', no_entry_record: '无入场记录', payment_required: '待缴费',
  consistency: '车位对账', unknown_plate: '未知车牌', timeout: '超时',
};
export function recordStatus(s) { return RECORD_STATUS[s] || s || '—'; }
export function barrierState(s) { return BARRIER_STATE[String(s || '').toLowerCase()] || s || '—'; }
// 道闸是否处于“抬起/可通行”语义（用于状态色）
export function barrierIsOpen(s) {
  const k = String(s || '').toLowerCase();
  return k === 'open' || k === 'opening' || k === 'keep_open';
}
export function listType(t) { return LIST_TYPE[t] || t || '—'; }
export function incidentKind(k) { return INCIDENT_KIND[k] || k || '—'; }

export function plateClass(color) {
  const c = String(color || '').toLowerCase();
  if (c.includes('green') || c.includes('绿') || c === 'new_energy') return 'green';
  if (c.includes('yellow') || c.includes('黄')) return 'yellow';
  if (c.includes('blue') || c.includes('蓝')) return 'blue';
  return 'unknown';
}

/**
 * 把后端存储的图片路径归一化为可访问 URL：
 * - 绝对 fs 路径含 /photos/ → /media/…；含 /datasets/ → /datasets/…
 * - 相对 datasets/… → /datasets/…；crops|uploads/… → /media/…
 * - 已是 http(s) 或 /media /datasets 前缀 → 原样
 */
export function mediaUrl(p) {
  if (!p) return null;
  let s = String(p).trim();
  if (!s) return null;
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith('/media/') || s.startsWith('/datasets/')) return s;
  const norm = s.replace(/\\/g, '/');
  let i = norm.indexOf('/photos/');
  if (i >= 0) return '/media/' + norm.slice(i + '/photos/'.length);
  i = norm.indexOf('/datasets/');
  if (i >= 0) return '/datasets/' + norm.slice(i + '/datasets/'.length);
  if (norm.startsWith('datasets/')) return '/' + norm;
  if (norm.startsWith('photos/')) return '/media/' + norm.slice('photos/'.length);
  if (norm.startsWith('crops/') || norm.startsWith('uploads/')) return '/media/' + norm;
  if (norm.startsWith('/')) return norm;
  return '/media/' + norm;
}

/** 构建 query string（忽略 null/undefined/''）。 */
export function qs(params = {}) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') continue;
    sp.append(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

// ----------------------------- 鉴权存储 -----------------------------
const TK = 'parking.admin.token';
const US = 'parking.admin.user';
export const store = {
  getToken: () => localStorage.getItem(TK),
  setToken: (t) => (t ? localStorage.setItem(TK, t) : localStorage.removeItem(TK)),
  getUser: () => { try { return JSON.parse(localStorage.getItem(US) || 'null'); } catch (_) { return null; } },
  setUser: (u) => (u ? localStorage.setItem(US, JSON.stringify(u)) : localStorage.removeItem(US)),
  clear: () => { localStorage.removeItem(TK); localStorage.removeItem(US); },
};

// ----------------------------- Toast -----------------------------
export function toast(message, type = 'info', ttl = 3600) {
  const root = $('#toasts') || document.body;
  const box = el('div', { class: `toast ${type}`, role:type==='danger'?'alert':'status' });
  box.appendChild(el('div', { class: 't-body', text: message }));
  root.appendChild(box);
  setTimeout(() => { box.classList.add('hide'); setTimeout(() => box.remove(), 320); }, ttl);
  return box;
}

// ----------------------------- Modal -----------------------------
/**
 * 打开模态框。opts: {title, body(Node|html string), wide, okText, cancelText,
 *   danger, onOk(close)->bool|Promise<bool>, hideFooter}
 * 返回 close 函数。onOk 返回 false 阻止关闭（如校验失败）。
 */
export function modal(opts = {}) {
  const root = $('#modal-root') || document.body;
  const restoreFocus=document.activeElement;
  const background=Array.from(document.querySelectorAll('#app,.modal-mask')).map(node=>({node,inert:node.inert}));
  background.forEach(({node})=>node.inert=true);
  const titleId=`dialog-title-${++domId}`;
  const mask = el('div', { class: 'modal-mask' });
  const box = el('div', { class: 'modal' + (opts.wide ? ' wide' : ''), role:'dialog','aria-modal':'true','aria-labelledby':titleId,tabindex:-1 });
  const head = el('div', { class: 'modal-head' }, [
    el('h3', { id:titleId,text: opts.title || t('ok') }),
    el('button', { class: 'x', html: '&times;','aria-label':t('close'), onclick: () => close() }),
  ]);
  const body = el('div', { class: 'modal-body' });
  if (opts.body instanceof Node) body.appendChild(opts.body);
  else if (opts.body != null) body.innerHTML = opts.body;

  box.appendChild(head);
  box.appendChild(body);

  let foot = null;
  if (!opts.hideFooter) {
    const okBtn = el('button', {
      class: `btn ${opts.danger ? 'danger' : 'primary'}`, text: opts.okText || t('ok'),
      onclick: async () => {
        if (!opts.onOk) return close();
        okBtn.disabled = true;
        try {
          const r = await opts.onOk(close, body);
          if (r !== false) close();
        } finally { okBtn.disabled = false; }
      },
    });
    foot = el('div', { class: 'modal-foot' }, [
      el('button', { class: 'btn ghost', text: opts.cancelText || t('cancel'), onclick: () => close() }),
      okBtn,
    ]);
    box.appendChild(foot);
  }

  mask.appendChild(box);
  mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
  root.appendChild(mask);
  mask.parkingClose=close;
  let closed=false;
  queueMicrotask(()=>{if(!closed)(box.querySelector('input:not([disabled]),textarea,select,button')||box).focus();});

  function close() {
    if(closed)return;closed=true;
    document.removeEventListener('keydown', onKey);
    mask.remove();
    background.forEach(({node,inert})=>node.inert=inert);
    if(restoreFocus?.isConnected&&!restoreFocus.closest('[inert]'))restoreFocus.focus();
    if (opts.onClose) opts.onClose();
  }
  function onKey(e) {
    if(document.querySelector('.modal-mask:last-child')!==mask||mask.inert)return;
    if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close();return;}
    if(e.key!=='Tab')return;
    const items=Array.from(box.querySelectorAll('button,a[href],input,select,textarea,[tabindex="0"]')).filter(n=>!n.disabled&&n.getClientRects().length);
    const first=items[0]||box,last=items.at(-1)||box;
    if(e.shiftKey&&(document.activeElement===first||!box.contains(document.activeElement))){e.preventDefault();last.focus();}
    else if(!e.shiftKey&&(document.activeElement===last||!box.contains(document.activeElement))){e.preventDefault();first.focus();}
  }
  document.addEventListener('keydown', onKey);
  return close;
}

export function dismissModals(){Array.from(document.querySelectorAll('.modal-mask')).reverse().forEach(m=>m.parkingClose?.());}

/** 确认框（Promise<bool>）。 */
export function confirmBox(message, { title = '请确认', danger = false, okText = '确定' } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const close = modal({
      title, body: el('div', { text: message }), danger, okText,
      onOk: () => { done = true; resolve(true); },
      onClose: () => { if (!done) resolve(false); },
    });
    // 关闭遮罩/取消时 onClose 触发 resolve(false)
    void close;
  });
}

/** 输入框弹窗（Promise<string|null>）。 */
export function promptBox(message, { title = '请输入', value = '', placeholder = '' } = {}) {
  return new Promise((resolve) => {
    const input = el('input', { value, placeholder });
    let ok = false;
    modal({
      title,
      body: el('div', {}, [el('div', { class: 'mb', text: message }), input]),
      okText: '确定',
      onOk: () => { ok = true; resolve(input.value.trim()); },
      onClose: () => { if (!ok) resolve(null); },
    });
    setTimeout(() => input.focus(), 50);
  });
}

/**
 * 统一加载失败卡片（P2-10）：可见错误提示 + 重试按钮 + 初始焦点 + aria-live。
 * retry 应由视图提供，负责清空/重载自身区域；成功后错误卡片自然被视图内容替换。
 */
export function loadErrorCard(message, retry, { title = t('loadFailed'), retryText = t('retry') } = {}) {
  const messageEl = el('div', { class: 'load-error-message', text: message || t('loadFailed') });
  const retryBtn = el('button', { type: 'button', class: 'btn primary', text: retryText });
  const box = el('div', {
    class: 'load-error-card', role: 'alert', 'aria-live': 'assertive', 'aria-atomic': 'true',
  }, [
    el('div', { class: 'load-error-head' }, [
      el('span', { class: 'load-error-icon', 'aria-hidden': 'true', text: '!' }),
      el('div', { class: 'load-error-title', text: title }),
    ]),
    messageEl,
    el('div', { class: 'load-error-actions' }, [retryBtn]),
  ]);
  retryBtn.addEventListener('click', async () => {
    if (retryBtn.disabled) return;
    retryBtn.disabled = true;
    const original = retryBtn.textContent;
    retryBtn.textContent = t('loading');
    try {
      await retry();
    } catch (e) {
      messageEl.textContent = e?.message || String(e);
      retryBtn.disabled = false;
      retryBtn.textContent = original;
      retryBtn.focus();
    }
  });
  // 让键盘/读屏用户立即到达重试动作。
  queueMicrotask(() => retryBtn.focus());
  return box;
}
