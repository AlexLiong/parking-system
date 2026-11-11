// 手动派车策略（纯函数，便于 Node 测试）：车牌校验/类型映射/后端 dispatch-plan 归一化。
// 约定后端契约：POST /api/sim/dispatch-plan 返回 plan，至少含
// plate/plate_type/image_url/vehicle_kind/duration（允许包在 plan/data/result 内）。

export const PLATE_TYPES = {
  blue: { label: '蓝牌普通', family: 'blue', color: 'blue', size: 'small' },
  green: { label: '小型新能源', family: 'green', color: 'green', size: 'small' },
  green_large: { label: '大型新能源', family: 'green', color: 'green', size: 'large' },
  yellow: { label: '黄牌大型', family: 'blue', color: 'yellow', size: 'large' },
};

const TYPE_ALIASES = {
  blue: 'blue', '蓝牌': 'blue', '蓝牌普通': 'blue', fuel: 'blue', normal: 'blue',
  green: 'green', '绿牌': 'green', '小型新能源': 'green', ev: 'green', new_energy: 'green',
  green_large: 'green_large', '大型新能源': 'green_large', large_green: 'green_large',
  new_energy_large: 'green_large', ev_large: 'green_large',
  yellow: 'yellow', '黄牌': 'yellow', '黄牌大型': 'yellow', large: 'yellow',
};

const KIND_FAMILY = {
  sedan: 'sedan', saloon: 'sedan', car: 'sedan', small: 'sedan',
  hatch: 'hatch', hatchback: 'hatch',
  suv: 'suv', mpv: 'suv',
  van: 'van', truck: 'van', lorry: 'van', large: 'van', big: 'van',
};

const MODELS = {
  blue: {
    sedan: ['car_sedan_standard_blue.glb', 'car_sedan_standard_white.glb'],
    hatch: ['car_hatchback_city_blue.glb'],
    suv: ['car_suv_urban_white.glb', 'car_suv_urban_black.glb'],
    van: ['car_van_passenger_white.glb'],
  },
  green: {
    sedan: ['car_nev_sedan_white.glb', 'car_nev_sport_blue.glb'],
    hatch: ['car_nev_hatch_green.glb'],
    suv: ['car_nev_suv_white.glb'],
    van: ['car_nev_van_blue.glb'],
  },
};

/** 去掉车牌分隔符并转大写。 */
export function normalizePlate(raw) {
  return String(raw == null ? '' : raw).replace(/[·.\-\s]/g, '').toUpperCase();
}

/** 中国车牌 7/8 位（含新能源 8 位）。返回 {ok, plate, error}。 */
export function validatePlate(raw) {
  const plate = normalizePlate(raw);
  if (!plate) return { ok: false, plate, error: '请输入车牌' };
  if (plate.length !== 7 && plate.length !== 8) {
    return { ok: false, plate, error: '车牌需为 7 位或 8 位（新能源）' };
  }
  if (!/^[\u4e00-\u9fa5][A-Z][A-Z0-9]{5,6}$/.test(plate)) {
    return { ok: false, plate, error: '车牌格式不正确（示例：湘N7D071 / 沪UD76840）' };
  }
  return { ok: true, plate, error: '' };
}

export function normalizePlateType(raw, fallback = 'blue') {
  const s = String(raw == null ? '' : raw).trim().toLowerCase();
  return TYPE_ALIASES[s] || TYPE_ALIASES[String(raw || '').trim()] || fallback;
}

export function plateTypeToKind(type) {
  return PLATE_TYPES[type]?.family || 'blue';
}

/** 后端返回的 vehicle_kind（或类型）映射到本地 GLB。 */
export function vehicleModelFor(type, vehicleKind = null, seq = 0) {
  const t = normalizePlateType(type);
  const family = KIND_FAMILY[String(vehicleKind || '').toLowerCase()]
    || (t === 'green_large' || t === 'yellow' ? 'van' : (t === 'green' ? 'sedan' : 'sedan'));
  const bucket = MODELS[plateTypeToKind(t)] || MODELS.blue;
  const list = bucket[family] || bucket.sedan;
  return list[Math.abs(Number(seq) || 0) % list.length];
}

/** 停车时长统一为仿真秒；unit 支持 sec/second/秒、min/minute/分钟。 */
export function durationToSeconds(value, unit = 'sec') {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return 0;
  const u = String(unit || 'sec').toLowerCase();
  return Math.round((u.startsWith('min') || u === '分钟' ? n * 60 : n) * 1000) / 1000;
}

function num(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function first(obj, keys) {
  for (const k of keys) {
    const v = k.split('.').reduce((a, key) => (a == null ? a : a[key]), obj);
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return null;
}

/**
 * 归一化 dispatch-plan 响应/请求；兼容 `{plan}`, `{data}`, 直接对象。
 * @returns {{plate:string, plateType:string, imageUrl:string, vehicleKind:string,
 *            durationSec:number, kind:string, color:string, model:string,
 *            vehicleId:string|null, raw:object}}
 */
export function normalizeDispatchPlan(resp, request = {}) {
  const wrapper = resp && typeof resp === 'object' ? (resp.plan || resp.data || resp.result || resp) : {};
  const plate = normalizePlate(first(wrapper, ['plate', 'plate_no', 'vehicle_plate']) || request.plate);
  const plateType = normalizePlateType(
    first(wrapper, ['plate_type', 'plateType', 'color', 'plate_color']) || request.plateType);
  const vehicleKind = String(first(wrapper, ['vehicle_kind', 'vehicleKind', 'kind', 'vehicle_type']) || '')
    .toLowerCase() || (plateType === 'green_large' || plateType === 'yellow' ? 'van' : 'sedan');
  const imageUrl = String(first(wrapper, ['image_url', 'imageUrl', 'plate_image_url', 'plate_image', 'image.url'])
    || request.imageUrl || '');
  const durationRaw = first(wrapper, ['duration', 'duration_sec', 'duration_seconds', 'stay_duration']);
  const durationUnit = String(first(wrapper, ['duration_unit', 'durationUnit']) || request.durationUnit || 'sec');
  let durationSec = num(first(wrapper, ['duration_sec', 'duration_seconds']));
  if (durationSec == null && durationRaw != null) durationSec = durationToSeconds(durationRaw, durationUnit);
  if (durationSec == null) durationSec = Number(request.durationSec) || 0;
  const vehicleId = first(wrapper, ['vehicle_id', 'vehicleId', 'vid', 'id']) || null;
  const color = PLATE_TYPES[plateType]?.color || 'blue';
  return {
    plate, plateType, imageUrl, vehicleKind,
    durationSec: Math.max(0, Number(durationSec) || 0),
    kind: plateTypeToKind(plateType),
    color,
    model: vehicleModelFor(plateType, vehicleKind, request.seq || 0),
    vehicleId: vehicleId ? String(vehicleId) : null,
    raw: wrapper,
  };
}

/** 给 UI 的车型中文名（仅用于展示）。 */
export function vehicleKindLabel(kind) {
  const k = String(kind || '').toLowerCase();
  if (k === 'hatch') return '掀背车';
  if (k === 'suv') return 'SUV';
  if (k === 'van' || k === 'truck') return '大型车';
  return '轿车';
}

export const KNOWN_MODELS = Object.values(MODELS).flatMap((x) => Object.values(x).flat());
