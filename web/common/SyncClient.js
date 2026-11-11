// Shared snapshot and durable event cursor for admin and simulator clients.
export class SyncClient {
  constructor({ clientType = 'sim', token, snapshot, events, onSnapshot, onEvent, onStatus }) {
    this.clientType = clientType;
    this.token = token;
    this.fetchSnapshot = snapshot;
    this.fetchEvents = events;
    this.onSnapshot = onSnapshot || (() => {});
    this.onEvent = onEvent || (() => {});
    this.onStatus = onStatus || (() => {});
    this.ws = null;
    this.lastSeq = 0;
    this.sessionId = null;
    this.closed = false;
    this.retry = 0;
    this.queue = Promise.resolve();
    this.timer = null;
  }

  async start() {
    await this.refresh();
    this.connect();
  }

  async refresh() {
    const snap = await this.fetchSnapshot();
    await this.applySnapshot(snap);
  }

  async applySnapshot(snap) {
    this.lastSeq = Number(snap.seq) || 0;
    this.sessionId = snap.session?.id || null;
    await this.onSnapshot(snap);
  }

  connect() {
    if (this.closed) return;
    const credential = this.token();
    if (!credential) {
      this.onStatus(false);
      return;
    }
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const query = new URLSearchParams({ features: 'v2', client: this.clientType,
      since: String(this.lastSeq) });
    if (this.sessionId) query.set('session', this.sessionId);
    const ws = new WebSocket(`${scheme}//${location.host}/ws?${query}`,
      ['v2', `token.${credential}`]);
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; this.onStatus(true); };
    ws.onmessage = ({ data }) => {
      let message;
      try { message = JSON.parse(data); } catch (_) { return; }
      this.queue = this.queue.then(() => this.handle(message)).catch(async () => {
        try { await this.refresh(); } catch (_) { this.onStatus(false); }
      });
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.onStatus(false);
      if (!this.closed) {
        const delay = Math.min(5000, 500 * 2 ** this.retry++);
        this.timer = setTimeout(() => this.connect(), delay);
      }
    };
    ws.onerror = () => ws.close();
  }

  async handle(message) {
    if (message.type === 'welcome' || message.type === 'pong') return;
    if (message.type === 'snapshot') return this.applySnapshot(message.data);
    if (message.type === 'resync_required') return this.refresh();
    if (message.type === 'batch') {
      for (const item of message.events || []) await this.applyEvent(item);
      return;
    }
    if (message.type === 'event') await this.applyEvent(message);
  }

  async applyEvent(event) {
    const seq = Number(event.seq);
    if (!Number.isInteger(seq) || seq <= this.lastSeq) return;
    if (seq !== this.lastSeq + 1) {
      let more = true;
      while (more && this.lastSeq + 1 < seq) {
        const page = await this.fetchEvents(this.lastSeq);
        if (!page.items?.length) break;
        for (const item of page.items) {
          if (item.seq > this.lastSeq) await this.acceptEvent(item);
        }
        more = !!page.has_more;
      }
      if (seq <= this.lastSeq) return;
      if (seq !== this.lastSeq + 1) {
        await this.refresh();
        return;
      }
    }
    await this.acceptEvent(event);
  }

  async acceptEvent(event) {
    if (event.seq <= this.lastSeq) return;
    if (event.seq !== this.lastSeq + 1) throw new Error('event gap');
    this.lastSeq = event.seq;
    if (event.name === 'DEMO_RESET') {
      await this.refresh();
      return;
    }
    await this.onEvent(event.data, event);
  }

  send(payload) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
  }

  close() {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.ws?.close();
  }
}
