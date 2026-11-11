// Presentation only: no admission, payment or barrier side effects.
//
// P2-01：入口/出口闸口屏始终展示“余位 N”（余位来自 snapshot/space_update/HUD 同源数据，
//       空闲/识别/收费状态下都保留）。
// P2-02：出口 fee_quote/payment_completed 时把出口抓拍照片（/media 鉴权路径）显示在屏幕上，
//       同时保留费用/车牌/余位文字；照片经注入的 loadImage 用 token fetch→blob 解码，失败静默回退文字屏。
export class GateDisplay {
  constructor(write, opts = {}) {
    this.write = write;
    this.loadImage = typeof opts.loadImage === 'function' ? opts.loadImage : null;
    this.quotes = {};
    this.free = null;
    this.states = {};
    this._photoCache = new Map();
    this._photoRev = { in: 0, out: 0 };
    this.reset();
  }

  reset() {
    this.quotes = {};
    this._photoRev = { in: (this._photoRev?.in || 0) + 1, out: (this._photoRev?.out || 0) + 1 };
    this.states.in = { kind: 'idle', lines: ['入口欢迎您', '请停车识别'], photo: null, photoUrl: null };
    this.states.out = { kind: 'idle', lines: ['出口', '请停车识别'], photo: null, photoUrl: null };
    this._paint('in');
    this._paint('out');
  }

  /** 余位数据（free={total,A,B,C,D} 或 {total}）；入口/出口屏都显示“余位 N”。 */
  setFree(free) {
    this.free = free || null;
    this._paint('in');
    this._paint('out');
  }

  apply(m) {
    if (!m) return;
    if (m.type === 'space_update') { this.setFree(m.free); return; }
    const gate = m.gate || (['fee_quote', 'payment_completed'].includes(m.type) ? 'out' : null);
    if (!['in', 'out'].includes(gate)) return;
    if (m.type === 'plate_recognized') {
      this.states[gate] = {
        kind: 'recognized',
        lines: [gate === 'in' ? '入口识别' : '出口识别', m.plate || '未识别',
          m.need_manual ? '待人工确认' : gate === 'in' ? '请通行' : '正在计费'],
        photo: null, photoUrl: null,
      };
      this._paint(gate);
    } else if (m.type === 'fee_quote') {
      const photoUrl = this._photoUrl(m.exit_photo || m.entry_photo);
      this.quotes[gate] = { plate: m.plate, minutes: m.minutes, amount: m.amount,
        exit_photo: photoUrl };
      this.states[gate] = {
        kind: 'fee',
        lines: [m.plate || '出口', `停车 ${Math.round(m.minutes || 0)} 分钟`,
          `应缴 ¥${Number(m.amount || 0).toFixed(2)}`, '请缴费'],
        photo: this._photoCache.get(photoUrl) || null, photoUrl, photoLabel: '出口抓拍',
      };
      this._paint(gate);
      this._loadPhoto(gate, photoUrl, 'fee');
    } else if (m.type === 'payment_completed') {
      const q = this.quotes[gate];
      const photoUrl = this._photoUrl(m.exit_photo || m.entry_photo) || q?.exit_photo || null;
      this.states[gate] = {
        kind: 'paid',
        lines: [m.plate || '出口', ...(q?.plate === m.plate ? [`停车 ${Math.round(q.minutes || 0)} 分钟`] : []),
          `已缴 ¥${Number(m.amount || 0).toFixed(2)}`, '请通行'],
        photo: this._photoCache.get(photoUrl) || q?.photo || null, photoUrl, photoLabel: '出口抓拍',
      };
      this._paint(gate);
      this._loadPhoto(gate, photoUrl, 'paid');
    } else if (m.type === 'barrier_state' && m.state === 'CLOSED') {
      delete this.quotes[gate];
      this.states[gate] = {
        kind: 'idle', lines: [gate === 'in' ? '入口欢迎您' : '出口', '请停车识别'],
        photo: null, photoUrl: null,
      };
      this._paint(gate);
    }
  }

  _paint(gate) {
    const st = this.states[gate];
    if (!st) return;
    const lines = this._withFree(st.lines || []);
    const opts = st.photo ? { photo: st.photo, photoLabel: st.photoLabel || '出口抓拍' } : {};
    this.write(gate, lines, opts);
  }

  _withFree(lines) {
    const total = this.free?.total ?? this.free?.free_total;
    if (total == null) return lines.slice();
    return [...lines, `余位 ${total}`];
  }

  async _loadPhoto(gate, url, kind) {
    if (!this.loadImage || !url) return;
    if (this._photoCache.has(url)) return;
    const rev = ++this._photoRev[gate];
    try {
      const img = await this.loadImage(url);
      if (!img) return;
      this._photoCache.set(url, img);
      const st = this.states[gate];
      if (rev !== this._photoRev[gate] || !st || st.photoUrl !== url) return; // 已被新事件覆盖
      if (st.kind !== kind && st.kind !== 'paid') return;
      st.photo = img;
      this._paint(gate);
    } catch (_) { /* 照片失败保留文字屏，不阻断闸口业务 */ }
  }

  /** 把服务端原始图片路径映射到前端可访问 URL（/media 鉴权同一路径，由 main 的 loadImage 带 token）。 */
  _photoUrl(raw) {
    if (!raw) return null;
    const s = String(raw).replace(/\\/g, '/');
    if (/^(https?:|data:|blob:|\/)/i.test(s)) return s;
    let i = s.indexOf('/photos/');
    if (i >= 0) return '/media/' + s.slice(i + '/photos/'.length);
    if (s.startsWith('photos/')) return '/media/' + s.slice('photos/'.length);
    if (s.startsWith('datasets/')) return '/' + s;
    i = s.indexOf('/datasets/');
    if (i >= 0) return s.slice(i);
    return null;
  }
}
