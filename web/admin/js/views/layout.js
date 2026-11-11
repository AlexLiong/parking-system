// 车位布局视图（DB 为布局唯一事实源，§3.9）：
// - 分区汇总（总数/余位/占用/类型）；
// - 2D 俯视图（按 space 坐标 x/z 绘制，颜色区分状态/类型，标注闸口）；
// - 声明式调整（按区填数量/类型 → POST /api/admin/layout，返回 diff，随后广播热更新）；
// - 布局 JSON 导出 / 导入（备份）。
import { el, clear, toast, modal, confirmBox, loadErrorCard } from '../util.js';
import { Api } from '../api.js';

const KIND_LABEL = { normal: '普通', ne_prefer: '新能源优先' };

export default {
  title: '车位布局',
  icon: '🗺️',
  async render(root, ctx) {
    let layout = null;
    const canvas = el('canvas', { id: 'lotmap' });
    const summaryBox = el('div', { class: 'grid cols-4' });
    const formBox = el('div', {});
    const tableBox = el('div', {});
    const loadErrorHost = el('div', {});

    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '车位布局' }),
      el('span', { class: 'sub', id: 'layout-ver', text: '版本 —' }),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn', text: '⬇ 导出', onclick: doExport }),
      el('button', { class: 'btn', text: '⬆ 导入', onclick: doImport }),
    ]));

    root.appendChild(loadErrorHost);
    root.appendChild(el('div', { class: 'card' }, [el('h3', { text: '分区汇总' }), summaryBox]));
    root.appendChild(el('div', { class: 'card' }, [
      el('h3', {}, ['俯视示意图', el('span', { class: 'spacer' }), mapLegend()]),
      el('div', { class: 'lotmap-wrap' }, [canvas]),
    ]));
    root.appendChild(el('div', { class: 'card' }, [el('h3', { text: '声明式调整布局' }), formBox]));
    root.appendChild(el('div', { class: 'card' }, [el('h3', { text: '车位明细' }), tableBox]));

    async function load() {
      try {
        layout = await Api.layout();
        clear(loadErrorHost);
        document.getElementById('layout-ver').textContent =
          `版本 v${layout.version} · 共 ${layout.spaces.length} 位 · 余 ${layout.free_total}`;
        renderSummary(summaryBox, layout);
        renderForm(formBox, layout, apply);
        renderTable(tableBox, layout);
        drawMap(canvas, layout);
      } catch (e) {
        clear(loadErrorHost);
        loadErrorHost.appendChild(loadErrorCard(e.message || '布局加载失败', () => load(),
          { title: '布局加载失败' }));
      }
    }

    async function apply(zones) {
      const ok = await confirmBox(
        `将按声明重新生成布局（${zones.map((z) => `${z.zone}:${z.count}${z.kind === 'ne_prefer' ? '(新能源)' : ''}`).join(' ')}）。在场车辆占用的车位会保留，确认？`,
        { title: '应用布局变更' });
      if (!ok) return;
      try {
        const diff = await Api.applyLayout(zones);
        toast(`布局已更新至 v${diff.version}：${diff.summary || ''}`, 'ok', 5000);
        await load();
      } catch (e) {
        toast(`应用失败：${e.message || e}`, 'danger', 5000);
      }
    }

    async function doExport() {
      try {
        const data = await Api.exportLayout();
        downloadJson(`layout_v${data.version}.json`, data);
        toast('已导出布局声明', 'ok');
      } catch (e) { toast(`导出失败：${e.message || e}`, 'danger'); }
    }

    function doImport() {
      const ta = el('textarea', { placeholder: '粘贴布局 JSON（{zones:[{zone,count,kind}]}）', style: { minHeight: '180px', fontFamily: 'var(--mono)', fontSize: '12px' } });
      modal({
        title: '导入布局声明', wide: true, okText: '导入并应用',
        body: el('div', {}, [
          el('div', { class: 'small muted mb', text: '导入 = 按声明重新生成布局（与"应用"等效），会广播热更新到 3D 端。' }),
          ta,
        ]),
        onOk: async () => {
          let payload;
          try { payload = JSON.parse(ta.value); }
          catch (_) { toast('JSON 解析失败', 'warn'); return false; }
          const zones = payload.zones || payload;
          if (!Array.isArray(zones) || !zones.length) { toast('缺少 zones 数组', 'warn'); return false; }
          try {
            const diff = await Api.importLayout({ zones });
            toast(`导入成功，布局更新至 v${diff.version}`, 'ok');
            await load();
          } catch (e) { toast(`导入失败：${e.message || e}`, 'danger'); return false; }
        },
      });
    }

    await load();
    const onResize = () => { if (layout) drawMap(canvas, layout); };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  },
};

