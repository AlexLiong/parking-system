// 管理端应用外壳：构建侧栏 + 顶栏骨架，装配 hash 路由与鉴权守卫，挂载各视图。
// 无框架、无构建；视图以 ES 模块静态导入（完全离线）。
import {locale,t,setLocale,translateDom,startI18nObserver} from './i18n.js';
import { Router } from './router.js';
import { el, clear, store, syncBusinessClock, toast, loadErrorCard } from './util.js';
import { Api, setUnauthorizedHandler } from './api.js';

import loginView from './views/login.js';
import monitorView from './views/monitor.js';
import recognizeView from './views/recognize.js';
import scenarioView from './views/scenario.js';
import recordsView from './views/records.js';
import feeView from './views/fee.js';
import layoutView from './views/layout.js';
import statsView from './views/stats.js';
import incidentsView from './views/incidents.js';
import listsView from './views/lists.js';
import findCarView from './views/find-car.js';

const ROUTES = {
  '/login': loginView,
  '/monitor': monitorView,
  '/recognize': recognizeView,
  '/scenario': scenarioView,
  '/records': recordsView,
  '/find-car': findCarView,
  '/fee-rules': feeView,
  '/layout': layoutView,
  '/stats': statsView,
  '/incidents': incidentsView,
  '/lists': listsView,
};

const NAV = [
  { group: '监控' },
  { path: '/monitor', label: t('monitor'), icon: '📡' },
  { path: '/recognize', label: t('recognize'), icon: '🔍' },
  { group: '运营' },
  { path: '/find-car', label: t('find'), icon: '📍' },
  { path: '/scenario', label: t('scenario'), icon: '🎬', adminOnly: true },
  { path: '/records', label: t('records'), icon: '🧾' },
  { path: '/stats', label: t('stats'), icon: '📊' },
  { group: '配置' },
  { path: '/fee-rules', label: t('fee'), icon: '💰', adminOnly: true },
  { path: '/layout', label: t('layout'), icon: '🗺️', adminOnly: true },
  { path: '/lists', label: t('lists'), icon: '📋', adminOnly: true },
  { group: '运维' },
  { path: '/incidents', label: t('incidents'), icon: '⚠️' },

];


const ADMIN_ONLY_PATHS = new Set(NAV.filter((item) => item.adminOnly).map((item) => item.path));

function currentRole() {
  return (store.getUser()?.role || 'admin');
}

function isAdmin() {
  return currentRole() === 'admin';
}

function visibleNav() {
  const out = [];
  let pendingGroup = null;
  for (const item of NAV) {
    if (item.group) { pendingGroup = item; continue; }
    if (item.adminOnly && !isAdmin()) continue;
    if (pendingGroup) { out.push(pendingGroup); pendingGroup = null; }
    out.push(item);
  }
  return out;
}

const appRoot = document.getElementById('app');
let shell = null;          // {navEl, titleEl, userEl, viewEl}
let currentCleanup = null;
let mountToken = 0;

const router = new Router(onRoute, { fallback: '/monitor' });
const ctx = { navigate: (p, o) => router.navigate(p, o), api: Api, toast };

// 令牌失效 → 跳登录
setUnauthorizedHandler(() => {
  toast('登录已失效，请重新登录', 'warn');
  if (router.current !== '/login') router.navigate('/login', { replace: true });
  else onRoute('/login', {});
});

function isAuthenticated() { return !!store.getToken(); }

function onRoute(path, params) {
  const view = ROUTES[path];
  // 未知路由 → 监控（或登录）
  if (!view) { router.navigate(isAuthenticated() ? '/monitor' : '/login', { replace: true }); return; }
  // 鉴权守卫
  if (!view.public && !isAuthenticated()) { router.navigate('/login', { replace: true }); return; }
  if (path === '/login' && isAuthenticated()) { router.navigate('/monitor', { replace: true }); return; }
  // 配置类页面仅管理员可进入；操作员直达 URL 时回到监控页。
  if (ADMIN_ONLY_PATHS.has(path) && !isAdmin()) {
    toast('该功能仅管理员可用', 'warn');
    router.navigate('/monitor', { replace: true });
    return;
  }

  if (view.public) mountBare(view, params);
  else mountInShell(path, view, params);
}

// 登录页：全屏无骨架
function mountBare(view, params) {
  runCleanup();
  appRoot.className = '';
  clear(appRoot);
  shell = null;
  setDocumentTitle('/login');
  const token = ++mountToken;
  Promise.resolve(view.render(appRoot, { ...ctx, params })).then((c) => {
    translateDom(appRoot);
    if (token === mountToken) currentCleanup = typeof c === 'function' ? c : null;
  }).catch((e) => fatal(e));
}

