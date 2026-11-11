// E2E 支持：dispatch-plan 渲染牌图派车 / 等待 / 出场缴费工具（仅测试脚本使用）。
// 2026-10-25 新契约：后端外部模板渲染牌图 + expectedPlate；3D 相机入口/出口可识别；
// 仿真时间/人工授权/黑名单由后端 + 3D 调度器实现。
const assert = require('assert/strict');

async function waitParked(page, vehicleId, expectedPlate, timeout) {
  try {
    await page.waitForFunction(
      (id) => window.__SIM__.scheduler.vehicles.get(id)?.state === 'parked',
      vehicleId, { timeout });
  } catch (err) {
    const diag = await page.evaluate(async (id) => {
      const S = window.__SIM__;
      const snap = await S.simApi.rest.snapshot();
      const v = S.scheduler.vehicles.get(id);
      return {
        state: v?.state || null,
        meta: v?.meta ? { plate: v.meta.plate, expectedPlate: v.meta.expectedPlate,
                          plateType: v.meta.plateType, needManual: v.meta.needManual } : null,
        pending: (snap.pending_manual || []).filter((p) => p.vehicle_id === id),
      };
    }, vehicleId);
    const pend = diag.pending[0] || {};
    const msg = 'PRODUCT_CAMERA_RECOGNITION_PENDING: expected=' + (expectedPlate || '')
      + ' state=' + diag.state
      + ' recognized=' + (pend.source_plate || (pend.candidates || []).join('|') || 'n/a')
      + ' reason=' + (pend.reason || 'n/a');
    const error = new Error(msg);
    error.diagnostic = diag;
    throw error;
  }
}

async function waitState(page, vehicleId, state, timeout) {
  await page.waitForFunction(
    (args) => window.__SIM__.scheduler.vehicles.get(args.id)?.state === args.state,
    { id: vehicleId, state }, { timeout });
}

/**
 * dispatch-plan → 取渲染牌图 → spawnEntry（不等待 parked）。可一次派多辆用于排队场景。
 * @param {import('playwright').Page} page
 * @param {{vehicleId:string, plate:string, plateType?:string, expectedPlate?:string,
 *          planDurationSec?:number, autoExitDurationSec?:number,
 *          plateImageExpectedMismatch?:boolean}} opts
 */
async function spawnDispatchNoWait(page, opts) {
  const {
    vehicleId, plate, plateType = 'blue', expectedPlate = null,
    planDurationSec = 60, autoExitDurationSec = 0,
  } = opts;
  return page.evaluate(async (args) => {
    const { vehicleId, plate, plateType, expectedPlate, planDurationSec, autoExitDurationSec } = args;
    const token = window.__SIM__?.simApi?.rt?.token?.() || null;
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers.Authorization = 'Bearer ' + token;
    const planResp = await fetch('/api/sim/dispatch-plan', {
      method: 'POST', headers,
      body: JSON.stringify({ plate, plate_type: plateType, duration_sec: planDurationSec }),
    });
    const planText = await planResp.text();
    if (!planResp.ok) {
      const err = new Error(`dispatch-plan HTTP ${planResp.status}: ${planText.slice(0, 400)}`);
      err.status = planResp.status;
      err.body = planText;
      throw err;
    }
    const plan = JSON.parse(planText);
    const imgResp = await fetch(plan.image_url, {
      headers: token ? { Authorization: 'Bearer ' + token } : {},
    });
    if (!imgResp.ok) throw new Error(`plate image HTTP ${imgResp.status}: ${plan.image_url}`);
    const image = await createImageBitmap(await imgResp.blob());
    const vehicle = await window.__SIM__.scheduler.spawnEntry({
      vehicleId,
      plate: plan.plate,
      expectedPlate: expectedPlate || plan.plate,
      plateType: plan.plate_type,
      plateImage: image,
      imageUrl: plan.image_url,
      duration: autoExitDurationSec,
    });
    return {
      id: vehicle.id, plate: vehicle.meta.plate, expectedPlate: vehicle.meta.expectedPlate,
      plateType: vehicle.meta.plateType, imageUrl: plan.image_url,
      plan: { plate: plan.plate, plate_type: plan.plate_type, vehicle_kind: plan.vehicle_kind },
    };
  }, { vehicleId, plate, plateType, expectedPlate, planDurationSec, autoExitDurationSec });
}

/** dispatch-plan 派车并等待自动入库（parked）。 */
async function spawnWithDispatch(page, opts) {
  const timeout = opts.timeout || 90000;
  const info = await spawnDispatchNoWait(page, opts);
  await waitParked(page, info.id, info.plate, timeout);
  return info;
}

/** 手动 startExit → 等待 wait_exit；可选自动 pay；返回 recordId。 */
async function exitAndPay(page, vehicleId, { pay = true, timeout = 90000 } = {}) {
  await page.evaluate((id) => {
    const v = window.__SIM__.scheduler.vehicles.get(id);
    if (v && ['parked', 'to_space'].includes(v.state)) window.__SIM__.scheduler.startExit(id);
  }, vehicleId);
  await page.waitForFunction((id) => {
    const v = window.__SIM__.scheduler.vehicles.get(id);
    return !!v && ['to_exit', 'wait_exit'].includes(v.state);
  }, vehicleId, { timeout });
  const recordId = await page.evaluate((id) => {
    const v = window.__SIM__.scheduler.vehicles.get(id);
    return v?.meta?.recordId ?? null;
  }, vehicleId);
  if (pay && recordId != null) await page.evaluate((rid) => window.__SIM__.simApi.pay(rid), recordId);
  return recordId;
}

/** 等车辆离场并从 scheduler 移除。 */
async function waitGone(page, vehicleId, timeout = 90000) {
  await page.waitForFunction((id) => {
    const v = window.__SIM__.scheduler.vehicles.get(id);
    return !v || v.state === 'gone';
  }, vehicleId, { timeout });
}

module.exports = { spawnDispatchNoWait, spawnWithDispatch, waitParked, waitState, exitAndPay, waitGone };
