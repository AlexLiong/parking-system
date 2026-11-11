"""Local-only HyperLPR3 inference with a conservative auto-pass policy."""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import numpy as np

from ..core.config import LPRConfig, PROJECT_ROOT
from ..core.domain import PlateColor, PlateContext, RecognitionResult
from .charset import validate_grammar
from .recognizer import PlateRecognizer, imread_unicode, imwrite_unicode

log = logging.getLogger("lpr.hyperlpr")

MODEL_DIR = PROJECT_ROOT / "server/lpr/models/hyperlpr3/20230229/onnx"
MODEL_HASHES = {
    "litemodel_cls_96x_r1.onnx": "fe123688d3bf08b9ef029fcd57f3ac4644ac2e5ef8b9e7a676aacd5fad152142",
    "rpv3_mdict_160_r3.onnx": "8fb08b5db2adeccf43b05006bbbf409e4659d08d72e46a62631c00ff751eaeb3",
    "y5fu_320x_sim.onnx": "2a985dc63a5cc947ec36d18503d6fc0fd54525b9dba17f2fe29a71e86b44456d",
    "y5fu_640x_sim.onnx": "0306de937471b87f56eb3f5620815e7e7058f8ab7428e0734fc64627cc4d716c",
}


def verify_models(model_dir: Path = MODEL_DIR) -> None:
    for name, expected in MODEL_HASHES.items():
        path = model_dir / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"HyperLPR model hash mismatch: {name}")


