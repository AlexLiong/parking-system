// 极简 hash 路由：解析 #/path?query，触发 onRoute(path, params)。
// 无历史栈依赖，刷新/分享链接均可直达；鉴权守卫由 app.js 在 onRoute 中处理。

export function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '';
  const [pathPart, queryPart] = raw.split('?');
  const params = {};
  new URLSearchParams(queryPart || '').forEach((v, k) => { params[k] = v; });
  return { path: pathPart || '', params };
}

export class Router {
  constructor(onRoute, { fallback = '/monitor' } = {}) {
    this.onRoute = onRoute;
    this.fallback = fallback;
    this._bound = () => this._handle();
  }

  start() {
    window.addEventListener('hashchange', this._bound);
    if (!location.hash) location.replace(`#${this.fallback}`);
    this._handle();
  }

  navigate(path, { replace = false } = {}) {
    const target = path.startsWith('#') ? path : `#${path}`;
    if (location.hash === target) { this._handle(); return; }
    if (replace) location.replace(target);
    else location.hash = target.replace(/^#/, '');
  }

  _handle() {
    let { path, params } = parseHash();
    if (!path) { path = this.fallback; }
    this.onRoute(path, params);
  }

  get current() { return parseHash().path || this.fallback; }

  stop() { window.removeEventListener('hashchange', this._bound); }
}