function mapLegend() {
  const sw = (c) => el('span', { class: 'sw', style: { background: c } });
  return el('div', { class: 'row small muted', style: { gap: '12px' } }, [
    el('span', { class: 'li row', style: { gap: '5px' } }, [sw('#cfe0ff'), '空闲']),
    el('span', { class: 'li row', style: { gap: '5px' } }, [sw('#f0a6a6'), '占用']),
    el('span', { class: 'li row', style: { gap: '5px' } }, [sw('#b8f0d0'), '新能源优先']),
  ]);
}

function renderSummary(box, layout) {
  clear(box);
  const zones = layout.zones || {};
  const free = layout.free || {};
  const kindByZone = {};
  layout.spaces.forEach((s) => { kindByZone[s.zone] = s.kind; });
  Object.keys(zones).sort().forEach((z) => {
    const total = zones[z] || 0, fr = free[z] ?? 0, occ = total - fr;
    const pct = total ? Math.round((occ / total) * 100) : 0;
    box.appendChild(el('div', { class: 'stat' }, [
      el('div', { class: 'label', text: `${z} 区 · ${KIND_LABEL[kindByZone[z]] || '普通'}` }),
      el('div', { class: 'value', html: `${fr}<small> / ${total} 空闲</small>` }),
      el('div', { class: 'foot', text: `占用 ${occ}（${pct}%）` }),
    ]));
  });
  if (!Object.keys(zones).length) box.appendChild(el('div', { class: 'empty', text: '暂无分区' }));
}

function renderForm(box, layout, apply) {
  clear(box);
  const zones = Object.keys(layout.zones || {}).sort();
  const kindByZone = {};
  layout.spaces.forEach((s) => { kindByZone[s.zone] = s.kind; });
  const rows = [];
  const zoneList = zones.length ? zones : ['A', 'B', 'C', 'D'];
  zoneList.forEach((z) => {
    const countIn = el('input', { type: 'number', min: '1', max: '20', value: String(layout.zones?.[z] ?? 10), style: { width: '90px' } });
    const kindSel = el('select', { style: { width: '150px' } }, [
      el('option', { value: 'normal', text: '普通' }),
      el('option', { value: 'ne_prefer', text: '新能源优先' }),
    ]);
    kindSel.value = kindByZone[z] || 'normal';
    rows.push({ zone: z, countIn, kindSel });
    box.appendChild(el('div', { class: 'row', style: { gap: '10px', padding: '6px 0' } }, [
      el('span', { class: 'badge brand', style: { width: '46px', justifyContent: 'center' }, text: `${z} 区` }),
      el('span', { class: 'small muted', text: '车位数' }), countIn,
      el('span', { class: 'small muted', text: '类型' }), kindSel,
    ]));
  });
  box.appendChild(el('div', { class: 'btn-row mt' }, [
    el('button', { class: 'btn primary', text: '应用变更', onclick: () => {
      const decls = rows.map((r) => ({ zone: r.zone, count: Math.min(20, Math.max(1, Math.round(Number(r.countIn.value) || 1))), kind: r.kindSel.value }));
      apply(decls);
    } }),
    el('span', { class: 'small muted', text: '已有车位的数量与类型可调整；各区最多20位，固定场地的坐标与道具由原布局决定。' }),
  ]));
}

