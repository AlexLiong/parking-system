// 面板拖动/最小化/位置持久化（ui/PanelDrag.js）。
// 仅标题栏可拖动；输入、按钮、下拉、链接不触发拖动；支持 Pointer Events / 触屏。
const DEFAULT_KEY = 'sim3d.panelLayout.v2';
const MARGIN = 6;
const TOPBAR = 46;
const INTERACTIVE = 'input,button,select,textarea,a,[contenteditable="true"],[role="button"]';

export function clampPanelPosition(left, top, width, height, viewport = {}) {
  const vw = Number(viewport.width) || 1024;
  const vh = Number(viewport.height) || 768;
  const w = Math.max(80, Number(width) || 0);
  const h = Math.max(32, Number(height) || 0);
  const maxLeft = Math.max(MARGIN, vw - w - MARGIN);
  const maxTop = Math.max(TOPBAR, vh - h - MARGIN);
  return {
    left: Math.max(MARGIN, Math.min(maxLeft, Number(left) || 0)),
    top: Math.max(TOPBAR, Math.min(maxTop, Number(top) || 0)),
  };
}

export function readPanelLayout(storage, key = DEFAULT_KEY) {
  if (!storage) return {};
  try {
    const raw = storage.getItem(key);
    const obj = raw ? JSON.parse(raw) : {};
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {};
  } catch (_) {
    return {};
  }
}

export function writePanelLayout(storage, layout, key = DEFAULT_KEY) {
  if (!storage) return false;
  try { storage.setItem(key, JSON.stringify(layout || {})); return true; } catch (_) { return false; }
}

export class PanelManager {
  constructor({
    ids = ['freeboard', 'hud', 'recognition-input', 'scenario-progress', 'control'],
    storage = (typeof localStorage !== 'undefined' ? localStorage : null),
    key = DEFAULT_KEY,
    doc = document,
    win = window,
  } = {}) {
    this.doc = doc;
    this.win = win;
    this.storage = storage;
    this.key = key;
    this.layout = readPanelLayout(storage, key);
    this.panels = [];
    for (const id of ids) {
      const el = doc.getElementById(id);
      if (el) this._setup(el);
    }
    this._onResize = () => {
      for (const p of this.panels) this._applySaved(p, false);
    };
    win.addEventListener?.('resize', this._onResize);
  }

  _setup(panel) {
    const title = panel.querySelector('.panel-title');
    if (!title) return;
    title.classList.add('panel-drag-handle');
    let minBtn = title.querySelector('.panel-minimize');
    if (!minBtn) {
      minBtn = this.doc.createElement('button');
      minBtn.className = 'panel-minimize';
      minBtn.type = 'button';
      minBtn.setAttribute('aria-label', '最小化面板');
      minBtn.title = '最小化';
      minBtn.textContent = '–';
      title.appendChild(minBtn);
    }
    const rec = { panel, title, minBtn };
    this.panels.push(rec);
    minBtn.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      this.toggleMinimized(panel);
    });
    title.addEventListener('pointerdown', (e) => this._onPointerDown(e, rec));
    this._applySaved(rec, true);
  }

  _applySaved(rec, initial) {
    const { panel } = rec;
    const saved = this.layout?.[panel.id];
    if (!saved) return;
    if (saved.minimized) panel.classList.add('minimized');
    if (Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
      // hidden 面板用保存值直接定位，等显示后仍保持。
      panel.style.left = `${saved.left}px`;
      panel.style.top = `${saved.top}px`;
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
    }
    if (initial && saved.minimized) rec.minBtn.textContent = '+';
  }

  _onPointerDown(e, rec) {
    if (e.button != null && e.button !== 0) return;
    if (e.target?.closest?.(INTERACTIVE) || e.target?.closest?.('.panel-minimize')) return;
    const { panel } = rec;
    const rect = panel.getBoundingClientRect();
    const start = { x: e.clientX, y: e.clientY, left: rect.left, top: rect.top };
    const width = rect.width, height = rect.height;
    const pointerId = e.pointerId;
    panel.style.left = `${start.left}px`;
    panel.style.top = `${start.top}px`;
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
    panel.style.zIndex = '35';
    panel.classList.add('dragging');
    this.doc.body?.classList.add('panel-dragging');
    try { rec.title.setPointerCapture?.(pointerId); } catch (_) { /* ignore */ }
    const move = (ev) => {
      const pos = clampPanelPosition(start.left + ev.clientX - start.x,
        start.top + ev.clientY - start.y, width, height,
        { width: this.win.innerWidth, height: this.win.innerHeight });
      panel.style.left = `${pos.left}px`;
      panel.style.top = `${pos.top}px`;
    };
    const up = () => {
      rec.title.removeEventListener('pointermove', move);
      rec.title.removeEventListener('pointerup', up);
      rec.title.removeEventListener('pointercancel', up);
      panel.classList.remove('dragging');
      this.doc.body?.classList.remove('panel-dragging');
      try { rec.title.releasePointerCapture?.(pointerId); } catch (_) { /* ignore */ }
      this._save(panel.id, {
        left: parseFloat(panel.style.left), top: parseFloat(panel.style.top),
        minimized: panel.classList.contains('minimized'),
      });
      panel.style.zIndex = '';
    };
    rec.title.addEventListener('pointermove', move);
    rec.title.addEventListener('pointerup', up);
    rec.title.addEventListener('pointercancel', up);
  }

  toggleMinimized(panelOrId) {
    const panel = typeof panelOrId === 'string' ? this.doc.getElementById(panelOrId) : panelOrId;
    if (!panel) return false;
    const minimized = !panel.classList.contains('minimized');
    panel.classList.toggle('minimized', minimized);
    const rec = this.panels.find((p) => p.panel === panel);
    if (rec) {
      rec.minBtn.textContent = minimized ? '+' : '–';
      rec.minBtn.title = minimized ? '恢复' : '最小化';
      rec.minBtn.setAttribute('aria-label', minimized ? '恢复面板' : '最小化面板');
    }
    const saved = this.layout?.[panel.id] || {};
    this._save(panel.id, {
      left: Number.isFinite(saved.left) ? saved.left : parseFloat(panel.style.left),
      top: Number.isFinite(saved.top) ? saved.top : parseFloat(panel.style.top),
      minimized,
    });
    return minimized;
  }

  _save(id, next) {
    const clean = {};
    if (Number.isFinite(next.left)) clean.left = next.left;
    if (Number.isFinite(next.top)) clean.top = next.top;
    if (typeof next.minimized === 'boolean') clean.minimized = next.minimized;
    this.layout[id] = { ...(this.layout[id] || {}), ...clean };
    writePanelLayout(this.storage, this.layout, this.key);
  }

  reset() {
    this.layout = {};
    writePanelLayout(this.storage, {}, this.key);
    for (const { panel } of this.panels) {
      panel.classList.remove('minimized');
      panel.style.left = ''; panel.style.top = '';
      panel.style.right = ''; panel.style.bottom = '';
    }
  }
}
