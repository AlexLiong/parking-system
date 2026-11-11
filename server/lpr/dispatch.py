"""演示用车牌图片生成（/api/sim/plate-image、dispatch-plan 共用）。

只使用现有的 MIT 字形/底板资产（``server/lpr/models/reference-plates``），
不引入系统字体；生成物写到 ``data/photos/uploads/dispatch/``，由前端经 /media 加载。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import cv2

from . import charset
from .render import PlateFonts, render_plate

_FONTS: PlateFonts | None = None


def get_fonts() -> PlateFonts:
    global _FONTS
    if _FONTS is None:
        _FONTS = PlateFonts()
    return _FONTS


def create_plate_image(plate: str, plate_type: str, photos_dir: str | Path) -> dict:
    """严格校验并渲染车牌图；返回不含 URL 的元数据（URL 由 gateway 映射）。"""
    normalized_plate, normalized_type = charset.normalize_plate_type_plate(
        plate, plate_type)
    spec = charset.plate_type_spec(normalized_type)
    image = render_plate(normalized_plate, normalized_type, get_fonts())
    dest_dir = Path(photos_dir) / "uploads" / "dispatch"
    dest_dir.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(
        f"{normalized_type}:{normalized_plate}:{uuid4().hex}".encode("utf-8")
    ).hexdigest()[:12]
    # 文件名只用 ASCII hash，避免 OpenCV 在 Windows 下无法写入中文路径
    target = dest_dir / f"{normalized_type}_{token}.png"
    if not cv2.imwrite(str(target), image):
        raise RuntimeError(f"车牌图片写入失败: {target}")
    return {
        "plate": normalized_plate,
        "plate_type": normalized_type,
        "color": spec["color"],
        "vehicle_kind": spec["vehicle_kind"],
        "image_path": str(target),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
    }
