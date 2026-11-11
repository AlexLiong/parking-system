// 默认剧本 E2E（2026-10-25 新默认 6 牌）：真实默认剧本（data/scenario.json/server builtin）
// 完整播放，验证入口/出口各 6/6 expected 匹配、无 pending、无重叠、进度/完成/空场。
// 不再使用短测试剧本；默认剧本总时长较长，CI 用 SCENARIO_SPEED=10 加速（断言不降级）。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9636';
const out = process.env.SCENARIO_EVIDENCE_DIR || '../_qa_audit/repair_tasks_20261025/E2E/evidence/screenshots';
fs.mkdirSync(out, { recursive: true });
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
      frames.push({ vehicle_id: body.vehicle_id, gate: body.gate,
                    expected: body.expected_plate, recognition: body.recognition });
    } catch (_) { /* ignore */ }
  });
  try {
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    await page.waitForFunction(() => {
      const s = window.__SIM__?.scenario;
      return !!(s && s.loaded && s.totalVehicles > 0 && (s.ready === true || s.preloadError));
    }, null, { timeout: 120000 });
    const scen = await page.evaluate(() => {
      const s = window.__SIM__.scenario;
      const box = document.getElementById('scenario-progress');
      return {
        name: s.name, ready: s.ready, preloadError: s.preloadError,
        preloadTotal: s.preloadTotal, preloadLoaded: s.preloadLoaded,
        duration: Number(s.duration) || 0, totalVehicles: s.totalVehicles,
        steps: s.steps.length, progressVisible: !!(box && !box.hidden),
        progressText: box ? box.innerText.trim() : '',
        vehicles: s.steps.filter((x) => x.action === 'spawn')
          .map((x) => ({ vid: x.vid, plate: x.plate, plate_type: x.plate_type })),
      };
    });
    if (!scen.ready) throw new Error('DEFAULT_SCENARIO_PRELOAD_FAILED: ' + scen.preloadError);
    assert.equal(scen.totalVehicles, 6, JSON.stringify(scen));
    assert.ok(scen.progressVisible, 'scenario progress panel missing');
    assert.ok(scen.progressText.includes('6'), 'progress should mention 6: ' + scen.progressText);
    await page.screenshot({ path: out + '/default-scenario-loaded.png' });

    const speed = Number(process.env.SCENARIO_SPEED || 10);
    await page.evaluate(async (spd) => {
      await window.__SIM__.simApi.resetDemo();
      await window.__SIM__.controls.setSpeed(spd);
      window.__SIM__.scenario.play();
    }, speed);
    const started = Date.now();
    const maxWallMs = Number(process.env.SCENARIO_MAX_WAIT_SECONDS || 600) * 1000;
    const samples = [];
    const statesSeen = new Set();
    let finalScenario = null;
    let simultaneous = false;
    while (Date.now() - started < maxWallMs) {
      const s = await page.evaluate(async () => {
        const S = window.__SIM__;
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
        const snap = await S.simApi.rest.snapshot();
        const box = document.getElementById('scenario-progress');
        return {
          scenario: S.scenario.debugSnapshot(),
          progressText: box ? box.innerText.trim() : '',
          snapshot: snap,
          vehicles: vs.map((v) => ({ id: v.id, state: v.state, plate: v.meta.plate,
                                     space: v.meta.spaceNo })),
          overlaps,
        };
      });
      assert.deepEqual(s.overlaps, [], 'vehicle overlap');
      assert.deepEqual(s.scenario.failures, [], 'scenario failures: ' + JSON.stringify(s.scenario.failures));
      const pending = (s.snapshot.pending_manual || []);
      if (pending.length) {
        throw new Error('DEFAULT_SCENARIO_PENDING: ' + JSON.stringify(pending));
      }
      s.vehicles.forEach((v) => statesSeen.add(v.state));
      const hasIn = s.vehicles.some((v) => ['approach', 'wait_entry', 'to_space'].includes(v.state));
      const hasOut = s.vehicles.some((v) => ['to_exit', 'wait_exit', 'leaving'].includes(v.state));
      if (hasIn && hasOut && !simultaneous) {
        simultaneous = true;
        await page.screenshot({ path: out + '/default-scenario-simultaneous.png' });
      }
      samples.push({ clock: s.scenario.clock, progress: s.progressText,
                     vehicles: s.vehicles, free: s.snapshot.layout.free_total,
                     finished: s.scenario.finished });
      if (samples.length % 40 === 0) console.log(JSON.stringify(samples.at(-1)));
      finalScenario = s;
      if (s.scenario.finished && s.vehicles.length === 0 && s.snapshot.layout.free_total === 80) break;
      await page.waitForTimeout(500);
    }
    assert.ok(finalScenario, 'no scenario sample');
    assert.ok(finalScenario.scenario.finished, 'default scenario should finish: ' + JSON.stringify(finalScenario.scenario));
    assert.equal(finalScenario.vehicles.length, 0);
    assert.equal(finalScenario.snapshot.layout.free_total, 80);
    assert.match(finalScenario.progressText, /总\s*6/, 'progress total: ' + finalScenario.progressText);
    assert.match(finalScenario.progressText, /已入场\s*6/, 'progress entered: ' + finalScenario.progressText);
    assert.match(finalScenario.progressText, /已出场\s*6/, 'progress exited: ' + finalScenario.progressText);
    assert.ok(statesSeen.has('parked'));
    assert.ok(statesSeen.has('wait_exit') || statesSeen.has('leaving') || statesSeen.has('to_exit'));

    const byGate = {};
    for (const f of frames) {
      if (!f.vehicle_id || !f.gate) continue;
      byGate[f.vehicle_id] ||= {};
      byGate[f.vehicle_id][f.gate] = f;
    }
    const summary = [];
    for (const v of scen.vehicles) {
      const vid = v.vid;
      const entry = byGate[vid]?.in;
      const exit = byGate[vid]?.out;
      assert.ok(entry, `missing entry frame for ${vid}`);
      assert.ok(exit, `missing exit frame for ${vid}`);
      for (const [gate, f] of [['in', entry], ['out', exit]]) {
        const rec = f.recognition || {};
        assert.ok(rec.plate_match === true || norm(rec.plate) === norm(v.plate),
          `${vid}/${gate} expected=${v.plate} got=${rec.plate} ${JSON.stringify(rec)}`);
        assert.equal(!!rec.need_manual, false, `${vid}/${gate} need_manual`);
      }
      summary.push({ vid, plate: v.plate, type: v.plate_type,
                     entry: entry.recognition?.plate, exit: exit.recognition?.plate });
    }
    assert.equal(summary.length, 6);
    const records = await page.evaluate(async () => {
      const h = { Authorization: 'Bearer ' + window.__SIM__.simApi.rt.token() };
      return (await fetch('/api/records?status=out&page_size=50', { headers: h })).json();
    });
    assert.equal(records.total, 6);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: out + '/default-scenario-completed.png' });
    const artifact = {
      scenario: scen, speed, wallSeconds: (Date.now() - started) / 1000,
      simultaneous, statesSeen: [...statesSeen], frames: frames.length, summary,
      records: records.total, free: finalScenario.snapshot.layout.free_total,
      progress: finalScenario.progressText, errors, samples,
    };
    fs.writeFileSync(out + '/scenario-default.json', JSON.stringify(artifact, null, 2));
    console.log('scenario passed ' + JSON.stringify({
      speed, wallSeconds: artifact.wallSeconds, frames: frames.length,
      summary, records: records.total, progress: finalScenario.progressText,
    }));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
