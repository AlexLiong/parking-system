"""Plate rendering backed by the external MIT Chinese plate generator.

The product/demo/dispatch paths use the upstream ``chinese_license_plate_generator``
templates (blue/yellow/green_car/green_truck + font_model + get_location_data).
The old in-repo ``_base_image`` hand-drawn bases/positions are removed/disabled.
"""
from __future__ import annotations

import logging
import random
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from ..core.domain import PlateColor
from . import charset
from .segmenter import CharSegmenter

log = logging.getLogger("lpr.render")

EXTERNAL_GENERATOR_ROOT = Path(__file__).resolve().parents[2] / (
    "datasets/external/chinese_license_plate_generator")
EXTERNAL_BASES = {
    "blue": "blue",
    "yellow": "yellow",
    "green": "green_car",
    "green_large": "green_truck",
}


@lru_cache(maxsize=1)
def _external_generator():
    """Lazy load upstream MultiPlateGenerator (no copy/fork of its assets)."""
    root = EXTERNAL_GENERATOR_ROOT
    if not (root / "generate_multi_plate.py").exists():
        raise RuntimeError(
            f"缺少外部车牌生成器（MIT）：{root}；请从 "
            "https://github.com/Pengfei8324/chinese_license_plate_generator 获取")
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from generate_multi_plate import MultiPlateGenerator  # type: ignore
    return MultiPlateGenerator(str(root / "plate_model"), str(root / "font_model"))


def external_render_plate(plate: str, plate_type: str,
                          size=None) -> np.ndarray:
    """Render with the external project's plate_model/font_model templates."""
    ptype = charset.normalize_plate_type(plate_type)
    base = EXTERNAL_BASES[ptype]
    generator = _external_generator()
    image = generator.generate_plate_special(plate, base, False, enhance=False)
    if image is None:
        raise RuntimeError(f"外部车牌生成器返回空图像: {plate}/{ptype}")
    image = np.asarray(image, dtype=np.uint8).copy()
    if size is not None:
        image = cv2.resize(image, tuple(map(int, size)))
    return image


class PlateFonts:
    """Legacy MIT glyph handle retained only for char-template compatibility."""
    def __init__(self, cjk_path=None, latin_path=None):
        if cjk_path or latin_path:
            raise ValueError("使用固定 MIT 字形；不接受系统字体替换")
        import hashlib,json
        root=Path(__file__).parent/'models/reference-plates'
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        data=(root/'assets.npz').read_bytes()
        if hashlib.sha256(data).hexdigest()!=manifest['sha256']:
            raise ValueError('reference plate assets hash mismatch')
        self.cjk_path=self.latin_path=str(root/'manifest.json')
        self.characters=manifest['characters']
        with np.load(root/'assets.npz',allow_pickle=False) as z:
            self.assets={key:z[key].copy() for key in z.files}
    def mask(self,ch,color=PlateColor.BLUE, plate_type=None):
        style='green' if _plate_type(color, plate_type) in ('green','green_large') else '140'
        return np.where(self.assets['glyphs_'+style][self.characters.index(ch)]<200,255,0).astype(np.uint8)


def _plate_type(color, plate_type=None) -> str:
    """兼容旧 PlateColor 调用，并接受新 blue/green/green_large/yellow。"""
    if plate_type is not None:
        return charset.normalize_plate_type(plate_type)
    if isinstance(color, PlateColor):
        return color.value
    return charset.normalize_plate_type(str(color))


def _glyph_color(plate_type: str) -> tuple[int, int, int]:
    # 仅 render_char_template 使用；plate 本体已由外部模板生成
    return (255, 255, 255) if plate_type == "blue" else (20, 20, 20)


def _paste_mask(image, mask, box, color, plate_type=None):
    """仅 render_char_template 使用；plate 本体不再用此逻辑贴字。"""
    ptype = _plate_type(color, plate_type)
    x0, y0, x1, y1 = map(int, box)
    mask = cv2.resize(mask, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST) > 127
    image[y0:y1, x0:x1][mask] = _glyph_color(ptype)


def render_glyph_mask(ch: str, fonts: PlateFonts, size: int = 140) -> np.ndarray:
    mask=fonts.mask(ch)
    return cv2.copyMakeBorder(mask,20,20,20,20,cv2.BORDER_CONSTANT,value=0)


def render_char_template(ch: str, fonts: PlateFonts, char_size=(20,40), color=PlateColor.BLUE,
                         plate_type=None) -> np.ndarray:
    ptype = _plate_type(color, plate_type)
    seg=CharSegmenter(char_size)
    bg=(90,160,90) if ptype in ('green','green_large') else (
        (0, 215, 255) if ptype == 'yellow' else (140,60,0))
    image=np.full((140,96,3),bg,np.uint8)
    _paste_mask(image,fonts.mask(ch,PlateColor.BLUE,plate_type=ptype),(25,25,70,115),None,ptype)
    seg_color = PlateColor.GREEN if ptype in ('green','green_large') else PlateColor.BLUE
    blocks=seg.segment(image,1,seg_color)
    return blocks[0] if blocks else seg._normalize(fonts.mask(ch,PlateColor.BLUE,plate_type=ptype))


def render_plate(plate: str, color, fonts: PlateFonts = None, size=None,
                 plate_type=None) -> np.ndarray:
    """Render via the external MIT plate_model/font_model (BGR output)."""
    ptype = _plate_type(color, plate_type)
    return external_render_plate(plate, ptype, size=size)


