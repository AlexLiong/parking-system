// 车位布局 E2E（2026-10 新特性后）：基本布局能力（1–20、C25→409、无 validate/C25 UI），
// 保留收缩→恢复与 80 位断言。不依赖已删除的 C25/预演能力。
const { chromium } = require('playwright');
const fs = require('fs');
const assert = require('assert/strict');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9635';
const out = process.env.EVIDENCE_DIR || '../_qa_audit/repair/P2-layout/evidence';
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
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto(base + '/sim/');
    await page.waitForFunction(() => window.__SIM__?.simApi?.rt?.sessionId, null, { timeout: 90000 });
    const auth = await page.request.post(base + '/api/auth/login',
      { data: { username: 'admin', password: 'admin123' } });
    assert.equal(auth.status(), 200);
    const token = (await auth.json()).token;
    const headers = { Authorization: 'Bearer ' + token };
    await page.evaluate(() => window.__SIM__.simApi.resetDemo());
    await page.evaluate(() => {
      window.__SIM__.layoutTestRefs = {
        planner: window.__SIM__.paths.planner,
        reservations: window.__SIM__.paths.reservations,
        props: window.__SIM__.scene.propGroup.children.length,
        colliders: window.__SIM__.scene.staticColliders.length,
      };
      window.__SIM__.scheduler._zoneCache.set('stale', {});
    });

    const zones = (n) => ['A', 'B', 'C', 'D'].map((zone) => ({
      zone, count: zone === 'C' ? n : 20, kind: zone === 'C' ? 'ne_prefer' : 'normal',
    }));
    async function apply(z) {
      const r = await page.request.post(base + '/api/admin/layout', { headers, data: { zones: z } });
      return { status: r.status(), data: await r.json().catch(() => ({})) };
    }
    async function waitSpaces(version, count) {
      await page.waitForFunction(
        (args) => window.__SIM__.scene.layoutVersion === args.v
          && window.__SIM__.scene.spaces.size === args.n,
        { v: version, n: count }, { timeout: 30000 });
      await page.waitForFunction(
        (n) => document.getElementById('free-total')?.textContent === String(n),
        count, { timeout: 15000 });
    }

    // 1) 范围下界 C=1 可接受（A/B/D=20 → 61 位）
    const lower = await apply(zones(1));
    assert.equal(lower.status, 200);
    await waitSpaces(lower.data.version, 61);
    await page.screenshot({ path: out + '/C1-min.png' });

    // 2) 收缩到 C=18（78 位）：热更新引用复用 / 缓存清理 / 道具碰撞体不重建
    const shrink = await apply(zones(18));
    assert.equal(shrink.status, 200);
    await waitSpaces(shrink.data.version, 78);
    const first = await page.evaluate(() => {
      const S = window.__SIM__;
      const refs = S.layoutTestRefs;
      return {
        scene: S.scene.spaces.size,
        paths: S.paths.spaces.length,
        markers: S.scene.markers.markers.size,
        plannerSame: S.paths.planner === refs.planner,
        reservationsSame: S.paths.reservations === refs.reservations,
        schedulerSame: S.scheduler.planner === S.paths.planner,
        cacheCleared: !S.scheduler._zoneCache.has('stale'),
        propsSame: S.scene.propGroup.children.length === refs.props,
        collidersSame: S.scene.staticColliders.length === refs.colliders,
      };
    });
    assert.equal(first.scene, 78);
    assert.equal(first.paths, 78);
    assert.equal(first.markers, 78);
    for (const k of ['plannerSame', 'reservationsSame', 'schedulerSame', 'cacheCleared',
                     'propsSame', 'collidersSame']) assert.ok(first[k], k);
    await page.screenshot({ path: out + '/C18.png' });

    // 3) 恢复 80 位
    const restored = await apply(zones(20));
    assert.equal(restored.status, 200);
    await waitSpaces(restored.data.version, 80);
    await page.screenshot({ path: out + '/C20-restored.png' });

    // 4) C25/越界/自定义坐标仍 409（能力删除 ≠ 放宽固定场地校验）
    const c25 = await apply(zones(25));
    assert.equal(c25.status, 409);
    const moved = await apply(zones(20).map((z) =>
      (z.zone === 'A' ? { ...z, anchor: [0, 0] } : z)));
    assert.equal(moved.status, 409);

    // 5) 旧 validate 预览接口已删除；layout 响应不再暴露旧能力字段
    const validateGet = await page.request.get(base + '/api/admin/layout/validate', { headers });
    assert.ok([404, 405].includes(validateGet.status()), 'validate GET should be removed, got ' + validateGet.status());
    const validatePost = await page.request.post(base + '/api/admin/layout/validate', { headers, data: {} });
    assert.ok([404, 405].includes(validatePost.status()), 'validate POST should be removed, got ' + validatePost.status());
    const layoutResp = await page.request.get(base + '/api/layout', { headers });
    assert.equal(layoutResp.status(), 200);
    const layout = await layoutResp.json();
    for (const legacy of ['layout_constraints', 'zone_capacity', 'runtime_geometry_fixed', 'validate']) {
      assert.ok(!(legacy in layout), 'legacy layout field present: ' + legacy);
    }

    // 6) 管理端布局页：基本能力 UI 存在；C25/预演入口不存在
    const admin = await ctx.newPage();
    await admin.goto(base + '/admin/');
    await admin.evaluate((t) => localStorage.setItem('parking.admin.token', t), token);
    await admin.goto(base + '/admin/#/layout');
    await admin.getByText('共 80 位', { exact: false }).waitFor();
    const adminText = await admin.evaluate(() => document.body.innerText);
    assert.ok(!/C25/.test(adminText), 'C25 should not appear in layout UI');
    assert.ok(!/预演/.test(adminText), 'preview(validate) UI should be removed');
    await admin.screenshot({ path: out + '/admin-layout.png', fullPage: true });

    assert.deepEqual(errors, []);
    const result = { lower: lower.status, shrink: shrink.status, restored: restored.status,
                     c25: c25.status, moved: moved.status,
                     validateGet: validateGet.status, validatePost: validatePost.status,
                     first, errors };
    fs.writeFileSync(out + '/browser.json', JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
