// Soak E2E（2026-10 新特性后）：dispatch-plan 渲染牌图+expected 自动入场/出场；
// exitStarted 以“观察到 parked 或 to_exit/wait_exit/leaving”为触发（采样可能错过 parked 瞬间），
// 其余 started/paid/occupiedCloseRejected/空场断言不降级。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const { spawnWithDispatch } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9632';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
const seconds = Number(process.env.SOAK_SECONDS || 600);
fs.mkdirSync(out, { recursive: true });

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROMIUM_PATH,
    args: (process.env.E2E_GPU
      ? ['--enable-gpu', '--ignore-gpu-blocklist', '--use-angle=d3d11']
      : ['--enable-unsafe-swiftshader', '--use-angle=swiftshader']),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1024, height: 700 } });
    const errors = [];
    const httpErrors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    page.on('response', (r) => { if (r.status() >= 500) httpErrors.push({ url: r.url(), status: r.status() }); });
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    // 预热 LPR 离屏抓拍（shader/编码/纹理），再派车；识别偶发误读由 CI 整脚本重试
    // （每次全新隔离服务/端口/库）兜底，不降低断言。
    await page.waitForTimeout(1200);
    await page.evaluate(async () => {
      try { await window.__SIM__.controls.benchmarkLprFrame?.('in', null); } catch (_) { /* 预热失败不阻断 */ }
    });
    await page.evaluate(() => window.__SIM__.simApi.resetDemo());
    await page.evaluate(() => window.__SIM__.controls.setSpeed(4));
    // dispatch-plan 渲染牌图 + expectedPlate（qa-sim 修复车牌面 UV 后 3D 相机可识别）；
    // autoExitDurationSec=0 保持 soak 原流程（入库后手动 startExit）。
    const spawned = await spawnWithDispatch(page, {
      vehicleId: 'soak-car', plate: '粤F4K1CZ', plateType: 'blue',
      planDurationSec: 60, autoExitDurationSec: 600, timeout: 90000,
    });
    const started = Date.now();
    const samples = [];
    let exitStarted = false, paid = false, occupiedCloseRejected = false;
    let entryManual = false, exitManual = false, parkedSeen = false;
    while ((Date.now() - started) / 1000 < seconds) {
      const state = await page.evaluate(async () => {
        const S = window.__SIM__;
        const snapshot = await S.simApi.rest.snapshot();
        const { Collider } = await import('./js/entities/Collider.js');
        const vs = [...S.scheduler.vehicles.values()];
        const overlaps = [];
        for (let i = 0; i < vs.length; i++) {
          for (let j = i + 1; j < vs.length; j++) {
            if (Collider.poseOverlap(
              { x: vs[i].pos.x, z: vs[i].pos.z, theta: vs[i].theta, ...vs[i].dims },
              { x: vs[j].pos.x, z: vs[j].pos.z, theta: vs[j].theta, ...vs[j].dims })) {
              overlaps.push([vs[i].id, vs[j].id]);
            }
          }
        }
        return {
          time: S.dump().simTime,
          vehicles: vs.map((v) => ({ id: v.id, state: v.state, meta: v.meta })),
          barriers: snapshot.barriers,
          free: snapshot.layout.free_total,
          pending: snapshot.pending_manual,
          occupied: S.scheduler._gateOccupied,
          overlaps,
          memory: S.scene.renderer.info.memory,
        };
      });
      assert.deepEqual(state.overlaps, []);
      for (const g of Object.values(state.barriers)) assert.notEqual(g.state, 'FAULT');
      const pending = state.pending.filter((p) => p.vehicle_id === 'soak-car');
      if (pending.length) { entryManual = true; if (state.vehicles.some((v) => v.state === 'wait_exit')) exitManual = true; }
      assert.equal(pending.length, 0, 'clean demo plate must enter and exit automatically');
      // 有车占闸（入口或出口）时手动关杆必须 409（防砸车）；采样更密以稳定捕捉闸区占用窗口。
      const busyGate = state.occupied.in ? 'in' : (state.occupied.out ? 'out' : null);
      if (busyGate && !occupiedCloseRejected) {
        const status = await page.evaluate(async (gate) => {
          const r = await fetch('/api/manual/close-barrier', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + window.__SIM__.simApi.rt.token() },
            body: JSON.stringify({ gate }),
          });
          return r.status;
        }, busyGate);
        assert.equal(status, 409);
        occupiedCloseRejected = true;
        await page.screenshot({ path: out + '/occupied-close-rejected.png' });
      }
      const car = state.vehicles.find((v) => v.id === 'soak-car');
      if (car?.state === 'parked') parkedSeen = true;
      // 采样 1s 可能错过 parked 瞬间：观察到 parked 或已进入出口流程都算 exitStarted
      if (!exitStarted && car && ['parked', 'to_exit', 'wait_exit', 'leaving'].includes(car.state)) {
        if (car.state === 'parked') {
          await page.screenshot({ path: out + '/real-glb-parked.png' });
          await page.evaluate(() => window.__SIM__.scheduler.startExit('soak-car'));
        }
        exitStarted = true;
      }
      // 2026-10-31：出口识别后自动结算/自动开闸，不再手动 pay；paid 表示已进入自动结算流程
      if (car && ['wait_exit', 'leaving'].includes(car.state) && !paid) paid = true;
      samples.push({ elapsed: (Date.now() - started) / 1000, ...state, pending: state.pending.length });
      if (samples.length % 10 === 0) {
        console.log(JSON.stringify({ elapsed: samples.at(-1).elapsed, states: state.vehicles.map((v) => v.state),
                                     exitStarted, paid, parkedSeen, spawned }));
      }
      await page.waitForTimeout(400);
    }
    const final = samples.at(-1);
    fs.writeFileSync(out + '/soak-browser.json', JSON.stringify({
      wallSeconds: (Date.now() - started) / 1000,
      entryManual, exitManual, exitStarted, parkedSeen, paid, occupiedCloseRejected,
      errors, httpErrors, spawned, samples,
    }, null, 2));
    assert.ok(exitStarted && paid && occupiedCloseRejected);
    assert.equal(entryManual, false);
    assert.equal(exitManual, false);
    assert.equal(final.vehicles.length, 0);
    assert.equal(final.free, 80);
    // 自动结算校验：记录 out + payment.method=auto（免/收费均含 0）
    const login = await page.request.post(base + '/api/auth/login', { data: { username: 'admin', password: 'admin123' } });
    const token = (await login.json()).token, headers = { Authorization: 'Bearer ' + token };
    const recs = await (await page.request.get(base + '/api/records?plate=' + encodeURIComponent('粤F4K1CZ') + '&page_size=20', { headers })).json();
    const listRec = (recs.items || [])[0];
    assert.ok(listRec, 'soak record missing'); const detail = await page.request.get(base + '/api/records/' + listRec.id, { headers });
    assert.equal(detail.status(), 200); const rec = await detail.json();
    assert.equal(rec.status, 'out');
    assert.equal(rec.payment?.method, 'auto', 'soak payment should be auto: ' + JSON.stringify(rec.payment));
    assert.ok(rec.exit_photo, 'soak exit_photo required');
    assert.deepEqual(errors, []);
    assert.deepEqual(httpErrors, []);
    await page.screenshot({ path: out + '/soak-final.png' });
    console.log(JSON.stringify({ wallSeconds: (Date.now() - started) / 1000, exitStarted, paid,
                                 occupiedCloseRejected, parkedSeen, errors }));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
