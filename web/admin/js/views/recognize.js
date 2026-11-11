// 车牌图片识别视图（FR-1.3：识别输入与 3D 建模彻底解耦）。
//
// 重要边界：本页只是**识别演示**，只调用 /api/recognize 展示识别输入与结果；
// 不调用任何入场、出场、授权、放行、人工登记/人工确认接口，也不写停车记录、不改变道闸/车位。
// 返回字段与 3D 仿真端 plate_recognized / 识别输入使用同一套结构：
//   plate / plate_color(color) / overall_conf(conf) / candidates / elapsed_ms /
//   image_path / image_url / plate_crop_path / plate_crop_url /
//   image_source / image_synthetic / need_manual / message / source_status
// 后端字段尚未全部切换时，前端做兼容回退（mediaUrl / image_path / plate_crop_path）。
import {
  el, clear, toast, mediaUrl, fmtTime, plateClass, escapeHtml, loadErrorCard,
} from '../util.js';
import { Api } from '../api.js';

const MAX_BYTES = 5 * 1024 * 1024;

export default {
  title: '车牌识别',
  icon: '🔍',
  render(root, ctx) {
    root.appendChild(el('div', { class: 'view-head' }, [
      el('h2', { text: '车牌图片识别演示' }),
      el('span', { class: 'sub', text: '上传整车/车牌图片识别' }),
    ]));

    const tabTool = el('button', { class: 'active', text: '识别演示' });
    const tabLogs = el('button', { text: '识别日志' });
    const tabs = el('div', { class: 'tabs' }, [tabTool, tabLogs]);
    const paneTool = el('div', {});
    const paneLogs = el('div', { style: { display: 'none' } });
    root.appendChild(tabs);
    root.appendChild(paneTool);
    root.appendChild(paneLogs);

    let logsLoaded = false;
    tabTool.onclick = () => {
      tabTool.classList.add('active'); tabLogs.classList.remove('active');
      paneTool.style.display = ''; paneLogs.style.display = 'none';
    };
    tabLogs.onclick = async () => {
      tabLogs.classList.add('active'); tabTool.classList.remove('active');
      paneLogs.style.display = ''; paneTool.style.display = 'none';
      if (!logsLoaded) { await renderLogs(paneLogs); logsLoaded = true; }
    };

    buildTool(paneTool, ctx);
    return () => {};
  },
};

