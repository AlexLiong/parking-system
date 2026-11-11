"""Real-photo character verification for the HyperLPR3 result (Idea A).

The verifier is *post-hoc*: HyperLPR3 already produced a plate string and a bounding
box. We segment that plate crop with the repository's ``CharSegmenter`` and classify
every character with a classifier trained **only on real photos** (``Idea A``):

- primary: ``server/lpr/models/char_cnn_real.onnx`` (``OnnxClassifier`` contract,
  three heads: hanzi/letter/alnum), trained by ``tools/train_real_char_cnn.py``;
- fallback: ``server/lpr/models/real_char_templates/`` (``TemplateClassifier``
  compatible NCC templates from the same real character crops).

Safety rule: the verifier never relaxes the recognition threshold. It can only
- lower the fused confidence (``min(hyperlpr_conf, min_expected_char_prob)``), and
- force manual review when the expected characters are not the classifier's top-1.

Every component is optional and injectable for tests; when the artifacts are absent
(or the plate crop is too small to segment) verification is reported as
``available=False`` and the caller keeps the baseline behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core.config import LPRConfig
from ..core.domain import PlateColor
from .classifier import HEAD_ALNUM, HEAD_HANZI, HEAD_LETTER, CharClassifier

log = logging.getLogger("lpr.real_char_verifier")

# Crops below this size are not real detections (unit-test fixtures / degenerate
# boxes); verification is skipped instead of blocking a gate.
MIN_CROP_HEIGHT = 24
MIN_CROP_WIDTH = 48


def head_for_position(pos: int) -> str:
    return HEAD_HANZI if pos == 0 else HEAD_LETTER if pos == 1 else HEAD_ALNUM


@dataclass
class RealCharVerification:
    available: bool
    agreement: bool = True
    min_char_conf: float = 0.0
    char_confs: list[float] = field(default_factory=list)
    top1: list[str] = field(default_factory=list)
    message: str = ""
    backend: str = ""

    def to_dict(self) -> dict:
        return {"available": self.available, "agreement": self.agreement,
                "min_char_conf": round(self.min_char_conf, 4),
                "char_confs": [round(c, 4) for c in self.char_confs],
                "top1": self.top1, "message": self.message,
                "backend": self.backend}


class RealCharVerifier:
    """Segment a plate crop and verify the HyperLPR3 characters with real-data model."""

    def __init__(self, cfg: LPRConfig, *, classifier: CharClassifier | None = None,
                 segmenter=None):
        self.cfg = cfg
        self.backend = "injected" if classifier is not None else "none"
        self.segmenter = segmenter
        if classifier is None:
            classifier = self._load_classifier()
        self.classifier = classifier
        self._ready = bool(classifier is not None
                           and getattr(classifier, "available", True))
        if not self._ready:
            log.warning("真实字符复核不可用（缺少 char_cnn_real.onnx 与 real_char_templates），"
                        "HyperLPR3 结果保持原行为")

    # ----------------------------- loading -----------------------------
    def _load_classifier(self) -> CharClassifier | None:
        from .classifier import OnnxClassifier, TemplateClassifier
        cfg = self.cfg
        char_size = tuple(cfg.char_size)
        model_path = Path(getattr(cfg, "real_char_model", ""))
        if model_path and model_path.exists():
            onnx = OnnxClassifier(model_path, char_size)
            if onnx.load():
                self.backend = f"onnx:{model_path.name}"
                log.info("真实字符复核后端：ONNX(%s)", model_path)
                return onnx
        knn_path = Path(getattr(cfg, "real_char_knn", ""))
        if knn_path and knn_path.exists():
            from .real_char_knn import RealCharKnn
            knn = RealCharKnn(knn_path)
            if knn.load():
                self.backend = f"knn:{knn_path.name}"
                log.info("真实字符复核后端：真实照片 kNN/NCC(%s)", knn_path)
                return knn
        template_dir = Path(getattr(cfg, "real_char_templates", ""))
        if template_dir and (template_dir / "meta.json").exists():
            templates = TemplateClassifier(template_dir, char_size)
            templates.load()
            if templates.available:
                self.backend = f"ncc:{template_dir.name}"
                log.info("真实字符复核后端：真实模板 NCC(%s)", template_dir)
                return templates
        return None

    @property
    def available(self) -> bool:
        return self._ready

    # ----------------------------- verification -----------------------------
    def verify_plate(self, plate: str, color: PlateColor | str | None,
                     plate_image: np.ndarray | None) -> RealCharVerification:
        if not self._ready:
            return RealCharVerification(available=False, backend=self.backend,
                                        message="真实字符复核不可用")
        if not plate or plate_image is None or plate_image.size == 0:
            return RealCharVerification(available=False, backend=self.backend,
                                        message="无车牌裁剪图，跳过复核")
        h, w = plate_image.shape[:2]
        if h < MIN_CROP_HEIGHT or w < MIN_CROP_WIDTH:
            return RealCharVerification(available=False, backend=self.backend,
                                        message="车牌裁剪过小，跳过复核")
        color_value = color.value if isinstance(color, PlateColor) else (
            color or ("green" if len(plate) == 8 else "blue"))
        # Training/evaluation crops are resized to the standard plate size; apply the
        # same normalisation here so the segmenter/classifier see the same distribution.
        import cv2
        size = (self.cfg.green_size if (len(plate) == 8 or color_value == "green")
                else self.cfg.blue_size)
        plate_image = cv2.resize(plate_image, (int(size[0]), int(size[1])),
                                 interpolation=cv2.INTER_CUBIC)
        expected = list(plate)
        if self.segmenter is None:
            from .segmenter import CharSegmenter
            self.segmenter = CharSegmenter(tuple(self.cfg.char_size))
        blocks = self.segmenter.segment(plate_image, len(expected), color_value)
        if len(blocks) != len(expected):
            return RealCharVerification(
                available=True, agreement=False, backend=self.backend,
                message=f"真实字符复核：分割出 {len(blocks)} 个字符（期望 {len(expected)}），转人工")
        confs: list[float] = []
        top1: list[str] = []
        mismatches = []
        for pos, (block, want) in enumerate(zip(blocks, expected)):
            head = head_for_position(pos)
            top = self.classifier.predict(block, head, topk=3)
            got = top[0][0] if top else ""
            want_conf = next((float(conf) for ch, conf in top if ch == want), 0.0)
            confs.append(want_conf)
            top1.append(got)
            if got != want:
                mismatches.append(f"{pos + 1}:{want}->{got or '?'}")
        agreement = not mismatches
        result = RealCharVerification(
            available=True, agreement=agreement,
            min_char_conf=float(min(confs)) if confs else 0.0,
            char_confs=confs, top1=top1, backend=self.backend)
        if agreement:
            result.message = ""
        else:
            result.message = ("真实字符复核不一致（" + "，".join(mismatches[:4]) +
                              "），请人工确认")
        return result


def fuse_confidence(base_conf: float, verification: RealCharVerification) -> float:
    """Fusion may only lower the reported confidence; thresholds never change."""
    if not verification.available or not verification.agreement:
        return base_conf
    return float(min(base_conf, verification.min_char_conf))


def crop_for_verification(image: np.ndarray, bbox) -> np.ndarray | None:
    """Detector-bbox crop, matching the crop strategy used to train the verifier."""
    if image is None or bbox is None or len(bbox) != 4:
        return None
    h, w = image.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in bbox)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 + 4 or y1 <= y0 + 4:
        return None
    return image[y0:y1, x0:x1]
