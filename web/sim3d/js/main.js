// 3D 仿真端入口（js/main.js）—— route-v2 装配。
//
// 装配顺序：SceneManifest（lot_v2.json 单一事实源）→ ParkingScene（壳/标线/道具/道闸/碰撞体）
//          → PathNetwork 门面（内含 RoutePlanner/ReservationTable/ConflictZones，由 three-route 提供）
//          → TrafficScheduler（three-scheduler，预约门控/弧线/排队）→ ScenarioPlayer / UI
//          → 连接后端 WS（业务真理在后端：本端只上报物理事实、执行下发指令的动画）。
//
// 预览模式：`/sim/?preview=1` 只用 manifest.defaultSpaces() 生成 80 车位本地预览，
//          不连后端、不自动播放剧本，便于场景/灯光/相机验收。
// 调试契约：window.__SIM__ = { manifest, scene, rig, paths, scheduler, scenario, simApi, hud, dump() }
//          （见 _qa_audit/route_v2/contract.md §4.1；dump 必须可 JSON 序列化）。
import { ParkingScene } from './scene/ParkingScene.js';
import { CameraRig } from './scene/CameraRig.js';
import { SceneManifest } from './scene/SceneManifest.js';
import { PathNetwork } from './sim/PathNetwork.js';
import { TrafficScheduler } from './sim/TrafficScheduler.js';
import { ScenarioPlayer } from './sim/ScenarioPlayer.js';
import { SimApi } from './sim/SimApi.js';
import { HudOverlay } from './ui/HudOverlay.js';
import { ControlPanel } from './ui/ControlPanel.js';
import { PanelManager } from './ui/PanelDrag.js';
import { Rest, getToken } from './api.js';
import { GateDisplay } from './ui/GateDisplay.js';
import { loadBackendPlateImage } from './ui/PlateImage.js';
import { normalizeError, formatDetail } from './ui/EventFormatter.js';
import { SHELL, CAR_MODELS } from './config.js';
import * as THREE from 'three';

const canvas = document.getElementById('scene');
const hud = new HudOverlay();
const panels = new PanelManager();
const params = new URLSearchParams(location.search);
const bootLog = (...args) => {
  if (params.get('debug') === '1' || params.get('debug') === 'boot') hud.log(...args);
};
const PREVIEW = params.get('preview') === '1' || params.get('preview') === 'true';
const DEBUG_COLLIDERS = params.get('debug') === 'colliders' || params.get('colliders') === '1';
// ?shell=0 强制回退程序化地面（无壳联调），?shell=1 强制尝试壳；缺省用 config.SHELL.enabled
const SHELL_ENABLED = params.has('shell') ? params.get('shell') !== '0' : SHELL.enabled;

let displays;
let scene, rig, manifest, paths, planner = null, scheduler, scenario, simApi;
let speed = 1;
let paused = false;
let simTime = 0;
let lastT = 0;
let statAccum = 0;
let previewMode = false;

// —— 3D 闸口相机截图识别（主项目接入）——
// 相机位来自 manifest.props.lpr_cameras（blender-assets 输出）；缺失时用 idea2 标定兜底。
// 实验/调试：?lpr=1920x1080 可放大 3D 闸口相机抓拍分辨率；默认仍为 1280×720。
const LPR_SIZE = (() => {
  const m = /^(\d{3,5})x(\d{3,5})$/i.exec(params.get('lpr') || '');
  if (!m) return { width: 1280, height: 720 };
  return { width: Math.max(320, Math.min(4096, Number(m[1]))),
    height: Math.max(180, Math.min(2304, Number(m[2]))) };
})();
const LPR_FRAME = { width: LPR_SIZE.width, height: LPR_SIZE.height, fov: 30, maxBytes: 5 * 1024 * 1024 };
const LPR_FALLBACK = {
  in: { view_three: { x: -5.6, y: 3.405, z: 21.0 }, look_at_three: { x: -3.5, y: 0.6, z: 25.8 }, fov_vertical_deg: 30 },
  out: { view_three: { x: 5.6, y: 3.405, z: 39.0 }, look_at_three: { x: 3.5, y: 0.6, z: 33.7 }, fov_vertical_deg: 30 },
};
let lprCaptureTarget = null;
let lprCaptureCamera = null;
let lprCapturing = false;
// C：首帧 LPR 截图冷启动优化。启动时预热 shader/离屏 buffer/PNG 编码/车辆材质；
// ?warmup=0 可关闭全部预热，用于前后对照。
const WARMUP_ENABLED = params.get('warmup') !== '0';
// 预览页默认不预热；?preview=1&warmup=1 用于本地测量预热效果。
const WARMUP_TEST = WARMUP_ENABLED && params.get('warmup') === '1';
let lprWarmupPromise = Promise.resolve(null);
let lprWarmupDetails = null;
let lprCaptureChain = Promise.resolve();
const lprCaptureStats = [];
let lprVehiclePrewarmDone = false;

function resize() {
  const w = window.innerWidth, h = window.innerHeight;
  if (scene) scene.resize(w, h);
  if (rig) rig.resize(w, h);
}

// ------------------------- LPR 闸口相机截图（3D → 识别） -------------------------
function lprCameraList() {
  const raw = manifest?.raw || manifest || {};
  const props = raw.props || {};
  return Array.isArray(props.lpr_cameras) ? props.lpr_cameras : [];
}

function lprCameraCfg(gate) {
  const cam = lprCameraList().find((c) => c && c.gate === gate);
  return cam || LPR_FALLBACK[gate === 'out' ? 'out' : 'in'];
}

/**
 * P2-02：加载道闸屏照片（/media 鉴权路径）。优先 createImageBitmap，回退 Image；
 * 照片失败静默返回 null，闸口屏保留文字内容，不影响业务。
 */
