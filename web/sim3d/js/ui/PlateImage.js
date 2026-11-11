// 后端车牌图加载（手动派车 / 新剧本 vehicles 共用）：
// /api/sim/plate-image 返回 /media/...；/media 受鉴权保护，必须 token fetch → ImageBitmap。
import { getToken } from '../api.js';
import { normalizeDispatchPlan } from './DispatchPolicy.js';

export async function loadAuthImage(url) {
  if (!url) return null;
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(url, { headers, cache: 'no-store' });
  if (!res.ok) throw new Error(`车牌图片 HTTP ${res.status}`);
  const blob = await res.blob();
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);
  return await new Promise((resolve, reject) => {
    const image = new Image();
    const objectUrl = URL.createObjectURL(blob);
    image.onload = () => { URL.revokeObjectURL(objectUrl); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(objectUrl); reject(new Error('车牌图片解码失败')); };
    image.src = objectUrl;
  });
}

/** 调后端 plate-image，返回 plan + 已解码车牌图（供 Vehicle.setPlateImage）。 */
export async function loadBackendPlateImage({ plate, plateType, simApi, seq = 0 }) {
  if (!simApi?.plateImage) throw new Error('后端未提供 /api/sim/plate-image');
  const raw = await simApi.plateImage({ plate, plate_type: plateType });
  const plan = normalizeDispatchPlan(raw, { plate, plateType, seq });
  if (!plan.imageUrl) throw new Error('plate-image 未返回 image_url');
  const image = await loadAuthImage(plan.imageUrl);
  return {
    plate: plan.plate,
    plateType: plan.plateType,
    color: plan.color,
    vehicleKind: plan.vehicleKind,
    model: plan.model,
    imageUrl: plan.imageUrl,
    image,
  };
}