// ----------------------------- 识别演示 -----------------------------
function buildTool(root, ctx) {
  const gateSel = el('select', {}, [
    el('option', { value: 'in', text: '入口场景 (in)' }),
    el('option', { value: 'out', text: '出口场景 (out)' }),
  ]);
  const fileInput = el('input', {
    type: 'file', accept: 'image/jpeg,image/png', style: { display: 'none' },
    'aria-label': '选择车牌图片上传识别',
  });
  const previewBox = el('div', { class: 'recognize-upload-preview' });
  const resultBox = el('div', { 'aria-live': 'polite', 'aria-atomic': 'true' });
  const galleryBox = el('div', { class: 'gallery mt' });
  let busy = false;
  let localUrl = null;

  const dropzone = el('div', {
    class: 'dropzone', role: 'button', tabindex: 0, 'aria-label': '选择或拖拽车牌图片上传识别',
    html: '📷 点击选择 / 拖拽车牌图片到此处<br>'
      + '<span class="small">支持 JPG/PNG，≤ 5 MiB；建议使用正常角度整车图（如 EasyPR）</span>',
  });
  dropzone.onclick = () => fileInput.click();
  dropzone.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
  };
  ['dragover', 'dragenter'].forEach((ev) => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((ev) => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.remove('over');
  }));
  dropzone.addEventListener('drop', (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) pickFile(f);
  });
  fileInput.onchange = () => {
    const f = fileInput.files?.[0];
    if (f) pickFile(f);
    fileInput.value = '';
  };

  const left = el('div', { class: 'card' }, [
    el('h3', { text: '上传识别输入' }),
    el('div', { class: 'field' }, [
      el('label', { text: '识别场景' }), gateSel,
    ]),
    dropzone, fileInput, previewBox,
    el('h3', { class: 'mt', text: '演示图库' }),
    galleryBox,
  ]);

  const right = el('div', { class: 'card' }, [
    el('h3', {}, ['识别结果', el('span', { class: 'spacer' }),
      el('span', { class: 'badge muted', text: '识别演示' })]),
    resultBox,
  ]);
  resultBox.appendChild(resultEmpty('尚未识别。请上传图片，或点击左侧演示图库样例。'));

  root.appendChild(el('div', { class: 'grid cols-2' }, [left, right]));

  loadGallery();

  function pickFile(file) {
    if (!/^image\/(jpeg|png)$/i.test(file.type)) {
      toast('请选择 JPG/PNG 图片', 'warn'); return;
    }
    if (file.size > MAX_BYTES) {
      toast('图片不能超过 5 MiB', 'warn'); return;
    }
    if (localUrl) URL.revokeObjectURL(localUrl);
    localUrl = URL.createObjectURL(file);
    renderLocalPreview(file, localUrl);
    doRecognize({ file, localUrl });
  }

  function renderLocalPreview(file, url) {
    clear(previewBox);
    previewBox.appendChild(el('div', { class: 'recog-card recognize-preview-card' }, [
      el('div', { class: 'recog-head' }, [
        el('span', { class: 'badge brand recog-badge', text: '识别输入（待识别）' }),
        el('span', { class: 'small muted', text: file.name }),
      ]),
      el('img', { class: 'recog-img', src: url, alt: '待识别上传原图' }),
      el('div', { class: 'small muted', text: `本地预览 · ${(file.size / 1024).toFixed(1)} KiB` }),
    ]));
  }

  async function loadGallery() {
    try {
      const rows = (await Api.recognitionGallery()).samples || [];
      const clean = rows.filter((r) => r.condition === 'clean');
      const pick = [
        ...clean.filter((r) => r.color === 'blue').slice(0, 12),
        ...clean.filter((r) => r.color === 'green').slice(0, 12),
      ].slice(0, 24);
      clear(galleryBox);
      if (!pick.length) {
        galleryBox.appendChild(el('div', { class: 'small muted', text: '图库为空' }));
        return;
      }
      pick.forEach((r) => {
        galleryBox.appendChild(el('button', {
          type: 'button', class: 'g', title: `${r.plate || ''}`,
          'aria-label': `${r.plate || '样例'} 识别演示`,
          onclick: () => doRecognize({ imagePath: `datasets/demo-lpr/${r.file}` }),
        }, [
          el('img', {
            src: `/datasets/demo-lpr/${r.file}`, alt: r.plate || '演示样例',
            loading: 'lazy', onerror: (e) => { e.target.style.opacity = 0.3; },
          }),
          el('div', { class: 'cap', text: r.plate || r.file }),
        ]));
      });
    } catch (e) {
      clear(galleryBox);
      galleryBox.appendChild(loadErrorCard(e.message || '图库加载失败', () => loadGallery(),
        { title: '演示图库加载失败' }));
    }
  }

  let lastRequest = null;
  async function doRecognize(request = null) {
    if (request) lastRequest = request;
    if (busy) return;
    const { file = null, imagePath = null, localUrl: previewUrl = null } = lastRequest || {};
    busy = true;
    clear(resultBox);
    resultBox.appendChild(el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '识别中…']));
    try {
      const res = await Api.recognize({ file, imagePath, gate: gateSel.value });
      renderResult(resultBox, res, { localUrl: previewUrl });
    } catch (e) {
      clear(resultBox);
      resultBox.appendChild(loadErrorCard(e.message || '识别失败',
        () => doRecognize(lastRequest), { title: '识别请求失败' }));
    } finally {
      busy = false;
    }
  }
}

function resultEmpty(text) {
  return el('div', { class: 'empty', text });
}

// ----------------------------- 结果归一化（兼容新旧契约） -----------------------------
function firstValue(...values) {
  return values.find((v) => v !== undefined && v !== null && v !== '') ?? null;
}

function toUrl(value) {
  if (!value) return null;
  const s = String(value).trim();
  if (!s) return null;
  if (/^(https?:|blob:|data:)/i.test(s) || s.startsWith('/media/') || s.startsWith('/datasets/')) return s;
  return mediaUrl(s);
}

