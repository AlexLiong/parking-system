// 新 E2E：黑名单入口流程（无后车→倒车离场；有后车→进场后直接出场）。
// 断言：不建普通 parking_record；有 incident/entry_denied 审计；blacklist-resolution 上报；
// 后车队列恢复正常；不产生第二辆/不清空后续车。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const { spawnDispatchNoWait, waitParked, waitGone } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9639';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261025/E2E/evidence/screenshots';
fs.mkdirSync(out, { recursive: true });

const PLATE = '苏B2S39K';       // 当前 demo-lpr blue-004
const REAR_PLATE = '甘N35MYY';  // 当前 demo-lpr blue-001

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
  const resolutions = [];
  const arrived = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('response', async (r) => {
    try {
      if (r.url().includes('/api/sim/blacklist-resolution')) {
        resolutions.push({ status: r.status(), body: await r.json() });
      }
      if (r.url().includes('/sim/events/arrived-frame')) {
        const req = r.request().postDataBuffer?.();
        arrived.push({ status: r.status(), body: await r.json() });
      }
    } catch (_) { /* ignore */ }
  });
  let auth = null;
  async function resetSim() {
    await page.request.post(base + '/api/demo/reset', {
      headers: auth, data: { request_id: crypto.randomUUID(), mode: 'soft', new_session: true, force: true },
    });
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    await page.waitForTimeout(300);
  }
  async function snapshot() {
    return page.evaluate(async () => {
      const S = window.__SIM__;
      return { snap: await S.simApi.rest.snapshot(), vehicles: [...S.scheduler.vehicles.values()].map((v) => ({ id: v.id, state: v.state, meta: v.meta })) };
    });
  }
  async function barrierActions() {
    const r = await page.request.get(base + '/api/barrier/actions?limit=300', { headers: auth });
    assert.equal(r.status(), 200);
    return (await r.json()).items || [];
  }
  async function startGateWatch() {
    await page.evaluate(() => {
      window.__gateWatch = { transitions: {}, violations: [] };
      window.__gateWatchTimer = setInterval(() => {
        const S = window.__SIM__;
        for (const [gateId, gateObj] of Object.entries(S.scene.gates || {})) {
          const st = gateObj.state;
          const w = window.__gateWatch;
          w.transitions[gateId] ||= [];
          const last = w.transitions[gateId].at(-1);
          if (!last || last.state !== st || !!last.passable !== !!gateObj.isPassable) {
            w.transitions[gateId].push({ state: st, passable: !!gateObj.isPassable,
              manual: !!gateObj.manual, authorization: gateObj.authorization || null,
              angle: gateObj.armAngleDeg ? gateObj.armAngleDeg() : null });
          }
          const center = gateObj.zoneCenter();
          const plane = center.z;
          for (const v of S.scheduler.vehicles.values()) {
            // 只检查确实在该闸口车道横向范围内的车，避免入口车穿过出口杆平面误报
            if (Math.abs(v.pos.x - center.x) > 2.6) continue;
            const half = Number(v.dims?.len || 4.6) / 2;
            const over = Math.min(v.pos.z + half - plane, plane - (v.pos.z - half));
            if (over > 0.25 && !gateObj.isPassable) {
              w.violations.push({ gate: gateId, id: v.id, state: v.state, z: v.pos.z,
                plane, over, gateState: st, passable: gateObj.isPassable });
            }
          }
        }
      }, 40);
    });
  }
  async function stopGateWatch() {
    return page.evaluate(() => { clearInterval(window.__gateWatchTimer); return window.__gateWatch; });
  }
  async function records(plate) {
    const r = await page.request.get(base + '/api/records?plate=' + encodeURIComponent(plate) + '&page_size=50', { headers: auth });
    assert.equal(r.status(), 200);
    return r.json();
  }
  async function incidentsContain(plate) {
    const r = await page.request.get(base + '/api/incidents?limit=200', { headers: auth });
    assert.equal(r.status(), 200);
    const body = await r.json();
    return { match: JSON.stringify(body).includes(plate), items: body.items || [] };
  }
  async function waitResolution(resolution, timeout = 90000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (resolutions.some((r) => r.status === 200 && r.body?.resolution === resolution)) return;
      await page.waitForTimeout(200);
    }
    throw new Error('blacklist-resolution not reported: ' + JSON.stringify(resolutions));
  }
  try {
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    const login = await page.request.post(base + '/api/auth/login', { data: { username: 'admin', password: 'admin123' } });
    assert.equal(login.status(), 200);
    auth = { Authorization: 'Bearer ' + (await login.json()).token };
    const added = await page.request.post(base + '/api/lists', {
      headers: auth, data: { plate: PLATE, list_type: 'black', note: 'E2E blacklist flow' },
    });
    assert.equal(added.status(), 200, 'add blacklist: ' + await added.text());

    // ---------- 场景 1：无后车 → 直接倒车离场 ----------
    await resetSim();
    await page.evaluate(() => window.__SIM__.controls.setSpeed(10));
    await startGateWatch();
    const alone = await spawnDispatchNoWait(page, {
      vehicleId: 'blk-alone', plate: PLATE, plateType: 'blue', planDurationSec: 60,
    });
    await page.waitForFunction(
      (id) => window.__SIM__.scheduler.vehicles.get(id)?.meta?.denied === true,
      alone.id, { timeout: 40000 });
    const denied = await page.evaluate((id) => {
      const v = window.__SIM__.scheduler.vehicles.get(id);
      return { state: v?.state, meta: v?.meta };
    }, alone.id);
    assert.equal(denied.meta.deniedMode, 'reverse_out', JSON.stringify(denied.meta));
    assert.equal(denied.meta.noRecord, true);
    await page.screenshot({ path: out + '/blacklist-no-rear-reverse.png' });
    await waitResolution('reversed');
    await page.waitForTimeout(500);
    const afterAlone = await snapshot();
    assert.ok(!afterAlone.vehicles.some((v) => String(v.meta?.plate || '').includes(PLATE)));
    assert.equal(afterAlone.snap.layout.free_total, 80);
    const recAlone = await records(PLATE);
    assert.equal(recAlone.total, 0, 'blacklist must not create parking_record');
    const incAlone = await incidentsContain(PLATE);
    assert.ok(incAlone.match, 'blacklist should create incident/audit record');
    assert.ok(resolutions.some((r) => r.status === 200 && r.body?.resolution === 'reversed'),
      'blacklist-resolution reversed not reported: ' + JSON.stringify(resolutions));
    const watchAlone = await stopGateWatch();
    assert.deepEqual(watchAlone.violations, [],
      'no-rear blacklisted car crossed closed barrier: ' + JSON.stringify(watchAlone.violations));
    const aloneActions = (await barrierActions()).filter(a => a.reason === 'blacklist_bypass');
    assert.ok(aloneActions.some(a => a.cmd === 'close' && a.gate === 'in'),
      'no-rear reversal should close entry gate via blacklist-gate: ' + JSON.stringify(aloneActions));

    // ---------- 场景 2：有后车 → 进场后直接出场，后车恢复 ----------
    await resetSim();
    await page.evaluate(() => window.__SIM__.controls.setSpeed(4));
    await startGateWatch();
    const blocked = await spawnDispatchNoWait(page, {
      vehicleId: 'blk-rear', plate: PLATE, plateType: 'blue', planDurationSec: 60,
    });
    const rear = await spawnDispatchNoWait(page, {
      vehicleId: 'rear-car', plate: REAR_PLATE, plateType: 'blue', planDurationSec: 120,
    });
    await page.waitForFunction(
      (id) => window.__SIM__.scheduler.vehicles.get(id)?.meta?.denied === true,
      blocked.id, { timeout: 60000 });
    const denied2 = await page.evaluate((id) => {
      const v = window.__SIM__.scheduler.vehicles.get(id);
      return { state: v?.state, meta: v?.meta };
    }, blocked.id);
    assert.equal(denied2.meta.deniedMode, 'forward_then_exit', JSON.stringify(denied2.meta));
    await page.screenshot({ path: out + '/blacklist-with-rear-forward.png' });
    await waitResolution('entered_then_left');
    // 后车必须**自动**继续入场（队列恢复）：后端 blacklist-resolution 自动重试同 gate 后车，
    // 前端只清旧 needManual 等新事件；不使用 admin authorize 兜底。
    // 后车可能入库后很快自动出场（planDuration 在仿真倍速下），因此接受
    // to_space/parked/to_exit/wait_exit/leaving/gone 任一状态即算自动恢复。
    await page.waitForFunction((id) => {
      const v = window.__SIM__.scheduler.vehicles.get(id);
      return !!v && ['to_space', 'parked', 'to_exit', 'wait_exit', 'leaving', 'gone'].includes(v.state)
        && !v.meta?.needManual;
    }, rear.id, { timeout: 90000 });
    await page.waitForTimeout(500);
    const pendingAfterRear = await page.evaluate(async () => (await window.__SIM__.simApi.rest.snapshot()).pending_manual || []);
    assert.equal(pendingAfterRear.length, 0, 'stale pending after blacklist recovery: ' + JSON.stringify(pendingAfterRear));
    const rearRecoveredVia = 'auto';
    await page.screenshot({ path: out + '/blacklist-rear-recovered.png' });
    const afterRear = await snapshot();
    assert.ok(!afterRear.vehicles.some((v) => String(v.meta?.plate || '').includes(PLATE)),
      'blacklisted car should leave the scene');
    const rearState = afterRear.vehicles.find((v) => v.id === rear.id)?.state || 'gone';
    assert.ok(['parked', 'to_space', 'wait_exit', 'to_exit', 'leaving', 'gone'].includes(rearState),
      'rear should auto-enter (not wait_entry), got ' + rearState);
    assert.ok([79, 80].includes(afterRear.snap.layout.free_total), 'free=' + afterRear.snap.layout.free_total);
    assert.equal((await records(PLATE)).total, 0);
    const rearRec = await records(REAR_PLATE);
    assert.equal(rearRec.total, 1, 'rear car should have normal record');
    assert.ok(['in', 'out'].includes(rearRec.items[0].status));
    const inc2 = await incidentsContain(PLATE);
    assert.ok(inc2.match);
    assert.ok(resolutions.some((r) => r.body?.resolution === 'entered_then_left'));
    const watchRear = await stopGateWatch();
    assert.deepEqual(watchRear.violations, [],
      'blacklisted bypass crossed closed barrier: ' + JSON.stringify(watchRear.violations));
    const bActions = (await barrierActions()).filter(a => a.reason === 'blacklist_bypass');
    const openActions = bActions.filter(a => a.cmd === 'open');
    const closeActions = bActions.filter(a => a.cmd === 'close');
    assert.ok(openActions.some(a => a.gate === 'in'), 'with-rear path must open entry gate via blacklist-gate');
    assert.ok(openActions.some(a => a.gate === 'out'), 'with-rear path must open exit gate via blacklist-gate');
    assert.ok(closeActions.some(a => a.gate === 'in'), 'with-rear path must close entry gate');
    assert.ok(closeActions.some(a => a.gate === 'out'), 'with-rear path must close exit gate');
    // 结束后双杆 closed
    const finalBarriers = watchRear.transitions;
    for (const gate of ['in', 'out']) {
      const seq = finalBarriers[gate] || [];
      assert.ok(seq.length > 0, 'no barrier samples for ' + gate);
      assert.ok(seq.some(s => s.passable === true), gate + ' should be passable during blacklist bypass');
      assert.equal(seq.at(-1).state, 'closed', gate + ' should end closed: ' + JSON.stringify(seq.at(-1)));
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(out + '/blacklist-flow.json', JSON.stringify({
      alone: denied, withRear: denied2, afterAlone: afterAlone.snap.layout,
      afterRear: afterRear.snap.layout, recordsAlone: recAlone.total,
      recordsRear: rearRec.total, rearRecoveredVia, resolutions, incidents: inc2.items.length,
      watchAlone, watchRear, aloneActions, barrierActions: bActions, errors,
    }, null, 2));
    console.log(JSON.stringify({
      aloneMode: denied.meta.deniedMode, withRearMode: denied2.meta.deniedMode,
      rearState,
      recordsBlack: recAlone.total, recordsRear: rearRec.total,
      resolutions: resolutions.map((r) => r.body?.resolution), rearRecoveredVia, incidents: inc2.items.length,
      barrierOpen: bActions.filter(a => a.cmd === 'open').map(a => a.gate),
      barrierClose: bActions.filter(a => a.cmd === 'close').map(a => a.gate),
      violations: watchAlone.violations.length + watchRear.violations.length,
    }));
  } finally {
    try { await page.request.delete(base + '/api/lists/' + encodeURIComponent(PLATE), { headers: auth || {} }); } catch (_) {}
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
