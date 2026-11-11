"""车牌识别流水线（lpr/recognizer.py）。

``PlateRecognizer`` 串起 预处理→定位→校正→颜色判定→分割→字符识别→校验 全流程，
是纯 CV 组件（无数据库/网络依赖），可脱离系统单独测试与替换（RR-4.2）。

跨平台注意：用 ``imread_unicode``/``imwrite_unicode`` 走 np.fromfile+imdecode，
避免 ``cv2.imread`` 在 Windows 上无法读取含中文的路径。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from ..core.config import LPRConfig
from ..core.domain import (PlateCandidate, PlateColor, PlateContext,
                           RecognitionResult)
from . import charset
from .classifier import (CharClassifier, HEAD_ALNUM, HEAD_HANZI, HEAD_LETTER,
                         build_classifier)
from .locator import PlateLocator
from .preprocess import Preprocessor
from .rectifier import PlateRectifier
from .segmenter import CharSegmenter
from .validator import PlateValidator

log = logging.getLogger("lpr.recognizer")


def imread_unicode(path: str | Path, flags=cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """Unicode 安全读图。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except (FileNotFoundError, OSError, cv2.error):
        return None


def imwrite_unicode(path: str | Path, img: np.ndarray, ext: str = ".jpg") -> bool:
    """Unicode 安全写图。"""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        buf.tofile(str(path))
        return True
    except (OSError, cv2.error):
        log.exception("写图失败: %s", path)
        return False


def _head_for_pos(pos: int) -> str:
    if pos == 0:
        return HEAD_HANZI
    if pos == 1:
        return HEAD_LETTER
    return HEAD_ALNUM


class PlateRecognizer:
    """车牌识别核心流水线。"""

    def __init__(self, cfg: LPRConfig, classifier: Optional[CharClassifier] = None):
        self.cfg = cfg
        self.pre = Preprocessor(cfg.max_image_side)
        self.locator = PlateLocator(cfg.locate_topk)
        self.rectifier = PlateRectifier(cfg.blue_size, cfg.green_size)
        self.segmenter = CharSegmenter(cfg.char_size)
        self.validator = PlateValidator(cfg.disambig_gap, cfg.conf_threshold)
        self.classifier = classifier or build_classifier(
            cfg.backend, cfg.onnx_model, cfg.template_dir, cfg.char_size)

    # ----------------------------- 单候选识别 -----------------------------
    def _recognize_candidate(self, color_img: np.ndarray, cand: PlateCandidate,
                             context: PlateContext,
                             crop_dir: Optional[Path] = None,
                             tag: str = "") -> RecognitionResult:
        crop = self.locator.crop(color_img, cand)
        if crop.size == 0:
            return RecognitionResult(success=False, message="候选区为空")
        color = cand.color or self.rectifier.detect_color(crop)
        target = self.rectifier.target_size_for(color)
        plate = self.rectifier.rectify(crop, cand, target)
        n = charset.expected_len(color.value)
        char_imgs = self.segmenter.segment(plate, n, color)

        preds: list[list[tuple[str, float]]] = []
        for i, cimg in enumerate(char_imgs):
            head = _head_for_pos(i)
            topk = self.classifier.predict(cimg, head, topk=3)
            preds.append(topk)

        vr = self.validator.validate(preds, color, context)

        crop_path = None
        if crop_dir is not None:
            fname = f"crop_{tag}_{int(time.time() * 1000)}.jpg"
            p = crop_dir / fname
            if imwrite_unicode(p, plate):
                crop_path = str(p)

        overall = vr.overall_conf
        need_manual = (not vr.valid) or overall < self.cfg.conf_threshold
        return RecognitionResult(
            plate=vr.plate, color=PlateColor(color.value),
            char_confs=vr.char_confs, overall_conf=overall,
            candidates=vr.candidates, plate_crop_path=crop_path,
            success=vr.valid and overall >= self.cfg.conf_threshold,
            suspected=vr.suspected, need_manual=need_manual,
            message=vr.reason,
        )

    # ----------------------------- 对外入口 -----------------------------
    def recognize_bgr(self, img_bgr: np.ndarray, *, context: Optional[PlateContext] = None,
                      crop_dir: Optional[Path] = None, tag: str = "") -> RecognitionResult:
        """对已在内存中的 BGR 图像执行识别。"""
        t0 = time.perf_counter()
        context = context or PlateContext()
        color_img = self.pre.process_color(img_bgr)
        gray = self.pre.to_gray(color_img)
        candidates = self.locator.locate(color_img, gray)
        if not candidates:
            return RecognitionResult(success=False, need_manual=True,
                                     elapsed_ms=int((time.perf_counter() - t0) * 1000),
                                     message="未定位到车牌")

        best: Optional[RecognitionResult] = None
        for idx, cand in enumerate(candidates[: self.cfg.locate_topk]):
            res = self._recognize_candidate(color_img, cand, context, crop_dir,
                                            tag=f"{tag}c{idx}")
            res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            # 优先选合法且置信度最高者
            if best is None or self._better(res, best):
                best = res
            if best.success and best.overall_conf >= 0.95:
                break
        assert best is not None
        return best

    @staticmethod
    def _better(a: RecognitionResult, b: RecognitionResult) -> bool:
        if a.success != b.success:
            return a.success
        return a.overall_conf > b.overall_conf

    def recognize(self, image_path: str, *, context: Optional[PlateContext] = None,
                  crop_dir: Optional[Path] = None, tag: str = "") -> RecognitionResult:
        """IPlateRecognizer 接口：对图像文件路径执行识别。"""
        img = imread_unicode(image_path)
        if img is None:
            return RecognitionResult(success=False, need_manual=True,
                                     message=f"无法读取图像: {image_path}")
        return self.recognize_bgr(img, context=context, crop_dir=crop_dir, tag=tag)