async function loadGateScreenPhoto(url) {
  if (!url) return null;
  try {
    const headers = {};
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(url, { headers, cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    if (typeof createImageBitmap === 'function') return await createImageBitmap(blob);
    const objectUrl = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.src = objectUrl;
      if (img.decode) await img.decode();
      return img;
    } finally {
      setTimeout(() => { try { URL.revokeObjectURL(objectUrl); } catch (_) { /* ignore */ } }, 5000);
    }
  } catch (e) {
    console.warn('[sim3d] 道闸屏照片加载失败（保留文字屏）：', url, e?.message || e);
    return null;
  }
}

function canonPlate(s) {
  return String(s || '').replace(/[·\s]/g, '').toUpperCase();
}

function expectedPlateFor(vehicleId, plate = null) {
  try {
    let v = scheduler?.vehicles?.get?.(vehicleId) || null;
    if (!v && scheduler?.vehicles?.values) {
      v = [...scheduler.vehicles.values()].find((x) =>
        x.vid === vehicleId || x.id === vehicleId ||
        (plate && canonPlate(x.meta?.plate) === canonPlate(plate))) || null;
    }
    return v?.meta?.expectedPlate || v?.meta?.plate || null;
  } catch (_) { return null; }
}

/** plate_recognized WS 消息附加 expected/matched/来源标注，供“识别输入”卡片显示。 */
function annotateRecognition(m) {
  if (!m) return m;
  m.expected_plate = m.expected_plate || expectedPlateFor(m.vehicle_id, m.plate);
  m.matched = (m.expected_plate && m.plate)
    ? canonPlate(m.expected_plate) === canonPlate(m.plate) : null;
  if (!m.image_source) {
    try {
      const v = scheduler?.vehicles?.get?.(m.vehicle_id);
      if (v?.meta?.lastCameraFrame) m.image_source = '3D 闸口相机截图';
    } catch (_) { /* ignore */ }
  }
  return m;
}

/**
 * 从对应 LPR 闸口相机渲染 1280×720 PNG（不打扰主画面：离屏 RenderTarget，
 * 同步读取像素后立即恢复主 renderTarget），供 POST /sim/events/arrived-frame。
 */
async function captureStopLineFrame(gate, vehicle) {
  if (!scene?.renderer || !scene?.scene) throw new Error('3D 场景未就绪');
  const cfg = lprCameraCfg(gate);
  const view = cfg.view_three || LPR_FALLBACK[gate === 'out' ? 'out' : 'in'].view_three;
  const fov = Number(cfg.fov_vertical_deg || LPR_FRAME.fov);
  const w = LPR_FRAME.width, h = LPR_FRAME.height;
  const renderer = scene.renderer;
  if (!lprCaptureTarget || lprCaptureTarget.width !== w || lprCaptureTarget.height !== h) {
    lprCaptureTarget?.dispose?.();
    lprCaptureTarget = new THREE.WebGLRenderTarget(w, h, {
      depthBuffer: true, stencilBuffer: false,
      minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter,
    });
    lprCaptureTarget.texture.colorSpace = THREE.SRGBColorSpace;
  }
  if (!lprCaptureCamera) lprCaptureCamera = new THREE.PerspectiveCamera(fov, w / h, 0.1, 600);
  const t0 = performance.now();
  // 抓拍/编码期间暂停主画面渲染，避免 SwiftShader/软件 WebGL 下 PNG 编码被主渲染循环拖慢。
  lprCapturing = true;
  try {
  scene.scene.updateMatrixWorld(true);
  // 瞄准本次车辆前牌（优先取 Plate_F 世界坐标，避免不同车型/偏移时偏框）
  const target = new THREE.Vector3();
  const plateMesh = vehicle?.plateMeshes?.[0];
  if (plateMesh) {
    plateMesh.updateWorldMatrix(true, false);
    plateMesh.getWorldPosition(target);
  } else {
    const nose = vehicle?.nosePos?.() || { x: 0, z: 0 };
    target.set(nose.x, 0.62, nose.z);
  }
  if (!Number.isFinite(target.y) || target.y < 0.05) target.y = 0.62;
  lprCaptureCamera.position.set(view.x, view.y, view.z);
  lprCaptureCamera.fov = fov;
  lprCaptureCamera.aspect = w / h;
  lprCaptureCamera.updateProjectionMatrix();
  lprCaptureCamera.lookAt(target);
  lprCaptureCamera.updateMatrixWorld(true);

  const prevTarget = renderer.getRenderTarget();
  const prevViewport = renderer.getViewport(new THREE.Vector4());
  const pixels = new Uint8Array(w * h * 4);
  const tRender0 = performance.now();
  try {
    renderer.setRenderTarget(lprCaptureTarget);
    renderer.setViewport(0, 0, w, h);
    renderer.render(scene.scene, lprCaptureCamera);
    renderer.readRenderTargetPixels(lprCaptureTarget, 0, 0, w, h, pixels);
  } finally {
    renderer.setRenderTarget(prevTarget);
    renderer.setViewport(prevViewport);
  }
  const tRender1 = performance.now();
  // WebGL 原点在左下：翻转到 2D canvas
  const out = document.createElement('canvas');
  out.width = w; out.height = h;
  const ctx = out.getContext('2d');
  const imgData = ctx.createImageData(w, h);
  const row = w * 4;
  for (let y = 0; y < h; y++) {
    const src = (h - 1 - y) * row;
    imgData.data.set(pixels.subarray(src, src + row), y * row);
  }
  ctx.putImageData(imgData, 0, 0);
  const tEncode0 = performance.now();
  const blob = await new Promise((resolve) => out.toBlob(resolve, 'image/png'));
  const tEncode1 = performance.now();
  if (!blob) throw new Error('闸口截图编码失败');
  if (blob.size > LPR_FRAME.maxBytes) {
    throw new Error(`闸口截图 ${(blob.size / 1024 / 1024).toFixed(2)} MiB 超过 5MiB 限制`);
  }
  return { blob, gate, camera: cfg, width: w, height: h,
    capture_ms: Math.round(performance.now() - t0),
    render_read_ms: Math.round(tRender1 - tRender0),
    encode_ms: Math.round(tEncode1 - tEncode0),
    bytes: blob.size };
  } finally { lprCapturing = false; }
}

/**
 * C：首帧相机截图冷启动预热。
 * 启动时完成：LPR 相机 shader 编译、WebGLRenderTarget/读回、2D canvas PNG 编码、
 * 图片/车辆材质首次使用；并在第一辆车 spawn 时用真实车型再预热一次。
 */
async function warmupLprPipeline() {
  if (!WARMUP_ENABLED || !scene?.renderer || (PREVIEW && !WARMUP_TEST)) return null;
  const started = performance.now();
  const details = { enabled: true, started_ms: Math.round(started) };
  let tempCar = null;
  try {
    // 等首帧渲染完成，避免与主渲染循环初始化竞争
    await new Promise((resolve) => requestAnimationFrame(() => resolve()));
    // 1) 编译 LPR 相机视角需要的全部 shader 程序（入口/出口）
    const compileCam = new THREE.PerspectiveCamera(LPR_FRAME.fov, LPR_FRAME.width / LPR_FRAME.height, 0.1, 600);
    for (const gate of ['in', 'out']) {
      const cfg = lprCameraCfg(gate);
      const v = cfg.view_three || LPR_FALLBACK[gate].view_three;
      const t = cfg.look_at_three || LPR_FALLBACK[gate].look_at_three;
      compileCam.position.set(v.x, v.y, v.z);
      compileCam.lookAt(t.x, t.y, t.z);
      compileCam.updateMatrixWorld(true);
      try {
        if (typeof scene.renderer.compileAsync === 'function') await scene.renderer.compileAsync(scene.scene, compileCam);
        else scene.renderer.compile(scene.scene, compileCam);
      } catch (e) { console.warn('[sim3d] LPR shader 预热 compile 失败（继续）', e?.message || e); }
    }
    // 2) 临时放置一辆真实 GLB 车在停止线，预热车辆材质 + 完整离屏截图/PNG 编码路径
    try {
      tempCar = await scene.assets.cloneModel(CAR_MODELS.blue[0].file);
      tempCar.position.set(-3.5, 0, 25.8);
      scene.scene.add(tempCar);
      scene.scene.updateMatrixWorld(true);
    } catch (e) {
      tempCar = null;
      console.warn('[sim3d] LPR 车辆材质预热加载失败（继续）', e?.message || e);
    }
    const warmStubIn = { nosePos: () => ({ x: -3.5, z: 25.8 }) };
    const f1 = await captureStopLineFrame('in', warmStubIn);
    details.first_capture = { capture_ms: f1.capture_ms, render_read_ms: f1.render_read_ms,
      encode_ms: f1.encode_ms, bytes: f1.bytes };
    if (tempCar) { scene.scene.remove(tempCar); tempCar = null; }
    // 3) 预热第二台相机（同 RT/canvas 路径）
    const warmStubOut = { nosePos: () => ({ x: 3.5, z: 33.7 }) };
    const f2 = await captureStopLineFrame('out', warmStubOut);
    details.second_capture = { capture_ms: f2.capture_ms, render_read_ms: f2.render_read_ms,
      encode_ms: f2.encode_ms, bytes: f2.bytes };
    details.total_ms = Math.round(performance.now() - started);
    console.info(`[sim3d] LPR 首帧预热完成 ${details.total_ms}ms（in ${f1.capture_ms}ms / out ${f2.capture_ms}ms）`);
  } catch (e) {
    details.error = String(e?.message || e);
    console.warn('[sim3d] LPR 首帧预热失败（保留正常抓拍）', e);
  } finally {
    if (tempCar && scene?.scene) scene.scene.remove(tempCar);
    lprWarmupDetails = details;
  }
  return details;
}

/** 串行化 LPR 截图（启动预热/首车预热/真实停止线抓拍不并发抢占 RT），真实抓拍等待预热完成。 */
function requestStopLineFrame(gate, vehicle) {
  const queuedAt = performance.now();
  const run = lprCaptureChain.then(async () => {
    try { await lprWarmupPromise; } catch (_) { /* 预热失败不阻断抓拍 */ }
    const t = performance.now();
    const frame = await captureStopLineFrame(gate, vehicle);
    lprCaptureStats.push({ gate, id: vehicle?.id || null, wait_ms: Math.round(t - queuedAt),
      capture_ms: frame.capture_ms, render_read_ms: frame.render_read_ms,
      encode_ms: frame.encode_ms, bytes: frame.bytes, at: Date.now() });
    if (lprCaptureStats.length > 30) lprCaptureStats.shift();
    return frame;
  });
  lprCaptureChain = run.catch(() => {});
  return run;
}

/** 第一辆车 spawn 后用**真实车型**再预热一次（车辆材质/车牌贴图首次渲染）。 */
function prewarmLprWithVehicle(vehicle) {
  if (!WARMUP_ENABLED || (PREVIEW && !WARMUP_TEST) || !vehicle || lprVehiclePrewarmDone) return;
  lprVehiclePrewarmDone = true;
  requestStopLineFrame('in', vehicle)
    .then((f) => console.info(`[sim3d] 首车 LPR 预热完成 ${f.capture_ms}ms（${vehicle.spec?.file || ''}）`))
    .catch((e) => console.warn('[sim3d] 首车 LPR 预热失败', e?.message || e));
}


// ----------------------------- HUD/仿真事件 -----------------------------
function onSimEvent(e) {
  switch (e.type) {
    case 'spawn':
      hud.event({ type: 'spawn', id: e.id, vid: e.vid, plate: scheduler?.vehicles?.get?.(e.id)?.meta?.plate || null });
      try { prewarmLprWithVehicle(scheduler?.vehicles?.get?.(e.id)); } catch (_) { /* ignore */ }
      break;
    case 'arrived': hud.event({ type: 'arrived', gate: e.gate, id: e.id }); break;
    case 'camera_frame':
      // 3D 闸口截图已送达后端：识别输入卡片先展示相机截图，等待 WS plate_recognized 补识别结果
      hud.setRecognitionInput({
        gate: e.gate, expected_plate: e.expected_plate || null, plate: '识别中…', conf: 0,
        image_url: e.image_url || null, image_path: e.image_path || null,
        image_source: '3D 闸口相机截图（仿真）', image_synthetic: false,
        camera_capture_ms: e.capture_ms, frame_size: e.width && e.height ? `${e.width}×${e.height}` : null,
        message: '闸口相机截图已上传，等待 HyperLPR3 识别结果…',
      }, getToken());
      hud.event({ type: 'camera_frame', gate: e.gate, width: e.width, height: e.height,
        capture_ms: e.capture_ms, image_url: e.image_url || e.image_path || null,
        message: '闸口相机截图已上传，等待识别' });
      break;
    case 'camera_frame_sent':
      hud.event({ type: 'camera_frame_sent', gate: e.gate, image_url: e.image_url || e.image_path || null });
      break;
    case 'assigned':
      hud.event({ type: 'space_assigned', space_no: e.space_no, id: e.id }); break;
    case 'parked':
      hud.event({ type: 'parked', plate: e.plate, id: e.id, space: e.space,
        entryGateAtSim: e.entryGateAtSim, parkedAtSim: e.parkedAtSim,
        entryTravel: e.entryTravel, exitTravelEst: e.exitTravelEst,
        scheduledExitAt: e.scheduledExitAt, autoExitAt: e.autoExitAt }); break;
    case 'passed': hud.event({ type: 'passed', gate: e.gate, id: e.id, plate: e.plate || null,
      gateAtSim: e.gateAtSim, entryGateAtSim: e.entryGateAtSim,
      gateToGateActual: e.gateToGateActual }); break;
    case 'left':
      hud.event({ type: 'left', plate: e.plate, id: e.id, vid: e.vid || null });
      try { scenario?.markVehicleLeft?.(e.vid || e.id || e.plate); } catch (_) { /* ignore */ }
      break;
    case 'exit_start': hud.event({ type: 'exit_start', plate: e.plate, id: e.id }); break;
    case 'auto_exit':
      hud.event({ type: 'auto_exit', plate: e.plate, vid: e.vid, reason: e.reason,
        entryTravel: e.entryTravel, exitTravelEst: e.exitTravelEst,
        scheduledExitAt: e.scheduledExitAt }); break;
  case 'entry_denied':
    hud.event({ type: 'entry_denied', plate: e.plate, vid: e.vid, reason: e.reason,
      mode: e.mode, no_record: e.no_record !== false });
    break;
  case 'entry_denied_departed':
    hud.event({ type: 'entry_denied_departed', plate: e.plate, reason: e.reason,
      mode: e.mode, no_record: true });
    break;
  case 'entry_authorized':
    hud.event({ type: 'entry_authorized', plate: e.plate, gate: 'in', mode: e.mode, space_no: e.space_no || null });
    break;
  case 'exit_authorized':
    hud.event({ type: 'exit_authorized', plate: e.plate, gate: 'out', mode: e.mode });
    break;
    case 'queue_hold': hud.event({ type: 'queue_hold', id: e.id, reason: e.reason }); break;
    case 'zone_blocked': hud.event({ type: 'zone_blocked', id: e.id, kind: e.zone }); break;
    case 'static_hit': hud.event({ type: 'static_hit', id: e.id, kind: e.kind }); break;
    case 'scn': hud.event({ type: 'scenario', message: e.msg }); break;
    case 'warn': hud.event({ type: 'warn', message: e.msg }); break;
    default:
      // 调度器未知事件也保留到事件列表，避免静默丢弃后端新增状态。
      if (e && e.type) hud.event({ ...e, type: String(e.type) });
      break;
  }
}

// ----------------------------- WebSocket 下行 -----------------------------
function onWsStatus(online) {
  hud.setConn(online);
}

let syncedSessionId = null;
function applySimulation(state) {
  if (!state) return;
  paused = !!state.paused;
  const smin = Number.isFinite(Number(state.speed_min)) ? Number(state.speed_min) : .25;
  const smax = Number.isFinite(Number(state.speed_max)) ? Number(state.speed_max) : 10;
  speed = Math.max(smin, Math.min(smax, Number(state.speed) || 1));
  const speedEl = document.getElementById('speed');
  const speedVal = document.getElementById('speed-val');
  if (speedEl) {
    speedEl.min = String(smin); speedEl.max = String(smax);
    if (document.activeElement !== speedEl) speedEl.value = String(speed);
  }
  if (speedVal) speedVal.textContent = `${speed.toFixed(2)}×`;
  simTime = Number(state.sim_time) || 0;
  lastT = performance.now();
  if (rig?.controls) rig.controls.enabled = true; // 暂停仍可拖拽视角（bug 修复）
  hud.setSimulation?.(simTime, paused, speed, state.business_time || null);
}

async function setPaused(value) {
  const state = previewMode ? {paused:!!value,speed,sim_time:simTime} :
    await Rest.simulationControl({paused:!!value});
  applySimulation(state);
  return paused;
}

async function setSpeed(value) {
  const next = Math.max(.25, Math.min(10, Number(value) || 1));
  const state = previewMode ? {paused,speed:next,sim_time:simTime} :
    await Rest.simulationControl({speed:next});
  applySimulation(state);
  return speed;
}

async function onSnapshot(snapshot) {
  applySimulation(snapshot.simulation);
  displays?.reset();
  let lastRecognition = null;
  for (const event of snapshot.display_events || []) {
    displays?.apply(event.data);
    if (event.data?.type === 'plate_recognized') lastRecognition = event.data;
  }
  if (lastRecognition) hud.setRecognitionInput(annotateRecognition(lastRecognition), getToken());
  const nextSessionId = snapshot.session?.id || null;
  if (syncedSessionId && nextSessionId !== syncedSessionId) {
    scheduler?.clearAll();
    scenario?.reset();
  }
  syncedSessionId = nextSessionId;
  const layout = snapshot.layout || {};
  if (scene && layout.version !== scene.layoutVersion) await reloadLayout();
  const free = { ...(layout.free_by_zone || {}), total: layout.free_total };
  hud.setFree(free, null);
  displays?.setFree?.(free);
  scene.setFreeBoard(layout.free_by_zone || {}, layout.free_total);
  scene.markers.resync(layout.spaces || []);
  for (const [gate, state] of Object.entries(snapshot.barriers || {})) {
    const g = scene.gates[gate];
    if (g) {
      g.openDuration = snapshot.config?.animation?.open_sec || 1.5;
      g.closeDuration = snapshot.config?.animation?.close_sec || 1.5;
      g.forceState(state.state, { hydration: true, arm_position: state.arm_position,
        passable: state.passable, manual: state.manual,
        authorization: state.authorization, keep_open: state.keep_open });
    }
  }
  await scheduler?.hydrate?.(snapshot, simApi?.ownerId);
  for (const payment of snapshot.paid_exits || []) scheduler?.onPaymentCompleted?.(payment);
}

function onWsMessage(m) {
  displays?.apply(m);
  // 兼容两种下发：业务事件直接带 type，或 v2 状态包装
  // {"type":"event","seq":...,"name":"DEMO_RESET","data":{...}}。
  if (String(m?.type || '').toLowerCase() === 'event' && m?.name) {
    m = { ...(m.data || {}), type: String(m.name), seq: m.seq, name: m.name };
  }
  // 兼容后端两种命名：snake_case（entry_denied）或事件类名（EntryDenied）。
  const wsType = String(m?.type || '').replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
  switch (wsType) {
    case 'welcome': break;
    case 'simulation_state': applySimulation(m); break;
    case 'plate_recognized':
      annotateRecognition(m);
      hud.setRecognitionInput(m, getToken());
      hud.popupRecognized(m);
      scheduler?.onRecognized?.(m);
      if (m.gate === 'in' && m.need_manual === false) {
        try { scenario?.markVehicleEntered?.(m.vehicle_id || m.plate); } catch (_) { /* ignore */ }
      }
      hud.event({ type: 'plate_recognized', gate: m.gate, plate: m.plate,
        expected_plate: m.expected_plate || null, conf: m.conf ?? m.overall_conf,
        plate_type: m.color, need_manual: m.need_manual,
        image_url: m.image_url || null, plate_crop_url: m.plate_crop_url || null,
        message: m.message || null });
      break;
    case 'pending_created':
      hud.event({ type: 'pending_created', gate: m.gate, pending_id: m.pending_id,
        source_plate: m.source_plate || '未识别', image_url: m.image_url || null });
      break;
    case 'pending_resolved':
      if (m.status === 'rejected' || m.status === 'expired') {
        hud.popup(`人工确认${m.status === 'expired' ? '超时' : '已拒绝'}，车辆继续等待`, 'warn', 6000);
      }
      scheduler?.onPendingResolved?.(m);
      hud.event({ type: 'pending_resolved', pending_id: m.pending_id, plate: m.plate || null,
        status: m.status, gate: m.gate || null, action: m.action || null });
      break;
    case 'entry_denied':
    case 'blacklist':
    case 'duplicate_entry': {
      const denied = { ...m, type: m.type || 'entry_denied', reason: m.reason || m.detail?.reason || m.kind || null };
      scheduler?.onEntryDenied?.(denied);
      hud.event({ type: 'entry_denied', plate: denied.plate || denied.source_plate || null,
        reason: denied.reason, code: denied.code || denied.type, gate: denied.gate || 'in',
        detail: denied.detail || null, message: denied.message || null });
      hud.popup(`⛔ 入口拒绝 ${denied.plate || ''}${denied.reason ? ' · ' + denied.reason : ''}`, 'warn', 7000);
      break;
    }
    case 'entry_authorized':
      scheduler?.onEntryAuthorized?.(m);
      hud.event({ type: 'entry_authorized', plate: m.plate || null, gate: m.gate || 'in',
        space_no: m.space_no || null, mode: m.mode || m.authorization || null });
      break;
    case 'exit_authorized':
      scheduler?.onExitAuthorized?.(m);
      hud.event({ type: 'exit_authorized', plate: m.plate || null, gate: m.gate || 'out',
        mode: m.mode || m.authorization || m.action || null, amount: m.amount_paid,
        record_id: m.record_id || null, message: m.message || m.reason || null });
      break;
    case 'manual_authorized':
      if (m.gate === 'in') {
        scheduler?.onEntryAuthorized?.(m);
        hud.event({ type: 'manual_authorized', plate: m.plate || null, gate: 'in',
          action: m.action || 'entry', record_id: m.record_id || null,
          space_no: m.space_no || null, message: m.reason || m.message || null });
      } else {
        scheduler?.onExitAuthorized?.(m);
        hud.event({ type: 'manual_authorized', plate: m.plate || null, gate: m.gate || 'out',
          action: m.action || 'exit', record_id: m.record_id || null,
          amount: m.amount_paid, space_no: m.space_no || null,
          message: m.reason || m.message || null });
      }
      break;
    case 'arrival_retry_requested':
      scheduler?.onArrivalRetryRequested?.(m);
      hud.event({ type: 'arrival_retry_requested', vehicle_id: m.vehicle_id || null,
        plate: m.plate || null, gate: m.gate || 'in', reason: m.reason || 'retry_failed',
        message: m.message || null });
      break;
    case 'error': {
      const err = normalizeError(m);
      hud.event({ type: 'error', code: err.code, message: err.message, plate: err.plate,
        detail: err.detail || null });
      break;
    }
    case 'demo_reset': {
      const payload = m.data || m.payload || m;
      scheduler?.clearAll?.();
      scenario?.reset?.();
      if (payload?.snapshot) {
        Promise.resolve(onSnapshot(payload.snapshot)).catch((e) =>
          hud.event({ type: 'error', code: 'demo_reset_snapshot', message: String(e?.message || e) }));
      }
      hud.event({ type: 'demo_reset', message: '演示已重置',
        detail: payload?.reset_report || null,
        closed_session: payload?.closed_session || null, new_session: payload?.new_session || null });
      break;
    }
    case 'payment_completed':
      scheduler?.onPaymentCompleted?.(m);
      hud.event({ type: 'payment_completed', gate: m.gate || 'out', plate: m.plate, amount: m.amount,
        record_id: m.record_id, method: m.method, image_url: m.exit_photo || null });
      break;
    case 'fee_quote':
      scheduler?.onFeeQuote?.(m);
      hud.popupFee(m);
      hud.event({ type: 'fee_quote', gate: m.gate || 'out', plate: m.plate, amount: m.amount,
        minutes: m.minutes, record_id: m.record_id, image_url: m.exit_photo || null });
      break;
    case 'barrier_cmd': {
      const g = scene.gates[m.gate];
      if (g) {
        g.command(m.cmd, m.reason, m.cmd_id, m.duration_sec, m.target);
        if (m.passable !== undefined) g.setPassable?.(m.passable, {
          manual: m.manual, authorization: m.authorization });
      }
      hud.event({ type: 'barrier_cmd', gate: m.gate, cmd: m.cmd, reason: m.reason,
        state: m.target, passable: m.passable, manual: m.manual,
        authorization: m.authorization });
      break;
    }
    case 'barrier_state': {
      const g = scene.gates[m.gate];
      // Lead 的 BarrierGate.forceState 只接受终态（OPEN/CLOSED/KEEP_OPEN），忽略 OPENING/CLOSING 瞬态
      if (g) g.forceState(m.state, { arm_position: m.arm_position, passable: m.passable,
        manual: m.manual, authorization: m.authorization, keep_open: m.keep_open });
      hud.event({ type: 'barrier_state', gate: m.gate, state: m.state,
        arm_position: m.arm_position, passable: m.passable, manual: m.manual,
        authorization: m.authorization });
      break;
    }
    case 'space_update':
      hud.setFree(m.free, m.recommend);
      scene.setFreeBoard(m.free || {}, m.free && m.free.total);
      scene.markers.setRecommend(m.recommend || null);
      break;
    case 'space_assigned':
      scheduler?.onSpaceAssigned?.(m);
      try { scenario?.markVehicleEntered?.(m.vehicle_id || m.plate); } catch (_) { /* ignore */ }
      scene.markers.setRecommend(null);
      break;
    case 'layout_changed':
      hud.log(`布局变更 v${m.version}: ${(m.diff && m.diff.summary) || ''}`);
      reloadLayout();
      break;
    case 'incident':
      hud.popup(`⚠ 异常 ${m.kind || ''}${m.gate ? '/' + m.gate : ''}`
        + `<br><small>${String(formatDetail(m.detail || m.reason || '')).replace(/[&<>"]/g, '')}</small>`, 'warn', 8000);
      hud.event({ type: 'incident', kind: m.kind, gate: m.gate, detail: m.detail,
        reason: m.detail?.reason || m.reason || null });
      break;
    case 'incident_resolved':
      hud.event({ type: 'incident_resolved', incident_id: m.incident_id,
        message: m.message || '异常已处理' });
      break;
    default: {
      // 未知 WS 类型也进入事件列表，禁止默认静默丢弃。
      if (!m || !m.type || m.type === 'welcome') break;
      hud.event({ ...m, type: String(m.type) });
      break;
    }
  }
}

async function resyncSpaces() {
  try {
    // 兜底超时：后端异常（如 DB 连接池耗尽/无响应）时不能阻塞首帧与启动流程
    const r = await Promise.race([
      Rest.spaces(),
      new Promise((_, reject) => setTimeout(() => reject(new Error('spaces 超时')), 8000)),
    ]);
    scene.markers.resync(r.items || []);
  } catch (_) { /* 忽略：非致命 */ }
}

/** 重置：清空剧本/手动车辆、道闸复位、仿真时钟归零（后端 force=true 由 resetDemo 调用）。 */
function resetLocalDemo() {
  scheduler?.clearAll?.();
  scenario?.reset?.();
  simTime = 0;
  lastT = performance.now();
  for (const gate of [scene?.gates?.in, scene?.gates?.out]) {
    try { gate?.forceState?.('CLOSED', { hydration: true }); } catch (_) { /* ignore */ }
  }
  hud.setSimulation?.(simTime, paused, speed);
  hud.setScenarioProgress?.(scenario?.progress?.() || null);
  if (paused) setPaused(false).catch(() => {});
  resyncSpaces();
}

let layoutReload = null, layoutReloadAgain = false;
async function reloadLayout() {
  if (PREVIEW) return;
  if (layoutReload) { layoutReloadAgain = true; return layoutReload; }
  layoutReload = performLayoutReload();
  try { await layoutReload; }
  finally { layoutReload = null; if (layoutReloadAgain) { layoutReloadAgain = false; await reloadLayout(); } }
}
async function performLayoutReload() {
  try {
    const layout = await Rest.getLayout();
    scene.applyLayout(layout);
    hud.setFree({...layout.free, total:layout.free_total}, null);
    displays?.setFree?.({...layout.free, total:layout.free_total});
    // PathNetwork 门面：优先 setSpaces（保留 planner/reservations），退 configure，最后旧签名
    if (paths) {
      if (typeof paths.setSpaces === 'function') paths.setSpaces(layout.spaces);
      else if (typeof paths.configure === 'function') {
        try { paths.configure({ manifest: manifest?.raw || manifest, spaces: layout.spaces }); }
        catch (_) { try { paths.configure(scene.sceneCfg, scene.gatesCfg); } catch (__) { /* ignore */ } }
      }
    }
    planner = paths?.planner || planner;
    scheduler?.refreshLayout();
    rig.updateSceneCfg(scene.sceneCfg);
    rig.setLprCameras?.(lprCameraList());
  } catch (e) {
    hud.log(`重拉布局失败 ${e}`, 'warn');
  }
}

// ----------------------------- 路径门面 -----------------------------
/**
 * 创建 PathNetwork（three-route 提供 v2 门面）。v2 构造签名：
 *   new PathNetwork({ manifest, spaces, sceneCfg, gatesCfg, options })
 * 兼容旧签名 new PathNetwork(sceneCfg, gatesCfg)（返回旧折线路径，供过渡期使用）。
 */
function createPaths(spaceList) {
  try {
    const p = new PathNetwork({
      manifest: manifest?.raw || manifest,
      spaces: spaceList,
      sceneCfg: scene.sceneCfg,
      gatesCfg: scene.gatesCfg,
    });
    const hasV2 = !!(p && (p.manifest || p.planner || p.usingPlanner ||
      typeof p.planEntry === 'function' || typeof p.planEntryRich === 'function'));
    if (hasV2) return p;
    console.warn('[main] PathNetwork 未返回 v2 门面（planner/reservations），回退旧签名');
  } catch (e) {
    console.warn('[main] PathNetwork v2 构造失败，回退旧签名：', e);
  }
  return new PathNetwork(scene.sceneCfg, scene.gatesCfg);
}

// ----------------------------- 预览模式 -----------------------------
function makePreviewApi() {
  const noop = async () => { throw new Error('preview 模式无后端'); };
  return {
    ack: () => {},
    connect: () => {},
    close: () => {},
    scenario: async () => ({ name: 'preview', steps: [] }),
    ensureSession: async () => false,
    pay: noop, camInject: noop, camRecover: noop,
    manualRegister: noop, manualExit: noop, manualOpen: noop,
    spaces: async () => ({ items: [] }),
    overview: async () => ({}),
    layout: async () => ({}),
  };
}

function buildPreviewLayout(m) {
  const spaces = m.defaultSpaces();
  const totals = m.zoneTotals();
  const free = { total: spaces.length, ...totals };
  return {
    version: m.manifestVersion,
    spaces,
    zones: totals,
    free,
    free_total: spaces.length,
    scene: m.sceneCfg(),
    gates: m.gatesCfg(),
  };
}

/** 动态补齐相机预设下拉项（plazaN/plazaS 等），不改 index.html。 */
function ensureCameraOptions() {
  const sel = document.getElementById('camera-preset');
  if (!sel) return;
  for (const p of CameraRig.presetList()) {
    if (sel.querySelector(`option[value="${p.id}"]`)) continue;
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    sel.appendChild(opt);
  }
}

// ----------------------------- 调试契约 -----------------------------
function num(v) { return Number.isFinite(Number(v)) ? Number(v) : null; }
function v2(v) {
  if (!v) return null;
  return { x: num(v.x) ?? 0, z: num(v.z) ?? 0 };
}

/** 收集单辆车对 dump 的可 JSON 化快照（对 three-scheduler 新字段做兼容读取）。 */
function vehicleSnapshot(v) {
  if (!v) return null;
  let pos = null;
  try { pos = v2(v.pos); } catch (_) { pos = null; }
  let theta = null;
  try { theta = typeof v.heading === 'function' ? num(v.heading()) : num(v.theta); } catch (_) { theta = null; }
  let nose = null;
  try {
    if (typeof v.nosePos === 'function') nose = v2(v.nosePos());
  } catch (_) { nose = null; }
  if (!nose && pos && theta != null) {
    // 车头本地 -Z，世界前向 = (-sinθ, -cosθ)
    const half = Number(v.dims?.len ?? v.dims?.length ?? 4.6) / 2;
    nose = { x: pos.x - Math.sin(theta) * half, z: pos.z - Math.cos(theta) * half };
  }
  let reservedZones = [];
  try {
    const rz = v.reservedZones;
    if (rz instanceof Set) reservedZones = Array.from(rz);
    else if (Array.isArray(rz)) reservedZones = rz.slice();
    else if (rz && typeof rz.values === 'function') reservedZones = Array.from(rz.values());
  } catch (_) { reservedZones = []; }
  let legKind = null;
  try { legKind = typeof v.legKind === 'function' ? v.legKind() : (v.legKind ?? null); } catch (_) { legKind = null; }
  return {
    id: v.id ?? null,
    vid: v.vid ?? v.meta?.vid ?? v.id ?? null,
    state: v.state ?? null,
    plate: v.meta?.plate ?? null,
    pos,
    nose,
    theta,
    speed: num(v.speed),
    legKind,
    reservedZones,
    spaceNo: v.meta?.spaceNo ?? v.meta?.space_no ?? null,
    reverseLeg: !!v.reverseLeg,
    duration: v.meta?.duration ?? null,
    entryGateAtSim: v.meta?.entryGateAtSim ?? null,
    parkedAtSim: v.meta?.parkedAtSim ?? null,
    entryTravel: v.meta?.entryTravel ?? null,
    exitTravelEst: v.meta?.exitTravelEst ?? null,
    scheduledExitAt: v.meta?.scheduledExitAt ?? null,
    autoExitAt: v.meta?.autoExitAt ?? null,
    exitGateAtSim: v.meta?.exitGateAtSim ?? null,
    gateToGateActual: v.meta?.gateToGateActual ?? null,
    dims: v.dims ? { len: v.dims.len, wid: v.dims.wid, hgt: v.dims.hgt } : null,
    visualLen: Number(v.visualLen) || null,
    visualWid: Number(v.visualWid) || null,
  };
}

function gatherVehicles() {
  const out = [];
  try {
    const map = scheduler?.vehicles;
    if (map && typeof map.forEach === 'function') {
      map.forEach((v) => { const s = vehicleSnapshot(v); if (s) out.push(s); });
    } else if (Array.isArray(map)) {
      for (const v of map) { const s = vehicleSnapshot(v); if (s) out.push(s); }
    }
  } catch (e) { console.warn('[dump] vehicles failed', e); }
  return out;
}

function gatherBarriers() {
  const out = {};
  try {
    for (const [id, g] of Object.entries(scene?.gates || {})) {
      out[id] = {
        state: g.state ?? null,
        isOpen: !!g.isOpen,
        passable: !!g.isPassable,
        manual: !!g.manual,
        authorization: g.authorization ?? null,
        armAngleDeg: typeof g.armAngleDeg === 'function' ? num(g.armAngleDeg()) : null,
      };
    }
  } catch (_) { /* ignore */ }
  return out;
}

function gatherZones() {
  try {
    const rt = scheduler?.reservations || paths?.reservations;
    if (rt && typeof rt.snapshot === 'function') return rt.snapshot();
  } catch (_) { /* ignore */ }
  return {};
}

function dump() {
  let counts = null;
  try { counts = typeof scheduler?.counts === 'function' ? scheduler.counts() : null; } catch (_) { counts = null; }
  const m = manifest;
  return {
    simTime: Number(simTime.toFixed(3)),
    paused: !!paused,
    counts,
    traffic: scheduler?.debugSnapshot?.() || null,
    barriers: gatherBarriers(),
    vehicles: gatherVehicles(),
    zones: gatherZones(),
    route: {
      roadGraphVersion: m ? (m.roadGraphVersion ?? m.raw?.road_graph_version ?? null) : null,
      manifestHash: m ? (m.manifestHash || m.raw?.manifest_hash || null) : null,
      source: m ? (m.sourceUrl || null) : null,
    },
    preview: previewMode,
    scene: (() => { try { return scene?.stats?.() ?? null; } catch (_) { return null; } })(),
  };
}

function installDebugHook() {
  if (!window.__SIM__) {
    // 占位：确保页面早期（scene 构造后、装配完成前）也有可用的调试入口
    window.__SIM__ = { version: 'route-v2', dump: () => ({ ready: false, error: 'scene not assembled yet' }) };
  }
  window.__SIM__ = {
    version: 'route-v2',
    preview: previewMode,
    manifest,
    scene,
    rig,
    paths,
    planner,
    scheduler,
    scenario,
    simApi,
    hud,
    panels,
    dump,
    flags: {
      preview: previewMode,
      shellEnabled: SHELL_ENABLED,
      shellLoaded: !!scene?.shellLoaded,
      shellFallback: !!scene?.shellFallback,
      debugColliders: DEBUG_COLLIDERS,
    },
    controls: {
      setPaused(v) { return setPaused(v); },
      isPaused() { return paused; },
      step(dt = 0.05) {
        if (!previewMode) throw new Error("单步仅支持本地预览");
        let remaining = Math.min(5,Math.max(0.001, Number(dt) || 0.05));
        while(remaining>1e-9) {
          const sdt=Math.min(.05,remaining);
          simTime += sdt;
          scheduler?.update?.(sdt);
          scenario?.update?.(sdt);
          scene?.update?.(sdt);
          remaining-=sdt;
        }
        return simTime;
      },
      setSpeed(v) { return setSpeed(v); },
      getSpeed() { return speed; },
      cameraPreset(name) { rig?.setPreset(name); return rig?.currentPreset; },
      lprCameras: lprCameraList(),
      async captureLprFrame(gate = 'in', vehicleId = null) {
        let v = vehicleId ? scheduler?.vehicles?.get?.(vehicleId) : null;
        if (!v && scheduler?.vehicles?.values) {
          v = [...scheduler.vehicles.values()].find((x) => x && !x.remote) || null;
        }
        return captureStopLineFrame(gate, v);
      },
      /** C：测量一次真实抓拍（会等待启动预热；返回全链路耗时，不返回 blob）。 */
      async benchmarkLprFrame(gate = 'in', vehicleId = null) {
        let v = vehicleId ? scheduler?.vehicles?.get?.(vehicleId) : null;
        if (!v && scheduler?.vehicles?.values) {
          v = [...scheduler.vehicles.values()].find((x) => x && !x.remote) || null;
        }
        const t = performance.now();
        const f = await requestStopLineFrame(gate, v);
        return { gate, vehicle: v?.vid || null, total_ms: Math.round(performance.now() - t),
          capture_ms: f.capture_ms, render_read_ms: f.render_read_ms, encode_ms: f.encode_ms,
          bytes: f.bytes, warmup: lprWarmupDetails };
      },
      lprStats() { return { warmup: lprWarmupDetails, captures: lprCaptureStats.slice(-20) }; },
      debugColliders(on = true) { return scene?.setDebugColliders?.(on); },
      sync() { return { sessionId: simApi?.rt?.sessionId || null,
        lastSeq: simApi?.rt?.lastSeq ?? null }; },
    },
  };
  window.__SIM__.dump = dump;
  return window.__SIM__;
}

// ----------------------------- 动画循环 -----------------------------
function frame(now) {
  requestAnimationFrame(frame);
  if (!lastT) lastT = now;
  const frameElapsed = Math.max(0, (now - lastT) / 1000);
  const dtReal = Math.min(0.05, frameElapsed);
  lastT = now;
  if (!paused) {
    let remaining = Math.min(5, frameElapsed) * speed;
    while (remaining > 1e-8) {
      const sdt = Math.min(.05, remaining);
      simTime += sdt;
      scheduler?.update?.(sdt);
      scenario?.update?.(sdt);
      scene.update(sdt);
      rig.update(sdt);
      remaining -= sdt;
    }
  }
  hud.setSimulation?.(simTime, paused, speed);
  try {
    hud.setScenarioProgress?.((scenario && scenario.steps?.length) ? scenario.progress?.() : null);
  } catch (_) { /* 进度面板不应影响渲染 */ }
  if (!lprCapturing) scene.render(rig.camera);
  statAccum += dtReal;
  if (statAccum > 0.5) {
    statAccum = 0;
    try { hud.setStat(scheduler?.counts?.() || { total: 0, onLot: 0, moving: 0 }); } catch (_) { /* ignore */ }
  }
}

// ----------------------------- 启动 -----------------------------
async function boot() {
  hud.setConn(false);


  scene = new ParkingScene(canvas, { debugColliders: DEBUG_COLLIDERS });
  scene.assets.onFailure = () => showLoadError('部分场景资源加载失败，请重试加载。');
  resize();
  window.addEventListener('resize', resize);
  // 关键：先安装调试钩子（scene 已可用），即使后续 manifest/布局/壳装配失败，
  // demo-qa / three-scheduler 仍能通过 window.__SIM__ 定位问题（dump 对缺失依赖做了兜底）。
  installDebugHook();
  if (!SHELL_ENABLED) hud.log('shell 已通过 ?shell=0 强制关闭，使用程序化地面', 'warn');

  // 1) manifest（单一事实源）
  try {
    manifest = await SceneManifest.load();
    const integ = manifest.integrity();
    bootLog(`场景 manifest v${integ.version} · ${manifest.hashShort} · ` +
      `车位${integ.spaces} 节点${integ.nodes} 边${integ.edges} 冲突区${integ.conflictZones} ` +
      `静态碰撞体${integ.staticColliders} · ${integ.sourceUrl}`);
    if (integ.hashVerified === false) hud.log('manifest hash 与期望值不一致（继续加载）', 'warn');
    else if (integ.hashVerified === true) bootLog('manifest hash 校验通过', 'ok');
    if (!integ.hashFormatOk) hud.log('manifest hash 格式异常（继续加载）', 'warn');
    if (integ.spaces !== 80) hud.log(`manifest 车位数=${integ.spaces}（期望 80）`, 'warn');
  } catch (e) {
    throw new Error('场地布局加载失败，请检查资源后重试。', {cause:e});
  }

  previewMode = PREVIEW;
  simApi = previewMode ? makePreviewApi() : new SimApi(onWsMessage, onWsStatus, onSnapshot);
  const authed = !previewMode && await simApi.ensureSession();
  hud.startClock(previewMode ? null : () => Rest.overview());

  // 2) 布局 / 车位
  let layout;
  if (PREVIEW) {
    if (!manifest) throw new Error('preview 模式需要 lot_v2.json（manifest 加载失败）');
    layout = buildPreviewLayout(manifest);
    previewMode = true;
    bootLog(`预览模式：本地生成 ${layout.spaces.length} 个车位（无后端连接）`, 'ok');
  } else {
    try {
      layout = await Rest.getLayout();
      // manifest hash 交叉校验（后端若透传 manifest_hash / 或 /api/layout.scene.manifest_hash）
      const backendHash = layout?.manifest_hash || layout?.manifest?.manifest_hash ||
        layout?.scene?.manifest_hash || null;
      if (manifest && backendHash) {
        manifest.hashVerified = String(backendHash).toLowerCase() === String(manifest.manifestHash).toLowerCase();
        bootLog(manifest.hashVerified ? 'manifest 与后端 hash 一致' : `manifest hash 与后端不一致：${backendHash}`,
          manifest.hashVerified ? 'ok' : 'warn');
      } else if (manifest && layout?.road_graph_version && manifest.roadGraphVersion &&
        Number(layout.road_graph_version) !== Number(manifest.roadGraphVersion)) {
        hud.log(`后端路网版本 ${layout.road_graph_version} ≠ manifest ${manifest.roadGraphVersion}`, 'warn');
      }
    } catch (e) {
      hud.log(`无法获取布局(${e})，使用 manifest 缺省车位`, 'warn');
      layout = manifest ? buildPreviewLayout(manifest) : {
        version: 0, spaces: [], free: {}, free_total: 0, scene: {}, gates: {},
      };
    }
  }

  const ackCb = (gate, state, cmdId, armPosition = null) => simApi.ack(gate, state, cmdId, armPosition);
  await scene.buildFromLayout(layout, ackCb, {
    manifest,
    preview: previewMode,
    shellEnabled: SHELL_ENABLED,
  });
  if (scene.shellLoaded) bootLog('静态场地壳 lot_shell.glb 已加载', 'ok');
  else hud.log('lot_shell.glb 未就绪，已回退程序化地面（?shell=0 可强制回退）', 'warn');
  // MIT generator 真实样式车牌贴图索引（web/sim3d/assets/plates/index.json）+ 预加载
  try {
    await scene.assets?.loadPlateManifest?.();
    const n = await scene.assets?.preloadPlates?.();
    bootLog(`车牌贴图预加载 ${n || 0} 张（MIT generator 真实样式，Plate_F/Plate_R 不再用 Canvas）`, 'ok');
  } catch (e) { hud.log(`车牌贴图索引加载失败，未知车牌回退 Canvas：${e?.message || e}`, 'warn'); }

  displays = new GateDisplay((gate, lines, opts) => scene.setGateScreen(gate, lines, opts),
    { loadImage: loadGateScreenPhoto });
  displays.reset();

  const lprCameras = lprCameraList();
  if (lprCameras.length) bootLog(`LPR 闸口相机：入口/出口共 ${lprCameras.length} 台（manifest.props.lpr_cameras）`, 'ok');
  else hud.log('manifest.props.lpr_cameras 未就绪，使用 idea2 标定兜底相机位', 'warn');
  rig = new CameraRig(scene.renderer, scene.sceneCfg, { lprCameras });
  resize();
  ensureCameraOptions();

  // 3) 路网门面（three-route）+ 调度器（three-scheduler）
  paths = createPaths(layout.spaces);
  planner = paths?.planner || null;
  if (planner) bootLog(`路径规划就绪：${typeof planner.entryStopZ === 'function' ? 'RoutePlanner v2' : 'planner'}`);
  scheduler = new TrafficScheduler({
    scene, paths, planner, manifest: manifest?.raw || manifest, onEvent: onSimEvent,
    options: {seed:Number(params.get("seed") || 20261020),ownerId:simApi?.ownerId,
      claimDispatcher:PREVIEW ? null : ()=>simApi.claimDispatcher(),
      // 停止线：优先 3D 闸口相机截图 → POST /sim/events/arrived-frame；不可用时自动回退旧图库模式
      cameraFrame: PREVIEW ? null : requestStopLineFrame,
      cameraRecognition: !PREVIEW && params.get('camrec') !== '0'},
  });
  scenario = new ScenarioPlayer({
    scheduler, onEvent: onSimEvent,
    // 新 vehicles 剧本：load 阶段预取每车后端渲染车牌图（/api/sim/plate-image）。
    plateLoader: (!PREVIEW && simApi?.plateImage)
      ? (plate, plateType) => loadBackendPlateImage({ plate, plateType, simApi })
      : null,
  });
  // eslint-disable-next-line no-new
  new ControlPanel({
    scenario, scheduler, rig, simApi, hud,
    setSpeed, setPaused, getPaused: () => paused, getSpeed: () => speed,
    onReset: resetLocalDemo,
  });

  // 余位屏/HUD：/api/layout.free 是“分区余位”字典，总数在 free_total；
  // HudOverlay.setFree(free, recommend) 需要 free.total（否则总数显示 --），这里补齐。
  const freeForHud = {
    ...(layout.free || {}),
    total: (layout.free_total ?? layout.free?.total ?? layout.free?.free_total ?? null),
  };
  hud.setFree(freeForHud, null);
  displays?.setFree?.(freeForHud);
  scene.setFreeBoard(layout.free || {}, layout.free_total);
  installDebugHook();
  // 场景已装配完成：先隐藏 loading 并启动渲染循环，再继续网络步骤（WS/剧本/会话）。
  // 这样即使后端异常/慢响应，也不会出现“场景已建好但一直转圈、renderer.info 为空”的情况。
  hud.hideLoading();
  requestAnimationFrame(frame);
  // C：非预览模式启动 LPR 首帧预热（异步，不阻塞场景/网络初始化）
  if (!PREVIEW || WARMUP_TEST) {
    if (WARMUP_ENABLED) lprWarmupPromise = warmupLprPipeline();
  }

  if (previewMode) {
    // 本地预览：不连 WS / 不拉剧本；HUD 顶部显示“本地预览”
    const conn = document.getElementById('conn');
    if (conn) { conn.textContent = '● 本地预览'; conn.className = 'pill pill-on'; }
    bootLog('预览就绪：?debug=colliders 可显示静态碰撞体，__SIM__.controls.cameraPreset("plazaN") 可切机位', 'ok');
    return;
  }

  await resyncSpaces();

  // 演示终端会话（供缴费/摄像头注入等管理动作；失败静默降级）
  bootLog(authed ? '演示终端已登录（管理动作可用）' : '演示终端未登录（管理动作受限）',
    authed ? 'ok' : 'warn');
  if (authed) {
    try { await simApi.connect(); }
    catch (e) { hud.log(`状态同步失败：${e.message || e}`, 'warn'); }
    let reporting = false;
    setInterval(async () => {
      if (reporting) return;
      reporting = true;
      try {
        await simApi.reportRuntime(scheduler.runtimeSnapshot(),scheduler.schedulerSnapshot());
        const snapshot = await Rest.snapshot();
        if (snapshot.session?.id === simApi.rt.sessionId) {
          await scheduler.syncRemoteRuntime(snapshot.vehicle_runtime, simApi.ownerId);
        }
      }
      catch (e) { console.warn('[sim3d] 运行态上报失败', e); }
      finally { reporting = false; }
    }, 1500);
  }

  try {
    const scn = await simApi.scenario();
    await scenario.load(scn);
    const format = Array.isArray(scn?.vehicles) ? 'vehicles' : 'steps';
    hud.setScenarioProgress?.(scenario.progress?.());
    if (scenario.preloadError) {
      hud.event({ type: 'warn', message: scenario.preloadError, image_url: null });
      hud.setScenarioProgress?.({ ...scenario.progress(), preloadError: scenario.preloadError });
    }
    hud.event({ type: 'scenario', message: `剧本已加载：${scenario.name}`,
      duration: scenario.duration, count: scenario.totalVehicles, format,
      image_url: null });
  } catch (e) {
    hud.log(`剧本加载失败 ${e}`, 'warn');
    await scenario.load({ name: 'empty', steps: [] });
    hud.setScenarioProgress?.(null);
  }

  // A refreshed client waits for an explicit play command; it must not replay a live session.
}

function showLoadError(message) {
  const panel = document.getElementById('load-error');
  panel.hidden = false; panel.querySelector('span').textContent = message;
}
document.getElementById('btn-retry-load').onclick = () => location.reload();

boot().catch((e) => {
  showLoadError('场景加载失败，请重试加载。');
  console.error('[sim3d] 启动失败', e);
  hud.log(`启动失败: ${e.message || e}`, 'warn');
  hud.hideLoading();
  // 即使启动失败也把可用的调试钩子挂上（便于 demo-qa 定位）
  try { if (scene) installDebugHook(); } catch (_) { /* ignore */ }
});