function inferSource(pathOrUrl) {
  const s = String(pathOrUrl || '').replace(/\\/g, '/');
  if (s.includes('/demo-lpr/')) return 'demo-lpr 合成演示样例';
  if (s.includes('/datasets/')) return 'datasets 演示图库';
  if (s.includes('/uploads/')) return '上传图片';
  if (s.includes('/crops/')) return '服务端裁剪图';
  if (s.includes('/photos/')) return '服务端照片';
  return '未知来源';
}

function normalizeResult(res) {
  const raw = res && typeof res === 'object' ? res : {};
  const imagePath = firstValue(raw.image_path, raw.input_image_path, raw.input_path);
  const cropPath = firstValue(raw.plate_crop_path, raw.crop_path, raw.plate_crop_image_path);
  const imageUrl = toUrl(firstValue(raw.image_url, raw.input_image_url, raw.input_url, imagePath));
  const cropUrl = toUrl(firstValue(raw.plate_crop_url, raw.crop_url, raw.plate_crop_image_url, cropPath));
  let conf = Number(firstValue(raw.overall_conf, raw.conf, raw.confidence, 0)) || 0;
  if (conf > 1) conf /= 100;
  const plate = firstValue(raw.plate, raw.plate_no, '') || '';
  const color = firstValue(raw.plate_color, raw.color, '') || '';
  const elapsed = firstValue(raw.elapsed_ms, raw.elapsed, raw.cost_ms);
  const source = raw.image_source || inferSource(imagePath || imageUrl);
  let synthetic = raw.image_synthetic;
  if (synthetic === undefined || synthetic === null) {
    synthetic = String(imagePath || imageUrl || '').includes('/demo-lpr/') ? true : null;
  }
  return {
    plate,
    color,
    conf,
    elapsed,
    candidates: Array.isArray(raw.candidates) ? raw.candidates : [],
    charConfs: Array.isArray(raw.char_confs) ? raw.char_confs : [],
    needManual: !!raw.need_manual,
    suspected: !!raw.suspected,
    message: raw.message || '',
    sourceStatus: raw.source_status || '',
    imagePath,
    cropPath,
    imageUrl,
    cropUrl,
    source,
    synthetic,
  };
}

// ----------------------------- 结果渲染 -----------------------------
function renderResult(box, res, opts = {}) {
  clear(box);
  const r = normalizeResult(res);
  const imageUrl = r.imageUrl || toUrl(opts.localUrl);
  const confPct = (r.conf * 100).toFixed(1);
  const confCls = r.conf >= 0.9 ? 'ok' : r.conf >= 0.7 ? 'warn' : 'danger';
  const quality = r.needManual
    ? el('span', { class: 'badge warn', text: '需人工核对' })
    : el('span', { class: 'badge ok', text: '高置信' });

  box.appendChild(el('div', { class: 'recognize-result-head' }, [
    el('div', {}, [
      r.plate
        ? el('span', { class: `plate-big tag-plate ${plateClass(r.color)}`, text: r.plate })
        : el('span', { class: 'badge danger', text: '未识别到车牌' }),
      el('div', { class: 'small muted mt', text: `颜色：${r.color || '—'} · 耗时 ${r.elapsed ?? '—'} ms` }),
    ]),
    el('div', { class: 'right' }, [
      el('div', { class: `badge ${confCls}`, text: `综合置信 ${confPct}%` }),
      el('div', { class: 'row mt', style: { gap: '6px' } }, [
        quality,
        r.suspected ? el('span', { class: 'badge info', text: '含易混字符' }) : null,
      ]),
    ]),
  ]));

  const facts = el('dl', { class: 'recognize-facts' }, [
    el('dt', { text: '识别状态' }), el('dd', { text: r.sourceStatus || '—' }),
    el('dt', { text: '结果说明' }), el('dd', { text: r.message || '—' }),
  ]);
  box.appendChild(facts);

  if (r.candidates.length) {
    box.appendChild(el('div', { class: 'small muted mt mb', text: '候选' }));
    box.appendChild(el('div', { class: 'recognize-candidates' },
      r.candidates.map((c) => el('span', { class: 'badge brand', style: { fontFamily: 'var(--mono)' }, text: String(c) }))));
  }

  if (r.charConfs.length && r.plate) {
    box.appendChild(el('div', { class: 'small muted mt mb', text: '逐字符置信度' }));
    const bars = el('div', { class: 'conf-bars' });
    Array.from(r.plate).forEach((ch, i) => {
      const c = Number(r.charConfs[i] ?? 0);
      const cls = c >= 0.9 ? '' : c >= 0.7 ? 'low' : 'bad';
      bars.appendChild(el('div', { class: 'conf-row' }, [
        el('span', { class: 'ch', text: ch }),
        el('div', { class: 'conf-track' }, [
          el('div', { class: `conf-fill ${cls}`, style: { width: `${Math.round(c * 100)}%` } }),
        ]),
        el('span', { class: 'small muted right', text: `${(c * 100).toFixed(0)}%` }),
      ]));
    });
    box.appendChild(bars);
  }

  const inputCard = el('div', { class: 'recog-card recognize-input-card mt' }, [
    el('div', { class: 'recog-head' }, [
      el('span', { class: 'badge brand recog-badge', text: '识别输入' }),
    ]),
    el('div', { class: 'recognize-media-grid' }, [
      mediaBox('识别输入（上传原图）', imageUrl),
      mediaBox('车牌裁剪图', r.cropUrl),
    ]),
  ]);
  box.appendChild(inputCard);
}

