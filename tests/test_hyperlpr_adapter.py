from __future__ import annotations

import numpy as np

from server.core.config import LPRConfig, PROJECT_ROOT
from server.lpr.hyperlpr_adapter import DemoAwareRecognizer, HyperPlateRecognizer


class FakeCatcher:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self, image):
        return self.rows


def test_high_confidence_valid_plate_can_pass():
    rec = HyperPlateRecognizer(LPRConfig(), catcher=FakeCatcher([
        ["京A12345", 0.98, 0, [1, 1, 20, 10]],
    ]))
    result = rec.recognize_bgr(np.zeros((20, 40, 3), dtype=np.uint8))
    assert result.success is True
    assert result.need_manual is False


def test_low_confidence_or_invalid_plate_requires_manual():
    image = np.zeros((20, 40, 3), dtype=np.uint8)
    low = HyperPlateRecognizer(LPRConfig(), catcher=FakeCatcher([
        ["京A12345", 0.94, 0, [1, 1, 20, 10]],
    ]))
    invalid = HyperPlateRecognizer(LPRConfig(), catcher=FakeCatcher([
        ["京A123456", 0.99, 0, [1, 1, 20, 10]],
    ]))
    assert low.recognize_bgr(image).need_manual is True
    assert invalid.recognize_bgr(image).need_manual is True


def test_multiple_plates_require_manual_even_with_high_confidence():
    rec = HyperPlateRecognizer(LPRConfig(), catcher=FakeCatcher([
        ["津E28437", 0.98, 0, [40, 30, 180, 100]],
        ["津GTL921", 0.995, 0, [10, 10, 90, 45]],
    ]))
    result = rec.recognize_bgr(np.zeros((120, 200, 3), dtype=np.uint8))
    assert result.plate == "津E28437"
    assert result.need_manual is True
    assert result.success is False
    assert result.candidates == ["津E28437", "津GTL921"]


def test_missing_real_model_never_falls_back_to_template(tmp_path):
    rec = DemoAwareRecognizer.__new__(DemoAwareRecognizer)
    rec.real = None
    assert rec.recognize(str(tmp_path / "real.jpg")).need_manual is True
    assert rec.recognize(str(tmp_path / "blue/blue_00000.jpg")).need_manual is True