# --------------------------------------------------------------------------
# 合成场景（把车牌嵌入更大的图像，满足定位器面积占比约束并增强真实感）
# --------------------------------------------------------------------------
def _background(W: int, H: int, rng: random.Random) -> np.ndarray:
    """生成中性渐变背景（模拟车头/路面），带轻微纹理，避免干扰颜色定位。"""
    base = rng.randint(40, 90)
    grad = np.linspace(-18, 18, H, dtype=np.float32)[:, None]
    arr = np.clip(base + grad, 0, 255)
    arr = np.repeat(arr, W, axis=1)
    img = np.dstack([arr, arr * 0.98, arr * 0.95]).astype(np.uint8)
    noise = rng.randint(0, 6)
    if noise:
        img = np.clip(img.astype(np.int16) +
                      np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, noise, img.shape),
                      0, 255).astype(np.uint8)
    return img


def augment(img: np.ndarray, rng: random.Random, level: float = 1.0) -> np.ndarray:
    """数据增强（RR-1.1）：亮度/对比度、模糊、噪声、轻微透视、JPEG 压缩、遮挡/污渍。

    ``level`` ∈ [0,1] 控制强度；0 表示不增强。所有变换保持车牌可辨识。
    """
    if level <= 0:
        return img
    out = img.copy()

    # 亮度/对比度
    if rng.random() < 0.7 * level:
        alpha = rng.uniform(1 - 0.35 * level, 1 + 0.35 * level)
        beta = rng.uniform(-30 * level, 30 * level)
        out = np.clip(out.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)

    # 模糊
    if rng.random() < 0.4 * level:
        k = rng.choice([3, 3, 5])
        out = cv2.GaussianBlur(out, (k, k), 0)

    # 高斯噪声
    if rng.random() < 0.4 * level:
        sigma = rng.uniform(2, 10) * level
        out = np.clip(out.astype(np.float32) +
                      np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, sigma, out.shape),
                      0, 255).astype(np.uint8)

    # 轻微透视（模拟拍摄角度）
    if rng.random() < 0.5 * level:
        h, w = out.shape[:2]
        dx = w * 0.02 * level
        dy = h * 0.03 * level
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        jx = rng.uniform(-dx, dx)
        jy = rng.uniform(-dy, dy)
        dst = np.float32([[jx, jy], [w - jx, -jy], [w + jx, h + jy], [-jx, h - jy]])
        M = cv2.getPerspectiveTransform(src, dst)
        out = cv2.warpPerspective(out, M, (w, h), flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)

    # 遮挡条（模拟栏杆/污物部分遮挡）
    if rng.random() < 0.18 * level:
        h, w = out.shape[:2]
        bh = max(2, int(h * rng.uniform(0.03, 0.08)))
        by = rng.randint(0, max(0, h - bh))
        out[by:by + bh, :] = rng.randint(20, 60)

    # 污渍斑点
    if rng.random() < 0.2 * level:
        for _ in range(rng.randint(1, 4)):
            h, w = out.shape[:2]
            rr = rng.randint(3, max(4, int(min(h, w) * 0.05)))
            cx, cy = rng.randint(0, w - 1), rng.randint(0, h - 1)
            cv2.circle(out, (cx, cy), rr, (rng.randint(30, 90),) * 3, -1)

    # JPEG 压缩伪影
    if rng.random() < 0.5 * level:
        q = rng.randint(int(45 - 20 * level), 90)
        ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), max(20, q)])
        if ok:
            out = cv2.imdecode(buf, cv2.IMREAD_COLOR)

    return out


def render_scene(plate: str, color, fonts: PlateFonts, *,
                 plate_type: Optional[str] = None,
                 scene_size: tuple[int, int] = (900, 480),
                 plate_scale: float = 0.62,
                 augment_level: float = 0.0,
                 rng: Optional[random.Random] = None) -> np.ndarray:
    """把车牌嵌入场景图（BGR）。用于合成数据集与识别往返测试。"""
    rng = rng or random.Random()
    plate_img = render_plate(plate, color, fonts, plate_type=plate_type)
    SW, SH = int(scene_size[0]), int(scene_size[1])
    bg = _background(SW, SH, rng)

    ph, pw = plate_img.shape[:2]
    target_w = int(SW * plate_scale * rng.uniform(0.9, 1.1))
    scale = target_w / float(pw)
    target_h = max(20, int(ph * scale))
    if target_h > SH * 0.7:
        target_h = int(SH * 0.7)
        scale = target_h / float(ph)
        target_w = int(pw * scale)
    plate_r = cv2.resize(plate_img, (target_w, target_h), interpolation=cv2.INTER_CUBIC)

    # 随机放置（留出边距，保证完整可见）
    max_x = max(0, SW - target_w)
    max_y = max(0, SH - target_h)
    x = rng.randint(int(max_x * 0.15), int(max_x * 0.85)) if max_x > 10 else max_x // 2
    y = rng.randint(int(max_y * 0.2), int(max_y * 0.8)) if max_y > 10 else max_y // 2
    bg[y:y + target_h, x:x + target_w] = plate_r

    if augment_level > 0:
        bg = augment(bg, rng, augment_level)
    return bg


# --------------------------------------------------------------------------
# 随机合法号牌（城市码来自 charset 的省份→发牌机关映射，不随机字母）
# --------------------------------------------------------------------------
def random_plate(color: PlateColor | str, rng: Optional[random.Random] = None) -> str:
    """生成一个语法合法的随机号牌（蓝 7 / 绿 8，绿牌序号首位 D/F）。"""
    rng = rng or random.Random()
    ptype = color.value if isinstance(color, PlateColor) else charset.normalize_plate_type(
        str(color))
    return charset.generate_plate_type(ptype, rng)


def random_plate_type(plate_type: str, rng: Optional[random.Random] = None) -> str:
    """生成任意演示 plate_type 的合法号牌（blue/green/green_large/yellow）。"""
    return charset.generate_plate_type(plate_type, rng or random.Random())
