"""Idea A: real-photo character verifier (unit + integration hooks)."""
from __future__ import annotations

import numpy as np
import pytest

from server.core.config import LPRConfig
from server.lpr.hyperlpr_adapter import HyperPlateRecognizer
from server.lpr.real_char_verifier import (
    RealCharVerification, RealCharVerifier, crop_for_verification)


class FakeSegmenter:
    """n blocks whose mean equals their index."""

    def segment(self, plate_img, n, color=None):
        blocks = []
        for i in range(n):
            block = np.zeros((64, 32), np.uint8)
            block[0, 0] = i * 10
            blocks.append(block)
        return blocks


class FakeClassifier:
    def __init__(self, mapping: dict[int, str]):
        self.mapping = mapping
        self.available = True

    def predict(self, char_img, head: str, topk: int = 3):
        idx = int(round(float(char_img[0, 0]) / 10.0))
        char = self.mapping.get(idx, "X")
        return [(char, 0.97), ("Y", 0.01), ("Z", 0.005)][:topk]


def _verifier(agree: bool) -> RealCharVerifier:
    plate = "京A12345"
    mapping = {i: ch for i, ch in enumerate(plate)}
    if not agree:
        mapping[3] = "9"   # third serial character mismatch
    return RealCharVerifier(LPRConfig(real_char_verify=True),
                            classifier=FakeClassifier(mapping),
                            segmenter=FakeSegmenter())


def test_verifier_agreement_and_mismatch():
    image = np.zeros((140, 440, 3), np.uint8)
    ok = _verifier(True).verify_plate("京A12345", "blue", image)
    assert ok.available and ok.agreement
    assert len(ok.char_confs) == 7 and ok.min_char_conf >= 0.9
    bad = _verifier(False).verify_plate("京A12345", "blue", image)
    assert bad.available and not bad.agreement
    assert "真实字符复核不一致" in bad.message


def test_verifier_skips_tiny_or_missing_crop():
    verifier = _verifier(True)
    assert verifier.verify_plate("京A12345", "blue", None).available is False
    small = np.zeros((10, 20, 3), np.uint8)
    assert verifier.verify_plate("京A12345", "blue", small).available is False


def test_crop_for_verification_bbox_and_guard():
    image = np.arange(100 * 200 * 3, dtype=np.uint8).reshape(100, 200, 3)
    crop = crop_for_verification(image, [10, 20, 110, 60])
    assert crop is not None and crop.shape[:2] == (40, 100)
    assert crop_for_verification(image, None) is None
    assert crop_for_verification(image, [10, 10, 10, 10]) is None


class FakeCatcher:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self, image):
        return self.rows


class FakeVerifier:
    available = True

    def __init__(self, agreement: bool):
        self.agreement = agreement
        self.calls = 0

    def verify_plate(self, plate, color, plate_image):
        self.calls += 1
        if self.agreement:
            return RealCharVerification(available=True, agreement=True,
                                        min_char_conf=0.99,
                                        char_confs=[0.99] * len(plate))
        return RealCharVerification(
            available=True, agreement=False, min_char_conf=0.0,
            char_confs=[0.0] * len(plate),
            message="真实字符复核不一致（3:5->9），请人工确认")


def _recognizer(cfg, verifier, rows=None):
    rows = rows or [["京A12345", 0.99, 0, [10, 10, 410, 130]]]
    return HyperPlateRecognizer(cfg, catcher=FakeCatcher(rows), verifier=verifier)


def test_adapter_mismatch_forces_manual_and_agreement_passes():
    cfg = LPRConfig(real_char_verify=True)
    cfg.style_refinement = False
    image = np.zeros((140, 440, 3), np.uint8)
    mismatch = _recognizer(cfg, FakeVerifier(False))
    result = mismatch.recognize_bgr(image)
    assert result.need_manual is True and result.success is False
    assert "真实字符复核" in result.message
    agreeing = _recognizer(cfg, FakeVerifier(True))
    result = agreeing.recognize_bgr(image)
    assert result.success is True and result.need_manual is False


def test_adapter_config_off_skips_verifier():
    cfg = LPRConfig(real_char_verify=False)
    cfg.style_refinement = False
    verifier = FakeVerifier(False)
    rec = _recognizer(cfg, verifier)
    result = rec.recognize_bgr(np.zeros((140, 440, 3), np.uint8))
    assert result.success is True
    assert verifier.calls == 0


@pytest.mark.skipif(
    not (LPRConfig().real_char_knn.exists() and
         (LPRConfig().real_char_templates / "meta.json").exists()),
    reason="Idea A 真实字符模型产物缺失；先运行 tools/train_real_char_cnn.py")
def test_real_artifacts_load():
    cfg = LPRConfig()
    verifier = RealCharVerifier(cfg)
    assert verifier.available
    assert verifier.backend.startswith(("knn:", "onnx:", "ncc:"))
    # classifier output contract
    pred = verifier.classifier.predict(np.zeros((64, 32), np.uint8), "hanzi", 3)
    assert pred and all(isinstance(ch, str) and 0.0 <= conf <= 1.0 for ch, conf in pred)
