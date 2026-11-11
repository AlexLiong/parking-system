// 反向寻车 E2E（2026-10 新特性后）：dispatch-plan 渲染牌图+expected 派车 → 自动入库 →
// 管理端按完整车牌查到车位/入场照片/高亮。使用当前 demo-lpr 有效牌（粤F4K1CZ）。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const { spawnWithDispatch } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9639';
const out = process.env.EVIDENCE_DIR || '../_qa_audit/repair/P3-08/evidence';
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
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    await page.evaluate(async () => {
      await window.__SIM__.simApi.resetDemo();
      await window.__SIM__.controls.setSpeed(4);
    });
    // 粤F4K1CZ 为当前 demo-lpr manifest 中 blue-000-clean 的牌；
    // dispatch-plan 渲染牌图 + expectedPlate 派车（qa-sim 修复车牌面 UV 后 3D 相机 6/6 识别）。
    const spawned = await spawnWithDispatch(page, {
      vehicleId: 'find-car', plate: '粤F4K1CZ', plateType: 'blue',
      planDurationSec: 600, autoExitDurationSec: 600, timeout: 90000,
    });
    const vehicle = await page.evaluate(() => window.__SIM__.scheduler.vehicles.get('find-car').meta);
    assert.equal(vehicle.plate, spawned.plate);
    assert.equal(vehicle.plate, '粤F4K1CZ');

    const admin = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    admin.on('pageerror', (e) => errors.push(String(e)));
    const login = await admin.request.post(base + '/api/auth/login',
      { data: { username: 'admin', password: 'admin123' } });
    assert.equal(login.status(), 200);
    const token = (await login.json()).token;
    await admin.goto(base + '/admin/');
    await admin.evaluate((t) => localStorage.setItem('parking.admin.token', t), token);
    await admin.goto(base + '/admin/#/find-car');
    const plateInput = admin.getByLabel('完整车牌');
    await plateInput.waitFor();
    // 输入允许小写/中点，前端归一化后查询
    const display = spawned.plate;
    const typed = display.slice(0, 1) + display.slice(1, 3).toLowerCase() + '·'
      + display.slice(3, 4).toLowerCase() + display.slice(4);
    await plateInput.fill(typed);
    await plateInput.press('Enter');
    await admin.getByText('车位 ' + vehicle.spaceNo, { exact: true }).waitFor();
    const photo = admin.getByRole('img', { name: display + ' 入场照片' });
    await photo.waitFor();
    const photoAlt = await photo.getAttribute('alt');
    const foundText = await admin.evaluate(() => document.body.innerText);
    assert.ok(foundText.includes('车位 ' + vehicle.spaceNo));
    await admin.screenshot({ path: out + '/found.png', fullPage: true });

    await plateInput.fill('沪UD76841');
    await plateInput.press('Enter');
    await admin.getByRole('alert').filter({ hasText: '未找到' }).waitFor();
    await admin.screenshot({ path: out + '/not-found.png' });
    assert.deepEqual(errors, []);
    const result = { plate: display, space: vehicle.spaceNo, typed, errors, photoAlt };
    fs.writeFileSync(out + '/browser.json', JSON.stringify(result, null, 2));
    console.log('find car passed ' + JSON.stringify(result));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
