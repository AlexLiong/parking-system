// 新 E2E：10× 仿真速度下停车 10 仿真分钟 → 计费/记录按仿真 10 分钟（非真实 1 分钟）；
// 同时验证暂停冻结仿真时钟、强制重置保留已完成历史。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const { spawnWithDispatch, waitGone } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9639';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261025/E2E/evidence/screenshots';
fs.mkdirSync(out, { recursive: true });
const PLATE = '甘N35MYY';

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
  page.on('pageerror', (e) => errors.push(String(e)));
  let auth = null;
  const api = (path, opts = {}) => page.request.fetch(base + path, { ...opts, headers: { ...auth, ...(opts.headers || {}) } });
  async function sim() {
    return page.evaluate(async () => (await window.__SIM__.simApi.rest.snapshot()).simulation);
  }
  async function records(plate) {
    const r = await api('/api/records?plate=' + encodeURIComponent(plate) + '&page_size=50');
    assert.equal(r.status(), 200);
    return r.json();
  }
  try {
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    const login = await page.request.post(base + '/api/auth/login', { data: { username: 'admin', password: 'admin123' } });
    assert.equal(login.status(), 200);
    auth = { Authorization: 'Bearer ' + (await login.json()).token };
    // 速度范围 0.25–10：10 生效、0.01 被 clamp 到 0.25
    const speedApplied = await page.evaluate(async () => {
      await window.__SIM__.controls.setSpeed(10);
      return { slider: Number(document.getElementById('speed')?.value),
               label: document.getElementById('speed-val')?.textContent,
               sim: window.__SIM__.simApi?.rt ? null : null };
    });
    assert.ok(Math.abs(Number(speedApplied.slider) - 10) < 0.01, JSON.stringify(speedApplied));
    const clamped = await page.evaluate(async () => {
      await window.__SIM__.controls.setSpeed(0.01);
      return { slider: Number(document.getElementById('speed')?.value),
               label: document.getElementById('speed-val')?.textContent };
    });
    assert.ok(Math.abs(Number(clamped.slider) - 0.25) < 0.01, JSON.stringify(clamped));
    await page.evaluate(async () => { await window.__SIM__.controls.setSpeed(10); });
    await page.request.post(base + '/api/demo/reset', {
      headers: auth, data: { request_id: crypto.randomUUID(), mode: 'soft', new_session: true, force: true },
    });
    await page.reload();
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    await page.evaluate(() => window.__SIM__.controls.setSpeed(10));

    const spawned = await spawnWithDispatch(page, {
      vehicleId: 'simtime-car', plate: PLATE, plateType: 'blue',
      planDurationSec: 3600, autoExitDurationSec: 3600, timeout: 90000,
    });
    let rec = await records(PLATE);
    assert.equal(rec.total, 1);
    const openRec = rec.items[0];
    assert.equal(openRec.status, 'in');
    const entrySim = Number(openRec.entry_sim_sec);
    assert.ok(Number.isFinite(entrySim) && entrySim >= 0, JSON.stringify(openRec));
    const simAtParked = Number((await sim()).sim_time);
    const wallStart = Date.now();
    console.log('DEBUG entrySim=' + entrySim + ' simAtParked=' + simAtParked + ' openRec=' + JSON.stringify(openRec).slice(0, 300));

    // 暂停：仿真时钟冻结（真实等待 1.2s）
    await page.click('#btn-pause');
    await page.waitForTimeout(400);
    const paused = await sim();
    assert.equal(paused.paused, true);
    const t1 = Number(paused.sim_time);
    await page.waitForTimeout(1200);
    const paused2 = await sim();
    assert.equal(paused2.paused, true);
    assert.ok(Math.abs(Number(paused2.sim_time) - t1) < 0.01, 'sim time must freeze while paused');
    await page.screenshot({ path: out + '/sim-billing-paused.png' });
    await page.click('#btn-pause');
    await page.waitForFunction(async () => !((await window.__SIM__.simApi.rest.snapshot()).simulation.paused), null, { timeout: 10000 });

    // 仿真时间前进 600s（10× 下约 60s 墙钟）
    // 停车 10 仿真分钟：从入库时刻起算（而非从入口识别起算，避免把入场行驶算入停车时长）
    const targetSim = simAtParked + 600;
    const deadline = Date.now() + 180000;
    let nowSim = Number((await sim()).sim_time);
    while (nowSim < targetSim && Date.now() < deadline) {
      await page.waitForTimeout(500);
      nowSim = Number((await sim()).sim_time);
    }
    assert.ok(nowSim >= targetSim, `sim clock did not reach ${targetSim}, got ${nowSim}`);
    console.log('DEBUG after wait sim=' + nowSim);

    // 自动结算适配：停车 10 仿真分钟后先在 parked 状态取报价（/api/quote 针对开放记录），
    // 再出场；后端识别后自动结算，无需 /api/pay。
    const quoteResp = await api('/api/quote?plate=' + encodeURIComponent(PLATE));
    assert.equal(quoteResp.status(), 200, await quoteResp.text());
    const quote = await quoteResp.json();
    await page.evaluate((id) => window.__SIM__.scheduler.startExit(id), spawned.id);
    const outDeadline = Date.now() + 90000;
    let final = null;
    while (Date.now() < outDeadline) {
      final = await records(PLATE);
      if ((final.items || [])[0]?.status === 'out') break;
      await page.waitForTimeout(500);
    }
    assert.equal(final.total, 1);
    const outRec = final.items[0];
    assert.equal(outRec.status, 'out');
    await waitGone(page, spawned.id, 30000).catch(() => {});
    const durationSim = Number(outRec.duration_sim_sec);
    console.log('DEBUG outRec=' + JSON.stringify(outRec).slice(0, 500) + ' quote=' + JSON.stringify(quote).slice(0, 300));
    // 入库后 10 仿真分钟：记录 duration_sim_sec 至少包含 600s 停车 + 入场行驶；报价分钟数 ≥10。
    assert.ok(durationSim >= 650, 'duration_sim_sec should include 10 sim minutes parking, got ' + durationSim);
    assert.ok(Number(quote.minutes) >= 10, 'quote.minutes should reflect >=10 sim min, got ' + quote.minutes);
    assert.ok(Number(outRec.exit_sim_sec) - Number(outRec.entry_sim_sec) >= 650);
    const wallSeconds = (Date.now() - wallStart) / 1000;
    assert.ok(wallSeconds < durationSim / 3,
      `real wall ${wallSeconds}s should be far below sim duration ${durationSim}s`);
    // 真实墙钟远小于业务仿真时间：created_at（真实）与 entry_time（业务 epoch+sim）不一致
    assert.notEqual(outRec.entry_time, outRec.created_at);
    await page.screenshot({ path: out + '/sim-billing-10min.png' });

    // 强制重置：不再 409，返回 force/aborted/released，且已完成历史保留
    const resetResp = await api('/api/demo/reset', {
      method: 'POST',
      data: { request_id: crypto.randomUUID(), mode: 'soft', new_session: true, force: true, reason: 'e2e reset' },
    });
    assert.equal(resetResp.status(), 200, await resetResp.text());
    const resetBody = await resetResp.json();
    const report = resetBody.reset_report || resetBody.report || {};
    assert.equal(report.force, true);
    assert.ok('released_spaces' in report && 'aborted_records' in report, JSON.stringify(report));
    const kept = await records(PLATE);
    assert.equal(kept.total, 1, 'completed history should be preserved after force reset');
    assert.equal(kept.items[0].status, 'out');
    const snap = await page.evaluate(async () => (await window.__SIM__.simApi.rest.snapshot()));
    assert.equal(snap.layout.free_total, 80);
    assert.deepEqual(errors, []);
    fs.writeFileSync(out + '/sim-time-billing.json', JSON.stringify({
      speedApplied, clamped, entrySim, simAtParked, targetSim, durationSim, quote, outRec,
      wallSeconds, resetReport: report, errors,
    }, null, 2));
    console.log(JSON.stringify({ entrySim, durationSim, quoteMinutes: quote.minutes,
      resetForce: report.force, released: report.released_spaces, historyKept: kept.total }));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