class HyperPlateRecognizer:
    model_id = "hyperlpr3-0.1.3-local-20230229"

    def __init__(self, cfg: LPRConfig, *, catcher=None, model_dir: Path = MODEL_DIR,
                 verifier=None):
        self.cfg = cfg
        self.threshold = max(0.95, cfg.conf_threshold)
        self._verifier = verifier
        self._verifier_initialized = verifier is not None
        if catcher is None:
            verify_models(model_dir)
            from hyperlpr3 import LicensePlateCatcher
            from hyperlpr3.common.typedef import DETECT_LEVEL_HIGH
            catcher = LicensePlateCatcher(folder=str(model_dir.parent.parent),
                                         detect_level=DETECT_LEVEL_HIGH)
        self.catcher = catcher

    def _get_verifier(self):
        """Lazy Idea-A verifier; disabled/missing artifacts leave the baseline intact."""
        if not getattr(self.cfg, "real_char_verify", False):
            return None
        if self._verifier_initialized:
            return self._verifier
        self._verifier_initialized = True
        try:
            from .real_char_verifier import RealCharVerifier
            verifier = RealCharVerifier(self.cfg)
            self._verifier = verifier if verifier.available else None
        except Exception:  # noqa: BLE001 - verification must never break recognition
            log.exception("真实字符复核初始化失败，保持原有识别行为")
            self._verifier = None
        return self._verifier

    def recognize_bgr(self, image: np.ndarray, *, context: PlateContext | None = None,
                      crop_dir: Path | None = None, tag: str = "") -> RecognitionResult:
        start = time.perf_counter()
        if image is None or image.size == 0:
            return RecognitionResult(need_manual=True, message="图像为空")
        try:
            found = self.catcher(image)
        except Exception:
            log.exception("HyperLPR inference failed")
            return RecognitionResult(need_manual=True, message="识别模型推理失败")
        if not found:
            return RecognitionResult(need_manual=True, message="未定位到车牌",
                                     elapsed_ms=int((time.perf_counter() - start) * 1000))

        candidates = []
        for item in found:
            plate, conf, plate_type, bbox = item
            plate = str(plate).replace("·", "").strip()
            color = PlateColor.GREEN if int(plate_type) == 3 or len(plate) == 8 else PlateColor.BLUE
            valid, _ = validate_grammar(plate, color.value)
            candidates.append((plate, float(conf), color, valid, bbox))
        candidates.sort(key=lambda row: (max(0, int(row[4][2]) - int(row[4][0])) *
                                         max(0, int(row[4][3]) - int(row[4][1])), row[1]),
                        reverse=True)
        plate, conf, color, valid, bbox = candidates[0]
        agreement = True
        alternatives = [row[0] for row in candidates]
        if self.cfg.style_refinement and len(candidates) == 1 and hasattr(self.catcher, "pipeline"):
            from .style_refiner import readings, select
            try:
                extra = readings(image, bbox, self.catcher.pipeline.recognizer)
                plate, conf, agreement, alternatives = select(plate, conf, extra, self.threshold)
                from .province_verifier import verify
                from .style_refiner import color_crop
                plate, corrected = verify(plate, color_crop(image, bbox))
                if corrected:
                    alternatives = list(dict.fromkeys([plate, *alternatives]))
                    agreement = False
                color = PlateColor.GREEN if len(plate) == 8 else PlateColor.BLUE
                valid, _ = validate_grammar(plate, color.value)
            except Exception:
                log.exception("Plate style refinement failed")
                agreement = False
        # ---- Idea A：真实照片字符分类器复核（可选；只降置信度/转人工）----
        char_ok = True
        char_confs: list[float] = []
        char_message = ""
        verifier = self._get_verifier()
        if verifier is not None and len(bbox) == 4:
            try:
                from .real_char_verifier import crop_for_verification, fuse_confidence
                crop = crop_for_verification(image, bbox)
                if crop is not None:
                    verification = verifier.verify_plate(plate, color, crop)
                    if verification.available:
                        char_confs = verification.char_confs
                        if verification.agreement:
                            conf = fuse_confidence(float(conf), verification)
                        else:
                            char_ok = False
                            char_message = verification.message
            except Exception:  # noqa: BLE001
                log.exception("真实字符复核失败，保持原有识别行为")
        crop_path = None
        if crop_dir is not None and len(bbox) == 4:
            x0, y0, x1, y1 = (int(v) for v in bbox)
            h, w = image.shape[:2]
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x1), min(h, y1)
            if x1 > x0 and y1 > y0:
                path = crop_dir / f"crop_{tag}_{time.time_ns()}.jpg"
                if imwrite_unicode(path, image[y0:y1, x0:x1]):
                    crop_path = str(path)
        success = (valid and conf >= self.threshold and len(candidates) == 1
                   and agreement and char_ok)
        if not valid:
            message = "车牌语法无效"
        elif len(candidates) > 1:
            message = "检测到多个车牌，请人工确认目标车辆"
        elif conf < self.threshold:
            message = "识别置信度不足"
        elif not agreement:
            message = "多种裁剪/光照处理未形成一致可信结果，请人工确认"
        elif not char_ok:
            message = char_message or "真实字符复核不一致，请人工确认"
        else:
            message = ""
        return RecognitionResult(
            plate=plate, color=color, overall_conf=conf,
            char_confs=char_confs,
            candidates=alternatives, plate_crop_path=crop_path,
            success=success, need_manual=not success, suspected=not valid,
            message=message,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )

    def recognize(self, image_path: str, *, context: PlateContext | None = None,
                  crop_dir: Path | None = None, tag: str = "") -> RecognitionResult:
        image = imread_unicode(image_path)
        if image is None:
            return RecognitionResult(need_manual=True, message=f"无法读取图像: {image_path}")
        return self.recognize_bgr(image, context=context, crop_dir=crop_dir, tag=tag)


class DemoAwareRecognizer:
    """兼容包装：所有图片统一走本地 HyperLPR3（不再保留旧 datasets/plates 模板分支）。"""

    def __init__(self, cfg: LPRConfig):
        try:
            self.real = HyperPlateRecognizer(cfg)
        except (FileNotFoundError, ValueError, ImportError, RuntimeError):
            log.exception("本地实拍识别模型不可用，真实图像转人工")
            self.real = None

    def recognize(self, image_path: str, **kwargs) -> RecognitionResult:
        if self.real is None:
            return RecognitionResult(need_manual=True, message="实拍识别模型不可用")
        return self.real.recognize(image_path, **kwargs)

    def recognize_bgr(self, image: np.ndarray, **kwargs) -> RecognitionResult:
        if self.real is None:
            return RecognitionResult(need_manual=True, message="实拍识别模型不可用")
        return self.real.recognize_bgr(image, **kwargs)
