// 演示剧本纯数据模型：不依赖 DOM，便于 Node 单测与后端契约兼容。
export const PLATE_TYPES = [
  { value: 'blue', label: '蓝牌' },
  { value: 'green', label: '绿牌（新能源）' },
  { value: 'green_large', label: '绿牌大车' },
  { value: 'yellow', label: '黄牌' },
];
export const TYPE_VALUES = PLATE_TYPES.map((x) => x.value);

export function emptyVehicle(index = 0) {
  return { vid: `E${index + 1}`, t: 0, plate: '', plate_type: 'blue', duration: 20 };
}

/** 稳定按 t 排序（同 t 保持列表顺序，作为播放顺序）。 */
export function sortVehicles(vehicles) {
  return [...vehicles].sort((a, b) => Number(a.t || 0) - Number(b.t || 0));
}

/** 时间轴展示：t 为唯一时间轴；无显式 pay 时按出场时刻缴费。 */
export function expectedTimes(vehicle) {
  const entry = Math.max(0, Number(vehicle?.t) || 0);
  const duration = Math.max(0, Number(vehicle?.duration) || 0);
  return { entry, exit: entry + duration, pay: entry + duration };
}

export function normalizeScenario(raw) {
  const source = raw && typeof raw === 'object'
    ? (raw.scenario || raw.data || raw)
    : {};
  const vehicles = Array.isArray(source.vehicles) ? source.vehicles : [];
  return {
    name: String(source.name ?? '默认演示剧本'),
    description: String(source.description ?? ''),
    seed: Number.isFinite(Number(source.seed)) ? Number(source.seed) : 1,
    vehicles: sortVehicles(vehicles.map((v, i) => ({
      vid: String(v?.vid ?? v?.vehicle_id ?? `E${i + 1}`),
      t: Number.isFinite(Number(v?.t)) ? Number(v.t) : 0,
      plate: String(v?.plate ?? ''),
      plate_type: TYPE_VALUES.includes(String(v?.plate_type || v?.type)) ? String(v.plate_type || v.type) : 'blue',
      duration: Number.isFinite(Number(v?.duration)) ? Number(v.duration) : 20,
    }))),
  };
}

export function validateScenario(s) {
  const name = String(s.name || '').trim();
  if (!name) return { message: '剧本名称不能为空', field: 'name' };
  if (!Number.isInteger(Number(s.seed)) || Number(s.seed) < 0) {
    return { message: '随机种子 seed 必须是 ≥0 的整数', field: 'seed' };
  }
  if (!s.vehicles.length) return { message: '至少需要一辆演示车辆', field: 'vehicles' };
  if (s.vehicles.length > 50) return { message: '最多 50 辆演示车辆', field: 'vehicles' };
  const seen = new Set();
  const plateWindows = new Map();
  for (let i = 0; i < s.vehicles.length; i += 1) {
    const v = s.vehicles[i];
    const vid = String(v.vid || '').trim();
    const plate = String(v.plate || '').trim().toUpperCase();
    if (!vid) return { message: `第 ${i + 1} 行 vid 不能为空`, field: `vid-${i}` };
    if (seen.has(vid)) return { message: `第 ${i + 1} 行 vid 重复：${vid}`, field: `vid-${i}` };
    seen.add(vid);
    if (!Number.isFinite(Number(v.t)) || Number(v.t) < 0) {
      return { message: `第 ${i + 1} 行入场时间必须 ≥0`, field: `t-${i}` };
    }
    if (!Number.isFinite(Number(v.duration)) || Number(v.duration) <= 0) {
      return { message: `第 ${i + 1} 行停车时长必须 >0`, field: `duration-${i}` };
    }
    if (!TYPE_VALUES.includes(v.plate_type)) return { message: `第 ${i + 1} 行车牌类型无效`, field: `type-${i}` };
    if (!/^[\u4e00-\u9fa5][A-Z][A-Z0-9]{5,6}$/.test(plate) || ![7, 8].includes(plate.length)) {
      return { message: `第 ${i + 1} 行车牌需为 7/8 位中文车牌（如 湘N7D071）`, field: `plate-${i}` };
    }
    const expectedLength = (v.plate_type === 'green' || v.plate_type === 'green_large') ? 8 : 7;
    if (plate.length !== expectedLength) {
      return {
        message: `第 ${i + 1} 行${v.plate_type} 车牌长度应为 ${expectedLength}，实际 ${plate.length}`,
        field: `plate-${i}`,
      };
    }
    const start = Number(v.t);
    const end = start + Number(v.duration);
    const windows = plateWindows.get(plate) || [];
    const clash = windows.find((w) => start < w.end && w.start < end);
    if (clash) {
      return {
        message: `第 ${i + 1} 行与第 ${clash.index + 1} 行车牌 ${plate} 时间窗重叠（${start}–${end}s / ${clash.start}–${clash.end}s）`,
        field: `plate-${i}`,
      };
    }
    windows.push({ index: i, start, end });
    plateWindows.set(plate, windows);
  }
  return null;
}