function mediaBox(caption, url) {
  return el('div', { class: 'recognize-media' }, [
    el('div', { class: 'small muted mb', text: caption }),
    url
      ? el('img', {
        class: 'recog-img', src: url, alt: caption,
        onerror: (e) => { e.target.replaceWith(el('div', { class: 'noimg', text: '图片不可用' })); },
      })
      : el('div', { class: 'noimg', text: '无可用图片' }),
  ]);
}

// ----------------------------- 识别日志（只读） -----------------------------
async function renderLogs(pane) {
  clear(pane);
  const body = el('div', {});
  pane.appendChild(el('div', { class: 'card' }, [
    el('h3', {}, ['最近识别记录（只读）', el('span', { class: 'spacer' }),
      el('button', { class: 'btn sm', text: '刷新', onclick: () => renderLogs(pane) })]),
    body,
  ]));
  body.appendChild(el('div', { class: 'empty' }, [el('div', { class: 'spinner' }), '加载中…']));
  try {
    const r = await Api.recognitionLogs(80);
    const items = r.items || [];
    clear(body);
    if (!items.length) {
      body.appendChild(el('div', { class: 'empty', text: '暂无识别记录' }));
      return;
    }
    const rows = items.map((it) => el('tr', {}, [
      el('td', { class: 'mono small', text: fmtTime(it.ts) }),
      el('td', {}, [el('span', { class: 'badge muted', text: it.gate || '—' })]),
      el('td', {}, [
        el('span', { class: `tag-plate ${plateClass(it.plate_color)}`, text: it.corrected_plate || it.plate || '—' }),
        it.manual_corrected ? el('div', { class: 'small muted', text: `原识别：${it.plate || '未识别'}` }) : null,
      ]),
      el('td', { class: 'num', text: `${(Number(it.overall_conf || 0) * 100).toFixed(1)}%` }),
      el('td', { class: 'num small', text: `${it.elapsed_ms ?? '—'} ms` }),
      el('td', {}, [it.success ? el('span', { class: 'badge ok', text: '成功' }) : el('span', { class: 'badge warn', text: '需人工' })]),
      el('td', {}, [it.image_path ? el('a', {
        href: mediaUrl(it.image_path), target: '_blank', rel: 'noopener', text: '原图',
      }) : el('span', { class: 'muted', text: '—' })]),
    ]));
    body.appendChild(el('div', { class: 'table-wrap' }, [
      el('table', {}, [
        el('thead', {}, [el('tr', {}, [
          el('th', { text: '时间(本地)' }), el('th', { text: '闸口' }), el('th', { text: '车牌' }),
          el('th', { class: 'num', text: '置信' }), el('th', { class: 'num', text: '耗时' }),
          el('th', { text: '结果' }), el('th', { text: '图像' }),
        ])]),
        el('tbody', {}, rows),
      ]),
    ]));
  } catch (e) {
    clear(body);
    body.appendChild(loadErrorCard(e.message || '加载失败', () => renderLogs(pane),
      { title: '识别日志加载失败' }));
  }
}
