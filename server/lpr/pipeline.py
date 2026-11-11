"""统一识别管线的简单变体生成与结果选择（CPU，HyperLPR3 为主）。

流程（由 ``LPRService.recognize`` 调用）：
1. 原图直接识别（HyperLPR3，保持主模型）；
2. 结果低置信/无车牌时，构造简单变体：CLAHE、2x 放大、±3° 旋转校正、
   bbox 裁剪（HyperLPR3 检测框优先，其次旧 PlateLocator 候选）；
3. 对每个变体识别一次，取置信度最高且车牌语法可用的结果。

不引入 PR-2 严格校验，也不启用 ``real_char_verify``（其默认 false）。
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import cv2
import numpy as np

log = logging.getLogger("lpr.pipeline")


def _limit_side(image: np.ndarray, max_side: int) -> np.ndarray:
    h, w = image.shape[:2]
    longest = max(h, w)
    if max_side and longest > max_side:
        scale = max_side / float(longest)
        image = cv2.resize(image, (int(w * scale), int(h * scale)),
                           interpolation=cv2.INTER_AREA)
    return image


def clahe_color(image: np.ndarray, clip: float = 2.5) -> np.ndarray:
    """在 LAB 亮度通道做 CLAHE，返回 BGR。"""
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lab[:, :, 0] = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8)).apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def upscale(image: np.ndarray, scale: float = 2.0) -> np.ndarray:
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    h, w = image.shape[:2]
    return cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))),
                      interpolation=cv2.INTER_CUBIC)


def rotate(image: np.ndarray, angle: float) -> np.ndarray:
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), float(angle), 1.0)
    return cv2.warpAffine(image, matrix, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def _catcher_boxes(recognizer, image: np.ndarray) -> list[tuple[int, int, int, int]]:
    catcher = getattr(recognizer, "catcher", None)
    if catcher is None:
        return []
    try:
        found = catcher(image) or []
    except Exception:  # noqa: BLE001 - 变体仅是兜底，不影响主流程
        log.exception("变体生成：检测器调用失败")
        return []
    boxes = []
    for item in found:
        try:
            bbox = item[3]
            x0, y0, x1, y1 = (int(v) for v in bbox)
            if x1 > x0 and y1 > y0:
                boxes.append((x0, y0, x1, y1))
        except (IndexError, TypeError, ValueError):
            continue
    return boxes


def _locator_boxes(image: np.ndarray, topk: int = 2) -> list[tuple[int, int, int, int]]:
    try:
        from .locator import PlateLocator
        locator = PlateLocator(locate_topk=topk)
        candidates = locator.locate(image)
    except Exception:  # noqa: BLE001
        log.exception("变体生成：旧定位器不可用")
        return []
    boxes = []
    for cand in candidates[:topk]:
        try:
            x0, y0, x1, y1 = (int(v) for v in (cand.x, cand.y, cand.x + cand.w, cand.y + cand.h))
            boxes.append((x0, y0, x1, y1))
        except (AttributeError, TypeError, ValueError):
            continue
    return boxes


def _crop_with_pad(image: np.ndarray, box, pad_ratio: float = 0.10) -> Optional[np.ndarray]:
    h, w = image.shape[:2]
    x0, y0, x1, y1 = box
    pad_x = max(2, int((x1 - x0) * pad_ratio))
    pad_y = max(2, int((y1 - y0) * pad_ratio))
    x0, y0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
    x1, y1 = min(w, x1 + pad_x), min(h, y1 + pad_y)
    if x1 <= x0 or y1 <= y0:
        return None
    return image[y0:y1, x0:x1]


def build_variants(image: np.ndarray, recognizer=None, *, max_variants: int = 6,
                   max_side: int = 1280) -> list[np.ndarray]:
    """低置信时使用的简单变体（不含原图；保持 CPU 可控数量）。"""
    if image is None or image.size == 0:
        return []
    base = _limit_side(image, max_side)
    variants: list[np.ndarray] = []

    def add(candidate: Optional[np.ndarray]) -> None:
        if candidate is None or candidate.size == 0 or len(variants) >= max_variants:
            return
        if candidate.shape[:2] == base.shape[:2] and np.shares_memory(candidate, base):
            return
        variants.append(candidate)

    add(clahe_color(base))
    add(upscale(base, 2.0))
    add(rotate(base, -3.0))
    add(rotate(base, 3.0))

    boxes = _catcher_boxes(recognizer, base) or _locator_boxes(base)
    for box in boxes[:2]:
        crop = _crop_with_pad(base, box)
        add(crop)
        add(upscale(crop, 2.0) if crop is not None else None)
        if len(variants) >= max_variants:
            break
    return variants


def is_low_result(result, threshold: float) -> bool:
    """是否需要尝试变体：无车牌/未成功/需人工/置信度低于阈值。"""
    if result is None:
        return True
    try:
        return bool(result.need_manual or not result.success or not result.plate
                    or result.overall_conf < float(threshold))
    except Exception:  # noqa: BLE001
        return True


def pick_best(results: Iterable):
    """选择置信度最高且带合法车牌的结果；同分时优先 success、再优先原顺序。"""
    best = None
    best_key = None
    for index, result in enumerate(results):
        if result is None:
            continue
        plate = getattr(result, "plate", None)
        conf = float(getattr(result, "overall_conf", 0.0) or 0.0)
        success = bool(getattr(result, "success", False))
        key = (1 if plate else 0, conf, success, -index)
        if best is None or key > best_key:
            best, best_key = result, key
    return best
