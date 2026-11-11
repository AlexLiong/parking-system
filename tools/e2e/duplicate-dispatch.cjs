// 新 E2E：同一车牌重复派车 → 前端/后端 409 duplicate，不产生第二辆。
// 走 3D 手动派车 modal（新 UI）；并直接验证 /api/sim/dispatch-plan 409 duplicate_entry。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9639';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261025/E2E/evidence/screenshots';
fs.mkdirSync(out, { recursive: true });
const PLATE = '粤F4K1CZ';

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
  async function countVehicles() {
    return page.evaluate((plate) => [...window.__SIM__.scheduler.vehicles.values()]
      .filter((v) => String(v.meta?.plate || '').replace(/[·.\-\s]/g, '').toUpperCase() === plate)
      .map((v) => ({ id: v.id, state: v.state })), PLATE);
  }
  async function records() {
    const r = await page.request.get(base + '/api/records?plate=' + encodeURIComponent(PLATE) + '&page_size=50', { headers: auth });
    assert.equal(r.status(), 200);
    return r.json();
  }
  async function openModal() {
    await page.click('#btn-open-dispatch');
    await page.waitForFunction(() => document.getElementById('manual-modal')?.hidden === false, null, { timeout: 10000 });
  }
  async function fillAndDispatch() {
    await page.fill('#manual-plate', PLATE);
    await page.selectOption('#manual-type', 'blue');
    await page.fill('#manual-duration', '5');
    await page.selectOption('#manual-duration-unit', 'min');
    await page.click('#btn-dispatch');
  }
  try {
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    const login = await page.request.post(base + '/api/auth/login', { data: { username: 'admin', password: 'admin123' } });
    auth = { Authorization: 'Bearer ' + (await login.json()).token };
    await page.request.post(base + '/api/demo/reset', {
      headers: auth, data: { request_id: crypto.randomUUID(), mode: 'soft', new_session: true, force: true },
    });
    await page.reload();
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    await page.evaluate(() => window.__SIM__.controls.setSpeed(10));

    // 第一次 modal 派车（成功）
    await openModal();
    await fillAndDispatch();
    await page.waitForFunction(() => document.getElementById('manual-modal')?.hidden === true, null, { timeout: 15000 });
    await page.waitForFunction((plate) => [...window.__SIM__.scheduler.vehicles.values()]
      .some((v) => String(v.meta?.plate || '').replace(/[·.\-\s]/g, '').toUpperCase() === plate && v.state === 'parked'),
      PLATE, { timeout: 90000 });
    assert.equal((await countVehicles()).length, 1);
    assert.equal((await records()).total, 1);

    // 第二次同牌 modal 派车：前端拒绝（duplicate_plate），modal 保留，不产生第二辆
    await openModal();
    await fillAndDispatch();
    await page.waitForFunction(() => {
      const el = document.getElementById('manual-status');
      return /duplicate|重复|已有/.test(el?.textContent || '');
    }, null, { timeout: 15000 });
    const statusText = await page.textContent('#manual-status');
    assert.ok(/duplicate|重复|已有/.test(statusText), statusText);
    assert.equal(await page.evaluate(() => document.getElementById('manual-modal')?.hidden), false,
      'failure should keep modal open');
    await page.screenshot({ path: out + '/duplicate-dispatch-modal.png' });
    assert.equal((await countVehicles()).length, 1, 'must not spawn a second vehicle');
    assert.equal((await records()).total, 1, 'must not create a second record');

    // 后端直接二次 dispatch-plan → 409 duplicate_entry（不生成牌图/不派车）
    const apiResult = await page.evaluate(async (plate) => {
      const token = window.__SIM__.simApi.rt.token();
      const r = await fetch('/api/sim/dispatch-plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ plate, plate_type: 'blue', duration_sec: 60 }),
      });
      const text = await r.text();
      return { status: r.status, body: text.slice(0, 500) };
    }, PLATE);
    assert.equal(apiResult.status, 409, apiResult.body);
    assert.ok(apiResult.body.includes('duplicate_entry'), apiResult.body);
    assert.equal((await countVehicles()).length, 1);
    assert.deepEqual(errors, []);
    fs.writeFileSync(out + '/duplicate-dispatch.json', JSON.stringify({
      plate: PLATE, statusText, apiResult, vehicles: await countVehicles(), records: (await records()).total, errors,
    }, null, 2));
    console.log(JSON.stringify({ plate: PLATE, statusText, apiStatus: apiResult.status,
      vehicles: (await countVehicles()).length, records: (await records()).total }));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