// 业务页：侧栏 + 顶栏骨架
function mountInShell(path, view, params) {
  ensureShell();
  setActiveNav(path);
  runCleanup();
  const viewEl = shell.viewEl;
  clear(viewEl);
  shell.titleEl.textContent = NAV.find(n=>n.path===path)?.label || view.title || '';
  shell.mobileNav.value=path;
  setDocumentTitle(path);
  const token = ++mountToken;

  // 占位：视图 render() 会同步追加其骨架（首部/卡片）后才 await 数据，
  // 故 render 调用返回后即可安全移除占位（持有直接引用，不误删视图自身的加载态）。
  const placeholder = el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…']);
  viewEl.appendChild(placeholder);

  let result;
  try {
    result = view.render(viewEl, { ...ctx, params });
  } catch (e) { placeholder.remove(); fatal(e); return; }
  placeholder.remove();

  Promise.resolve(result).then((c) => {
    translateDom(appRoot);
    if (token !== mountToken) return;   // 已切走，丢弃迟到的 cleanup
    currentCleanup = typeof c === 'function' ? c : null;
  }).catch((e) => { if (token === mountToken) fatal(e); });
}

function runCleanup() {
  if (typeof currentCleanup === 'function') {
    try { currentCleanup(); } catch (_) { /* ignore */ }
  }
  currentCleanup = null;
}

function fatal(e) {
  console.error('[admin] 视图渲染失败', e);
  const message = e?.message || String(e);
  const target = shell?.viewEl || appRoot;
  clear(target);
  target.appendChild(loadErrorCard(message, () => onRoute(router.current || '/monitor', {}),
    { title: t('loadFailed') }));
  toast(`${t('loadFailed')}：${message}`, 'danger', 6000);
}

function setDocumentTitle(path) {
  if (locale !== 'en') {
    document.title = '停车场收费系统 · 管理端';
    return;
  }
  const nav = NAV.find((n) => n.path === path);
  document.title = nav ? `${nav.label} · Parking Admin` : 'Parking Admin';
}

function ensureShell() {
  if (shell) { appRoot.className = ''; return; }
  clear(appRoot);
  appRoot.className = '';

  const navEl = el('nav', { class: 'nav','aria-label':t('nav') });
  const navItems=visibleNav();
  const mobileNav=el('select',{class:'mobile-nav','aria-label':t('nav'),onchange:e=>router.navigate(e.target.value)},navItems.filter(n=>n.path).map(n=>el('option',{value:n.path,text:n.label})));
  const language=el('select',{class:'language-select','aria-label':t('language'),onchange:e=>setLocale(e.target.value)},[el('option',{value:'zh-CN',text:'中文',selected:locale==='zh-CN'}),el('option',{value:'en',text:'English',selected:locale==='en'})]);
  navItems.forEach((item) => {
    if (item.group) { navEl.appendChild(el('div', { class: 'group', text: item.group })); return; }
    navEl.appendChild(el('a', { href: `#${item.path}`, dataset: { path: item.path } }, [
      el('span', { class: 'ico', text: item.icon }),
      el('span', { text: item.label }),
    ]));
  });

  const user = store.getUser() || {};
  const roleLabel = user.role === 'operator' ? '操作员' : '管理员';
  const userEl = el('span', { class: 'user-chip', title: roleLabel }, [
    el('span', { class: 'avatar', text: (user.username || 'A').slice(0, 1).toUpperCase() }),
    el('span', { text: `${user.username || '管理员'} · ${roleLabel}` }),
  ]);
  const titleEl = el('div', { class: 'title', text: '' });

  const topbar = el('header', { class: 'topbar' }, [
    titleEl, mobileNav, language,
    el('span', { class: 'spacer' }),
    el('a', { class: 'btn sm', href: '/sim/', target: '_blank', rel: 'noopener', text:t('sim') }),
    userEl,
    el('button', { class: 'btn sm ghost', text:t('logout'), onclick: doLogout }),
  ]);

  const viewEl = el('main', { class: 'main',id:'main-content',tabindex:-1 });
  const sidebar = el('aside', { class: 'sidebar' }, [
    el('div', { class: 'brand' }, [el('span', { class: 'logo', text: '🅿️' }), el('span', { text: '停车场管理端' })]),
    navEl,
    el('div', { class: 'foot', text: 'v1.0.0' }),
  ]);

  appRoot.appendChild(el('a',{class:'skip-link',href:'#main-content',text:t('skip'),onclick:e=>{e.preventDefault();viewEl.focus();}}));
  appRoot.appendChild(el('div', { class: 'shell' }, [sidebar, topbar, viewEl]));
  shell = { navEl, titleEl, userEl, viewEl, mobileNav };
}

function setActiveNav(path) {
  if (!shell) return;
  shell.navEl.querySelectorAll('a').forEach((a) => {
    a.classList.toggle('active', a.dataset.path === path);
    if(a.dataset.path===path)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');
  });
}

async function doLogout() {
  try { await Api.logout(); } catch (_) {}
  runCleanup();
  store.clear();
  toast('已退出登录', 'ok');
  router.navigate('/login', { replace: true });
}

// 启动：若已登录先校验令牌有效性（me），失败则清理
async function boot() {
  document.documentElement.lang = locale === 'en' ? 'en' : 'zh-CN';
  if (locale === 'en') startI18nObserver(document.body);
  if (isAuthenticated()) {
    try { await Api.me(); }
    catch (_) { store.clear(); }
  }
  try {
    if(!isAuthenticated()){router.start();return;}
    const overview = await Api.overview();
    syncBusinessClock(overview.server_time, overview.timezone);
  } catch (_) { /* Other views retain the default business timezone. */ }
  router.start();
}

boot();
