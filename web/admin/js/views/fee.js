// 费率管理视图：主页面仅当前生效费率与编辑；历史费率在只读弹窗中查看。
import { el, clear, toast, modal, fmtTime, fmtMoney, loadErrorCard } from '../util.js';
import { Api } from '../api.js';

export default {
  title: '费率管理',
  icon: '💰',
  async render(root) {
    const currentBox = el('div', {});
    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '费率管理' }),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn', text: '查看历史费率', onclick: openHistory }),
    ]));
    root.appendChild(currentBox);

    async function load() {
      clear(currentBox);
      try {
        const cur = await Api.currentFeeRule();
        renderCurrent(currentBox, cur, () => openEdit(cur, load));
      } catch (e) {
        currentBox.appendChild(loadErrorCard(e.message || '加载失败', () => load(), { title: '费率加载失败' }));
      }
    }

    await load();
    return () => {};
  },
};

function openEdit(rule, reload) {
  if (!rule) return;
  const f = {
    free_minutes: numInput(rule.free_minutes ?? 15, '0', '1'),
    first_hour_price: numInput(rule.first_hour_price ?? 5, '0', '0.1'),
    unit_minutes: numInput(rule.unit_minutes ?? 30, '1', '1'),
    unit_price: numInput(rule.unit_price ?? 2, '0', '0.1'),
    daily_cap: numInput(rule.daily_cap ?? 40, '0', '0.5'),
    ne_discount: numInput(rule.ne_discount ?? 0.9, '0', '0.01'),
  };
  const roundIn = el('input', { type: 'checkbox' });
  roundIn.checked = rule.round_up !== false;
  const noteIn = el('textarea', { placeholder: '备注（可选）' });
  noteIn.value = rule.note || '';

  const body = el('div', {}, [
    el('div', { class: 'inline-fields' }, [
      fieldEl('免费时长（分钟）', f.free_minutes),
      fieldEl('首小时价格（¥）', f.first_hour_price),
      fieldEl('计费单位（分钟）', f.unit_minutes),
      fieldEl('单位价格（¥）', f.unit_price),
      fieldEl('每日封顶（¥）', f.daily_cap),
      fieldEl('新能源折扣（0~1）', f.ne_discount),
    ]),
    el('div', { class: 'checkline mb' }, [roundIn, el('label', { text: '不足一个计费单位按进一法计费（round_up）' })]),
    el('div', { class: 'field' }, [el('label', { text: '备注' }), noteIn]),
  ]);
  const close = modal({
    title: '编辑当前费率', wide: true, body, okText: '保存',
    onOk: async () => {
      const payload = {
        free_minutes: intOf(f.free_minutes), first_hour_price: numOf(f.first_hour_price),
        unit_minutes: intOf(f.unit_minutes), unit_price: numOf(f.unit_price),
        daily_cap: numOf(f.daily_cap), ne_discount: numOf(f.ne_discount),
        round_up: roundIn.checked, note: noteIn.value.trim(),
      };
      if (payload.ne_discount <= 0 || payload.ne_discount > 1) { toast('新能源折扣应在 (0,1]', 'warn'); return false; }
      if (payload.unit_minutes < 1) { toast('计费单位需 ≥ 1 分钟', 'warn'); return false; }
      try {
        await Api.updateCurrentFeeRule(payload);
        toast('当前费率已保存', 'ok');
        await reload();
      } catch (e) { toast(`保存失败：${e.message || e}`, 'danger'); return false; }
    },
  });
  void close;
}

async function openHistory() {
  const body = el('div', {}, [el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…'])]);
  modal({ title: '历史费率', wide: true, body, hideFooter: true });
  try {
    const all = await Api.feeRules();
    clear(body);
    const items = all?.items || [];
    if (!items.length) {
      body.appendChild(el('div', { class: 'empty', text: '暂无历史费率' }));
      return;
    }
    body.appendChild(el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: '版本' }), el('th', { text: '生效时间' }), el('th', { text: '停用时间' }),
          el('th', { class: 'num', text: '免费' }), el('th', { class: 'num', text: '首小时' }),
          el('th', { class: 'num', text: '续时' }), el('th', { class: 'num', text: '封顶' }),
          el('th', { class: 'num', text: '新能源' }), el('th', { text: '创建时间' }),
          el('th', { text: '操作员' }), el('th', { text: '备注' }),
        ])]),
        el('tbody', {}, items.map((r) => el('tr', {}, [
          el('td', { class: 'mono small', text: `#${r.id}` }),
          el('td', { class: 'small', text: fmtTime(r.effective_from, { utcTag: true }) }),
          el('td', { class: 'small', text: r.effective_to ? fmtTime(r.effective_to, { utcTag: true }) : '—' }),
          el('td', { class: 'num', text: `${r.free_minutes}′` }),
          el('td', { class: 'num', text: fmtMoney(r.first_hour_price) }),
          el('td', { class: 'num', text: `${fmtMoney(r.unit_price)}/${r.unit_minutes}′` }),
          el('td', { class: 'num', text: fmtMoney(r.daily_cap) }),
          el('td', { class: 'num', text: `${(Number(r.ne_discount) * 10).toFixed(1)}折` }),
          el('td', { class: 'small', text: r.created_at ? fmtTime(r.created_at, { utcTag: true }) : '—' }),
          el('td', { class: 'small', text: r.operator || '—' }),
          el('td', { class: 'small', text: r.note || '—' }),
        ]))),
      ]),
    ]));
  } catch (e) {
    clear(body);
    body.appendChild(loadErrorCard(e.message || '加载失败', () => openHistory(), { title: '历史费率加载失败' }));
  }
}

function renderCurrent(box, cur, onEdit) {
  clear(box);
  if (!cur) return;
  box.appendChild(el('div', { class: 'card accent' }, [
    el('h3', {}, ['当前生效费率', el('span', { class: 'spacer' }),
      el('span', { class: 'badge brand', text: `#${cur.id}` }),
      el('button', { class: 'btn sm', text: '编辑', onclick: onEdit })]),
    el('div', { class: 'grid cols-4' }, [
      mini('免费时长', `${cur.free_minutes} 分钟`),
      mini('首小时', fmtMoney(cur.first_hour_price)),
      mini('续时', `${fmtMoney(cur.unit_price)} / ${cur.unit_minutes} 分钟`),
      mini('每日封顶', fmtMoney(cur.daily_cap)),
      mini('新能源折扣', `${(Number(cur.ne_discount) * 10).toFixed(1)} 折`),
      mini('进一法', cur.round_up ? '是' : '否'),
      mini('生效时间', fmtTime(cur.effective_from, { utcTag: true })),
      mini('操作员', cur.operator || '—'),
    ]),
    cur.note ? el('div', { class: 'small muted mt', text: `备注：${cur.note}` }) : null,
  ]));
}

function numInput(v, min = '0', step = '1') {
  return el('input', { type: 'number', min, step, value: String(v) });
}
function fieldEl(label, node) { return el('div', { class: 'field' }, [el('label', { text: label }), node]); }
function intOf(node) { return Math.max(0, Math.round(Number(node.value) || 0)); }
function numOf(node) { return Math.max(0, Number(node.value) || 0); }
function mini(label, value) {
  return el('div', { class: 'stat' }, [
    el('div', { class: 'label', text: label }),
    el('div', { class: 'value', style: { fontSize: '18px' }, text: String(value) }),
  ]);
}
