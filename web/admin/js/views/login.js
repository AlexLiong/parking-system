// 登录视图：账号/密码 → POST /api/auth/login，成功后存令牌并跳转监控页。
import { el, store, toast } from '../util.js';
import {t} from '../i18n.js';
import { Api } from '../api.js';

export default {
  title: '登录',
  public: true,
  render(root, ctx) {
    const user = el('input', { placeholder: t('username'), autocomplete: 'username' });
    const pass = el('input', { placeholder: t('password'), type: 'password', autocomplete: 'current-password' });
    const err = el('div', { class: 'login-err',role:'alert' });
    const btn = el('button', { class: 'btn primary', text:t('login') });

    async function submit() {
      if(btn.disabled)return;
      const username = user.value.trim();
      const password = pass.value;
      if (!username || !password) { err.textContent = '请输入用户名和密码'; return; }
      btn.disabled = true; btn.textContent = t('logging'); err.textContent = '';
      try {
        const r = await Api.login(username, password);
        store.setToken(r.token);
        store.setUser({ username: r.username, role: r.role });
        toast(`欢迎回来，${r.username}`, 'ok');
        ctx.navigate('/monitor', { replace: true });
      } catch (e) {
        err.textContent = e.message || '登录失败';
        btn.disabled = false; btn.textContent = t('login');
      }
    }

    btn.addEventListener('click', submit);
    [user, pass].forEach((inp) => inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submit();
    }));

    root.appendChild(el('div', { class: 'login-wrap' }, [
      el('div', { class: 'login-card' }, [
        el('div', { class: 'logo', text: '🅿️' }),
        el('h1', { text: '停车场收费系统' }),
        el('div', { class: 'sub', text: '管理控制台 · Admin Console' }),
        el('div', { class: 'field' }, [el('label', { text:t('username') }), user]),
        el('div', { class: 'field' }, [el('label', { text:t('password') }), pass]),
        btn,
        err,
        el('div', { class: 'login-tip', text: '请使用管理员或操作员账号登录' }),
      ]),
    ]));
  },
};
