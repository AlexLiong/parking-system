// 演示剧本编辑器：管理端只编辑 GET/PUT /api/admin/scenario 的数据；
// 保存/重置不直接控制 3D 播放（3D 端播放时自行锁播放）。
import { el, clear, toast, confirmBox, loadErrorCard } from '../util.js';
import { Api } from '../api.js';
import {
  PLATE_TYPES, TYPE_VALUES, emptyVehicle, normalizeScenario, validateScenario,
  sortVehicles, expectedTimes,
} from '../scenario-model.js';

export default {
  title: '演示剧本',
  icon: '🎬',
  async render(root) {
    const state = { data: normalizeScenario(null), busy: false };
    let tbody = null;
    const loadHost = el('div', {});
    const metaBox = el('div', { class: 'grid cols-3 scenario-meta' });
    const statsBox = el('div', { class: 'grid cols-4' });
    const errorBox = el('div', { class: 'scenario-error', role: 'alert', 'aria-live': 'assertive', hidden: true });
    const tableBox = el('div', { class: 'table-wrap scenario-table-wrap' });

    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '演示剧本' }),
      el('span', { class: 'sub', text: '编辑 3D 演示车辆编排 · 保存/重置不影响正在播放的 3D（播放时由 3D 端自行锁播放）' }),
    ]));

    const nameIn = el('input', { id: 'scenario-name', maxlength: '80', autocomplete: 'off' });
    const seedIn = el('input', { id: 'scenario-seed', type: 'number', min: '0', step: '1' });
    const descIn = el('textarea', { id: 'scenario-description', rows: '2', maxlength: '500' });
    nameIn.addEventListener('input', () => { state.data.name = nameIn.value; });
    seedIn.addEventListener('input', () => { state.data.seed = Number(seedIn.value); });
    descIn.addEventListener('input', () => { state.data.description = descIn.value; });

    metaBox.append(
      el('div', { class: 'field' }, [el('label', { for: 'scenario-name', text: '剧本名称' }), nameIn]),
      el('div', { class: 'field' }, [el('label', { for: 'scenario-seed', text: '随机种子 seed' }), seedIn]),
      el('div', { class: 'field' }, [el('label', { for: 'scenario-description', text: '说明' }), descIn]),
    );

    const addBtn = el('button', { id: 'scenario-add', class: 'btn primary', text: '＋ 添加车辆', onclick: addRow });
    const saveBtn = el('button', { id: 'scenario-save', class: 'btn primary', text: '保存剧本', onclick: save });
    const resetBtn = el('button', { id: 'scenario-reset', class: 'btn ghost', text: '重置默认', onclick: reset });
    const headerActions = el('div', { class: 'btn-row' }, [addBtn, saveBtn, resetBtn]);

    tbody = el('tbody', {});
    tableBox.appendChild(el('table', { id: 'scenario-table', class: 'scenario-table' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { class: 'num', text: '#' }),
        el('th', { text: 'vid' }),
        el('th', { class: 'num', text: '入场时间 t(s)' }),
        el('th', { text: '车牌' }),
        el('th', { text: '类型' }),
        el('th', { class: 'num', text: '停车时长(s)' }),
        el('th', { class: 'num', text: '预计入场' }),
        el('th', { class: 'num', text: '预计出场' }),
        el('th', { class: 'num', text: '预计缴费' }),
        el('th', { text: '操作' }),
      ])]),
      tbody,
    ]));

    root.appendChild(el('div', { class: 'card scenario-note', role: 'note' }, [
      el('b', { text: '编辑说明：' }),
      el('span', { text: '表格按 vid 唯一；相同车牌的时间窗不能重叠；plate_type 支持 blue/green/green_large/yellow；保存前会校验车牌、重复 vid、时间和时长。仅保存剧本 JSON，不启动/暂停 3D。' }),
    ]));
    root.appendChild(loadHost);
    root.appendChild(el('div', { class: 'card' }, [
      el('h3', {}, ['剧本元信息', el('span', { class: 'spacer' }), headerActions]),
      metaBox,
    ]));
    root.appendChild(el('div', { class: 'card' }, [
      el('h3', {}, ['车辆编排', el('span', { class: 'spacer' }), el('span', { class: 'small muted', text: '上移/下移调整播放顺序' })]),
      errorBox,
      tableBox,
      el('div', { class: 'small muted mt', text: '列表按当前顺序下发；3D 播放时由 3D 端自行锁定正在播放的剧本。' }),
    ]));
    root.appendChild(statsBox);
    root.appendChild(el('div', { class: 'muted small', id: 'scenario-status', role: 'status', 'aria-live': 'polite' }, '尚未保存'));

    await load();

    async function load() {
      clear(loadHost);
      loadHost.appendChild(el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载剧本中…']));
      try {
        const raw = await Api.scenario();
        state.data = normalizeScenario(raw);
        loadHost.replaceChildren();
        applyData();
      } catch (e) {
        clear(loadHost);
        loadHost.appendChild(loadErrorCard(e.message || '剧本加载失败', () => load(),
          { title: '剧本加载失败' }));
      }
    }

    function applyData() {
      nameIn.value = state.data.name;
      seedIn.value = String(state.data.seed);
      descIn.value = state.data.description;
      hideError();
      renderRows();
      renderStats();
      setStatus('已加载当前剧本');
    }

    function addRow() {
      const v = emptyVehicle(state.data.vehicles.length);
      state.data.vehicles.push(v);
      renderRows();
      renderStats();
      const row = tbody.querySelector(`[data-field="vid-${state.data.vehicles.length - 1}"]`);
      row?.focus();
      row?.select?.();
    }

    function renderRows() {
      clear(tbody);
      state.data.vehicles.forEach((v, i) => {
        const vidIn = el('input', {
          value: v.vid, autocomplete: 'off', dataset: { field: `vid-${i}` },
          'aria-label': `第 ${i + 1} 行 vid`,
        });
        const tIn = el('input', {
          type: 'number', min: '0', step: '0.1', value: String(v.t), dataset: { field: `t-${i}` },
          'aria-label': `第 ${i + 1} 行入场时间 t`,
        });
        const plateIn = el('input', {
          value: v.plate, autocomplete: 'off', maxlength: '8', dataset: { field: `plate-${i}` },
          'aria-label': `第 ${i + 1} 行车牌`,
        });
        const typeSel = el('select', { dataset: { field: `type-${i}` }, 'aria-label': `第 ${i + 1} 行车牌类型` },
          PLATE_TYPES.map((x) => el('option', { value: x.value, text: x.label })));
        typeSel.value = v.plate_type;
        const durationIn = el('input', {
          type: 'number', min: '0.1', step: '0.1', value: String(v.duration),
          dataset: { field: `duration-${i}` }, 'aria-label': `第 ${i + 1} 行停车时长（秒）`,
        });
        const durationHint = el('div', { class: 'scenario-mini', text: `≈ ${(Number(v.duration || 0) / 60).toFixed(1)} min` });
        const entryCell = el('td', { class: 'num small mono' });
        const exitCell = el('td', { class: 'num small mono' });
        const payCell = el('td', { class: 'num small mono' });
        const refreshTimes = () => {
          const stamps = expectedTimes(v);
          entryCell.textContent = `${stamps.entry.toFixed(1)}s`;
          exitCell.textContent = `${stamps.exit.toFixed(1)}s`;
          payCell.textContent = `${stamps.pay.toFixed(1)}s`;
        };
        refreshTimes();
        vidIn.addEventListener('input', () => { v.vid = vidIn.value; });
        tIn.addEventListener('input', () => { v.t = Number(tIn.value); refreshTimes(); renderStats(); });
        plateIn.addEventListener('input', () => { v.plate = plateIn.value; });
        typeSel.addEventListener('change', () => { v.plate_type = typeSel.value; renderStats(); });
        durationIn.addEventListener('input', () => {
          v.duration = Number(durationIn.value);
          durationHint.textContent = `≈ ${(Number(v.duration || 0) / 60).toFixed(1)} min`;
          refreshTimes();
          renderStats();
        });

        const actions = el('div', { class: 'btn-row', style: { gap: '4px' } }, [
          el('button', {
            class: 'btn sm ghost', text: '↑', 'aria-label': `上移第 ${i + 1} 行`, disabled: i === 0,
            onclick: () => moveRow(i, -1),
          }),
          el('button', {
            class: 'btn sm ghost', text: '↓', 'aria-label': `下移第 ${i + 1} 行`, disabled: i === state.data.vehicles.length - 1,
            onclick: () => moveRow(i, 1),
          }),
          el('button', {
            class: 'btn sm danger', text: '删除', 'aria-label': `删除第 ${i + 1} 行`,
            onclick: () => {
              state.data.vehicles.splice(i, 1);
              renderRows(); renderStats(); setStatus('');
            },
          }),
        ]);
        tbody.appendChild(el('tr', { dataset: { row: String(i) } }, [
          el('td', { class: 'num small muted', text: String(i + 1) }),
          el('td', {}, [vidIn]),
          el('td', {}, [tIn]),
          el('td', {}, [plateIn]),
          el('td', {}, [typeSel]),
          el('td', {}, [durationIn, durationHint]),
          entryCell,
          exitCell,
          payCell,
          el('td', {}, [actions]),
        ]));
      });
    }

    function moveRow(index, delta) {
      const next = index + delta;
      if (next < 0 || next >= state.data.vehicles.length) return;
      const arr = state.data.vehicles;
      [arr[index], arr[next]] = [arr[next], arr[index]];
      renderRows();
      tbody.querySelector(`[data-field="vid-${next}"]`)?.focus();
    }

    function renderStats() {
      clear(statsBox);
      const vs = state.data.vehicles;
      const timelineEnd = vs.reduce((max, v) => Math.max(max, Number(v.t || 0) + Number(v.duration || 0)), 0);
      const stopTotal = vs.reduce((sum, v) => sum + Number(v.duration || 0), 0);
      const counts = PLATE_TYPES.map((x) => `${x.label} ${vs.filter((v) => v.plate_type === x.value).length}`).join(' · ');
      const card = (label, value, foot = '') => el('div', { class: 'stat' }, [
        el('div', { class: 'label', text: label }),
        el('div', { class: 'value', text: String(value) }),
        foot ? el('div', { class: 'foot', text: foot }) : null,
      ]);
      statsBox.append(
        card('车辆数', vs.length, counts),
        card('时间轴总长', `${timelineEnd.toFixed(1)} s`),
        card('停车时长合计', `${stopTotal.toFixed(1)} s`),
        card('播放顺序', vs.map((v) => v.vid).join(' → ') || '—', '当前列表顺序'),
      );
    }

    function showError(message, field) {
      errorBox.hidden = false;
      errorBox.textContent = message;
      if (field) {
        tbody.querySelector(`[data-field="${CSS.escape(field)}"]`)?.focus();
      }
    }
    function hideError() { errorBox.hidden = true; errorBox.textContent = ''; }
    function setStatus(text) {
      const node = root.querySelector('#scenario-status');
      if (node) node.textContent = text;
    }

    async function save() {
      if (state.busy) return;
      const problem = validateScenario(state.data);
      if (problem) { showError(`保存失败：${problem.message}`, problem.field); setStatus('保存失败：请修正校验错误'); return; }
      state.busy = true;
      saveBtn.disabled = true;
      resetBtn.disabled = true;
      setStatus('保存中…');
      try {
        state.data.vehicles = sortVehicles(state.data.vehicles);
        const payload = {
          name: state.data.name.trim(),
          description: state.data.description,
          seed: Number(state.data.seed),
          vehicles: state.data.vehicles.map((v) => ({
            vid: v.vid.trim(), t: Number(v.t), plate: v.plate.trim().toUpperCase(),
            plate_type: v.plate_type, duration: Number(v.duration),
          })),
        };
        const raw = await Api.saveScenario(payload);
        state.data = normalizeScenario(raw?.vehicles ? raw : { ...payload, ...(raw || {}) });
        applyData();
        setStatus('保存成功');
        toast('剧本已保存', 'ok');
      } catch (e) {
        showError(`保存失败：${e.message || e}`);
        setStatus(`保存失败：${e.message || e}`);
        toast(`保存失败：${e.message || e}`, 'danger', 5000);
      } finally {
        state.busy = false;
        saveBtn.disabled = false;
        resetBtn.disabled = false;
      }
    }

    async function reset() {
      const ok = await confirmBox('重置为默认演示剧本？当前未保存的编辑会丢失。', { title: '重置演示剧本', danger: true, okText: '重置' });
      if (!ok) return;
      state.busy = true;
      try {
        const raw = await Api.resetScenario();
        state.data = normalizeScenario(raw?.vehicles ? raw : { ...state.data, ...(raw || {}), vehicles: Array.isArray(raw?.vehicles) ? raw.vehicles : undefined });
        if (!Array.isArray(raw?.vehicles) && !Array.isArray(raw?.scenario?.vehicles)) await load();
        else applyData();
        setStatus('已重置默认剧本');
        toast('已重置默认剧本', 'ok');
      } catch (e) {
        showError(`重置失败：${e.message || e}`);
        toast(`重置失败：${e.message || e}`, 'danger', 5000);
      } finally {
        state.busy = false;
      }
    }

    return () => {};
  },
};
