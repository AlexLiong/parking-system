// 新 E2E：外部模板 3D 入口/出口 expected 6/6 + 动态 yellow/green_large 自动入场。
// 每个车牌独立 force reset，拦截 /sim/events/arrived-frame 的 recognition 判定
// plate_match/need_manual；不依赖停车记录/缴费状态，避免产品入场后快速转出口影响。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const { spawnDispatchNoWait } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9639';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261025/E2E/evidence/screenshots';
fs.mkdirSync(out, { recursive: true });
const PLATES = [
  ['粤F4K1CZ', 'blue'], ['甘N35MYY', 'blue'],
  ['贵JD8H5PF', 'green'], ['渝GDKLV32', 'green'],
  ['鄂ABWTXHF', 'green_large'], ['桂ES6EKB', 'yellow'],
];
const norm = (s) => String(s || '').replace(/[·.\-\s]/g, '').toUpperCase();

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROMIUM_PATH,
    args: (process.env.E2E_GPU
      ? ['--enable-gpu', '--ignore-gpu-blocklist', '--use-angle=d3d11']
      : ['--enable-unsafe-swiftshader', '--use-angle=swiftshader']),
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  const frames = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('response', async (r) => {
    if (!r.url().includes('/sim/events/arrived-frame')) return;
    try {
      const body = await r.json();
      frames.push({ status: r.status(), vehicle_id: body.vehicle_id, gate: body.gate,
                    expected: body.expected_plate, recognition: body.recognition });
    } catch (_) { /* ignore */ }
  });
  let auth = null;
  async function resetPage() {
    await page.request.post(base + '/api/demo/reset', {
      headers: auth, data: { request_id: crypto.randomUUID(), mode: 'soft', new_session: true, force: true },
    });
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    await page.evaluate(async () => { await window.__SIM__.controls.setSpeed(10); });
  }
  function frameFor(vid, gate) {
    return frames.filter((f) => f.vehicle_id === vid && f.gate === gate).at(-1);
  }
  try {
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    const login = await page.request.post(base + '/api/auth/login', { data: { username: 'admin', password: 'admin123' } });
    auth = { Authorization: 'Bearer ' + (await login.json()).token };
    const summary = [];
    for (let i = 0; i < PLATES.length; i++) {
      const [plate, type] = PLATES[i];
      const vid = `plate-${i + 1}`;
      await resetPage();
      const spawned = await spawnDispatchNoWait(page, {
        vehicleId: vid, plate, plateType: type, planDurationSec: 120,
      });
      assert.equal(spawned.plate, plate);
      // 先等进入车位/出口流程；to_space 阶段再等 parked 后主动 startExit（自动出场守卫修复后车会稳定停住）
      await page.waitForFunction((id) => {
        const v = window.__SIM__.scheduler.vehicles.get(id);
        return !!v && ['parked', 'to_exit', 'wait_exit', 'leaving', 'gone'].includes(v.state);
      }, vid, { timeout: 90000 });
      const st = await page.evaluate((id) => window.__SIM__.scheduler.vehicles.get(id)?.state, vid);
      if (st === 'parked') {
        await page.evaluate((id) => window.__SIM__.scheduler.startExit(id), vid);
      }
      // 等入口/出口两次 arrived-frame（出口到达在 to_exit 完成时上报）
      const deadline = Date.now() + 60000;
      while (Date.now() < deadline && (!frameFor(vid, 'in') || !frameFor(vid, 'out'))) {
        await page.waitForTimeout(300);
      }
      const entry = frameFor(vid, 'in');
      const exit = frameFor(vid, 'out');
      assert.ok(entry, `missing entry frame for ${plate}`);
      assert.ok(exit, `missing exit frame for ${plate}`);
      const eRec = entry.recognition || {};
      const xRec = exit.recognition || {};
      assert.ok(eRec.plate_match === true || norm(eRec.plate) === norm(plate),
        `${plate} entry mismatch: ${JSON.stringify(eRec)}`);
      assert.ok(xRec.plate_match === true || norm(xRec.plate) === norm(plate),
        `${plate} exit mismatch: ${JSON.stringify(xRec)}`);
      assert.equal(!!eRec.need_manual, false, `${plate} entry need_manual`);
      assert.equal(!!xRec.need_manual, false, `${plate} exit need_manual`);
      summary.push({ plate, type, entry: { rec: eRec.plate, match: eRec.plate_match, conf: eRec.overall_conf },
                     exit: { rec: xRec.plate, match: xRec.plate_match, conf: xRec.overall_conf } });
    }
    assert.ok(summary.some((s) => s.type === 'yellow'));
    assert.ok(summary.some((s) => s.type === 'green_large'));
    await page.screenshot({ path: out + '/plates-external-3d.png' });
    assert.deepEqual(errors, []);
    fs.writeFileSync(out + '/plates-external-3d.json', JSON.stringify({ summary, frames, errors }, null, 2));
    console.log(JSON.stringify({ summary: summary.map((s) => ({ plate: s.plate, type: s.type,
      entry: s.entry.rec, exit: s.exit.rec })), frameCount: frames.length }));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