function renderTable(box, layout) {
  clear(box);
  const items = layout.spaces || [];
  const rows = items.map((s) => el('tr', {}, [
    el('td', { class: 'mono small', text: s.space_no }),
    el('td', {}, [el('span', { class: 'badge muted', text: s.zone })]),
    el('td', { class: 'small', text: KIND_LABEL[s.kind] || s.kind }),
    el('td', { class: 'num small mono', text: s.x.toFixed(2) }),
    el('td', { class: 'num small mono', text: s.z.toFixed(2) }),
    el('td', { class: 'num small mono', text: `${Math.round(s.rotation)}°` }),
    el('td', {}, [s.status === 'occupied'
      ? el('span', { class: 'badge warn', text: '占用' })
      : el('span', { class: 'badge ok', text: '空闲' })]),
    el('td', { class: 'small', text: s.current_plate || '—' }),
  ]));
  box.appendChild(el('div', { class: 'table-wrap', style: { maxHeight: '360px', overflowY: 'auto' } }, [
    el('table', {}, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '车位号' }), el('th', { text: '区' }), el('th', { text: '类型' }),
        el('th', { class: 'num', text: 'x' }), el('th', { class: 'num', text: 'z' }),
        el('th', { class: 'num', text: '朝向' }), el('th', { text: '状态' }), el('th', { text: '当前车牌' }),
      ])]),
      el('tbody', {}, rows),
    ]),
  ]));
}

// ----------------------------- 2D 俯视图 -----------------------------
function drawMap(canvas, layout) {
  const spaces = layout.spaces || [];
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.parentElement?.clientWidth ? Math.min(760, canvas.parentElement.clientWidth - 8) : 720;
  const H = 420;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!spaces.length) {
    ctx.fillStyle = '#8a94a3'; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('暂无车位数据', W / 2, H / 2); return;
  }
  // 世界坐标范围（x 东西，z 南北）；含闸口
  const gates = layout.gates || {};
  const pts = spaces.map((s) => [s.x, s.z]);
  Object.values(gates).forEach((g) => { if (g && typeof g.x === 'number') pts.push([g.x, g.y ?? g.z ?? 0]); });
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
  pts.forEach(([x, z]) => { minX = Math.min(minX, x); maxX = Math.max(maxX, x); minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z); });
  const pad = 3; minX -= pad; maxX += pad; minZ -= pad; maxZ += pad;
  const scale = Math.min((W - 20) / (maxX - minX), (H - 20) / (maxZ - minZ));
  // 世界(x,z) → 画布(px,py)：x 向右，z 向上（北）
  const toPx = (x) => 10 + (x - minX) * scale;
  const toPy = (z) => H - 10 - (z - minZ) * scale;

  // 车位矩形（约 2.5m × 5m），按 rotation 旋转
  const sw = 2.4 * scale, sh = 4.8 * scale;
  spaces.forEach((s) => {
    const cx = toPx(s.x), cy = toPy(s.z);
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-(s.rotation || 0) * Math.PI / 180);
    ctx.beginPath();
    ctx.rect(-sw / 2, -sh / 2, sw, sh);
    ctx.fillStyle = s.status === 'occupied' ? '#f0a6a6'
      : (s.kind === 'ne_prefer' ? '#b8f0d0' : '#cfe0ff');
    ctx.fill();
    ctx.strokeStyle = 'rgba(30,50,90,.35)'; ctx.lineWidth = 1; ctx.stroke();
    ctx.restore();
  });

  // 闸口标注
  Object.entries(gates).forEach(([id, g]) => {
    if (!g || typeof g.x !== 'number') return;
    const gx = toPx(g.x), gy = toPy(g.y ?? g.z ?? 0);
    ctx.fillStyle = '#2f6df6';
    ctx.beginPath(); ctx.arc(gx, gy, 5, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#1f2733'; ctx.font = 'bold 11px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(id === 'in' ? '入口' : id === 'out' ? '出口' : id, gx, gy - 9);
  });
  ctx.textAlign = 'left';
}

function downloadJson(name, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
