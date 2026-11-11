// 名单管理视图：白名单（免费/授权）/黑名单（拒绝）/月租车。
// 支持新增（upsert 语义，同车牌覆盖）、按类型筛选、删除。进出场时 access 会据此裁决。
import { el, clear, toast, modal, confirmBox, fmtTime, listType, loadErrorCard } from '../util.js';
import { Api } from '../api.js';

const TYPES = [['', '全部'], ['white', '白名单'], ['black', '黑名单'], ['monthly', '月租']];
const TYPE_BADGE = { white: 'ok', black: 'danger', monthly: 'info' };

export default {
  title: '名单管理',
  icon: '📋',
  render(root, ctx) {
    const typeSel = el('select', {'aria-label':'名单类型', style: { width: '140px' } },
      TYPES.map(([v, t]) => el('option', { value: v, text: t })));
    const tbody = el('tbody', {});

    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '名单管理' }),
      el('span', { class: 'sub', text: '白名单 / 黑名单 / 月租车 · 影响进出场裁决' }),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn primary', text: '＋ 新增名单', onclick: () => openForm() }),
    ]));

    root.appendChild(el('div', { class: 'card' }, [
      el('h3', {}, ['名单', el('span', { class: 'spacer' }),
        el('span', { class: 'row', style: { gap: '8px' } }, [
          typeSel, el('button', { class: 'btn sm', text: '刷新', onclick: load }),
        ])]),
      el('div', { class: 'table-wrap' }, [
        el('table', { class: 'list-table' }, [
          el('thead', {}, [el('tr', {}, [
            el('th', { text: '车牌' }), el('th', { text: '类型' }),
            el('th', { text: '有效期至(本地)' }), el('th', { text: '状态' }),
            el('th', { text: '备注' }), el('th', { text: '操作' }),
          ])]),
          tbody,
        ]),
      ]),
    ]));

    typeSel.onchange = load;

    async function load() {
      clear(tbody);
      tbody.appendChild(el('tr', {}, [el('td', { colspan: '6' }, [el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…'])])]));
      try {
        const r = await Api.lists(typeSel.value || null);
        const items = r.items || [];
        clear(tbody);
        if (!items.length) { tbody.appendChild(el('tr', {}, [el('td', { colspan: '6' }, [el('div', { class: 'empty', text: '名单为空' })])])); return; }
        items.forEach((it) => {
          const expired = !!it.valid_until && Date.parse(it.valid_until) <= Date.now();
          tbody.appendChild(el('tr', { class: 'list-item' }, [
          el('td', {}, [el('span', { class: 'tag-plate blue', text: it.plate })]),
          el('td', {}, [el('span', { class: `badge ${TYPE_BADGE[it.list_type] || 'muted'}`, text: listType(it.list_type) })]),
          el('td', { class: 'small', text: it.valid_until ? fmtTime(it.valid_until, { utcTag: true }) : '长期' }),
          el('td', {}, [el('span', { class: `badge ${expired ? 'muted' : 'ok'}`,
            text: expired ? '已过期' : '生效中' })]),
          el('td', { class: 'small', text: it.note || '—' }),
          el('td', {}, [el('div', { class: 'btn-row', style: { gap: '6px' } }, [
            el('button', { class: 'btn sm', text: '编辑', onclick: () => openForm(it) }),
            el('button', { class: 'btn sm danger', text: '删除', onclick: () => remove(it) }),
          ])]),
          ]));
        });
      } catch (e) {
        clear(tbody);
        tbody.appendChild(el('tr', {}, [el('td', { colspan: '6' }, [
          loadErrorCard(e.message || '加载失败', () => load(), { title: '名单加载失败' })])]));
      }
    }

    function openForm(item) {
      const plateIn = el('input', { placeholder: '如 京A12345', value: item?.plate || '' });
      if (item) plateIn.readOnly = true;   // 车牌为主键，编辑时不可改（改为删除+新增）
      const typeIn = el('select', {}, [
        el('option', { value: 'white', text: '白名单（授权/免费）' }),
        el('option', { value: 'black', text: '黑名单（拒绝入场）' }),
        el('option', { value: 'monthly', text: '月租车' }),
      ]);
      typeIn.value = item?.list_type || 'white';
      const untilIn = el('input', { type: 'datetime-local' });
      if (item?.valid_until) untilIn.value = String(item.valid_until).slice(0, 16);
      const noteIn = el('input', { placeholder: '备注（可选）', value: item?.note || '' });

      modal({
        title: item ? `编辑名单 · ${item.plate}` : '新增名单',
        okText: '保存',
        body: el('div', {}, [
          el('div', { class: 'field' }, [el('label', { text: '车牌' }), plateIn,
            item ? el('div', { class: 'hint', text: '车牌为唯一键，如需更换请删除后新增。' }) : null]),
          el('div', { class: 'field' }, [el('label', { text: '类型' }), typeIn]),
          el('div', { class: 'field' }, [el('label', { text: '有效期至（停车场本地时间，留空为长期）' }), untilIn]),
          el('div', { class: 'field' }, [el('label', { text: '备注' }), noteIn]),
        ]),
        onOk: async () => {
          const plate = plateIn.value.trim().toUpperCase();
          if (!plate) { toast('请输入车牌', 'warn'); return false; }
          const payload = {
            plate, list_type: typeIn.value,
            valid_until: untilIn.value ? `${untilIn.value}:00` : null,
            note: noteIn.value.trim() || null,
          };
          try {
            await Api.upsertList(payload);
            toast(`已保存 ${plate}`, 'ok');
            load();
          } catch (e) { toast(`保存失败：${e.message || e}`, 'danger'); return false; }
        },
      });
    }

    async function remove(item) {
      const ok = await confirmBox(`确认从名单删除车牌 ${item.plate}？`, { title: '删除名单', danger: true, okText: '删除' });
      if (!ok) return;
      try { await Api.removeList(item.plate); toast(`已删除 ${item.plate}`, 'ok'); load(); }
      catch (e) { toast(`删除失败：${e.message || e}`, 'danger'); }
    }

    load();
    return () => {};
  },
};
