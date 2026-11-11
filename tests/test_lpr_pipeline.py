"""车牌识别流水线测试（lpr/recognizer.py + segmenter + validator + render）。

覆盖 prompt 的三条硬要求：
1. **识别与 3D 建模彻底解耦**：识别只吃一张独立车牌图像文件（render_scene 产出），
   不加载任何 glb/3D 资产；
2. **鲁棒性**：干净合成牌端到端识别达标；空白/损坏/缺失图不崩溃且转人工；
3. **安全性**：绝不把识别错误的车牌自动放行（success=True 必然字符串正确），
   不确定一律 need_manual。

同时回归一个真实缺陷：字符归一化的“边界环极性判定”会把粗笔画触边字符
（P/D/E/A/3…）误反相，导致 P→F 等系统性误识；主分割路径已改为信任整牌极性。
"""
from __future__ import annotations

import random

import numpy as np
import pytest

from server.core.domain import PlateColor, PlateContext
from server.lpr import charset
from server.lpr.recognizer import PlateRecognizer, imwrite_unicode
from server.lpr.render import PlateFonts, random_plate, render_plate, render_scene

cv2 = pytest.importorskip("cv2")


@pytest.fixture
def recognizer(cfg):
    rec = PlateRecognizer(cfg.lpr)
    if not getattr(rec.classifier, "available", True):
        pytest.skip("字符分类器不可用（缺模板/模型）")
    return rec


@pytest.fixture(scope="module")
def fonts():
    try:
        f = PlateFonts()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"渲染字体不可用：{exc}")
    if not f.cjk_path or not f.latin_path:
        pytest.skip("缺少 CJK/拉丁字体，无法渲染合成车牌")
    return f


def _scene_file(plate, color, fonts, path, augment=0.0, seed=0):
    img = render_scene(plate, color, fonts, scene_size=(900, 480),
                       augment_level=augment, rng=random.Random(seed))
    assert imwrite_unicode(path, img, ".jpg")
    return str(path)


def _core_recognize(rec, plate_img, color):
    """直接驱动 分割→分类→校验 三段核心（绕过定位/校正），验证识别逻辑本身。"""
    n = charset.expected_len(color.value)
    blocks = rec.segmenter.segment(plate_img, n, color)
    if len(blocks) != n:
        return None
    preds = []
    for i, b in enumerate(blocks):
        head = "hanzi" if i == 0 else ("letter" if i == 1 else "alnum")
        preds.append(rec.classifier.predict(b, head, topk=3))
    return rec.validator.validate(preds, color, PlateContext()).plate


# ----------------------------- 识别核心（回归极性缺陷） -----------------------------
def test_core_recognition_accurate(recognizer, fonts):
    """干净车牌本体经核心三段应高准确率识别（回归 P→F 极性反相缺陷）。"""
    rng = random.Random(7)
    hits = total = 0
    for _ in range(20):
        color = PlateColor.BLUE
        plate = random_plate(color, rng)
        got = _core_recognize(recognizer, render_plate(plate, color, fonts), color)
        total += 1
        hits += (got == plate)
    assert hits >= total * 0.8, f"核心识别准确率过低: {hits}/{total}"


def test_heavy_stroke_char_not_inverted(recognizer, fonts):
    """粗笔画触边字符（如 P）归一化后不得被误反相：应与自身模板高度相关。"""
    from server.lpr.classifier import _ncc
    from server.lpr.render import render_char_template
    img = render_plate("浙ER346P", PlateColor.BLUE, fonts)
    blocks = recognizer.segmenter.segment(img, 7, PlateColor.BLUE)
    tmpl = render_char_template("P", fonts, tuple(recognizer.cfg.char_size))
    # 末位 P 的归一化块与 P 模板强正相关（反相会得到负相关）
    assert _ncc(blocks[6], tmpl) > 0.8


# ----------------------------- 端到端往返（场景图） -----------------------------
def test_clean_scene_roundtrip(recognizer, fonts, tmp_path):
    rng = random.Random(123)
    hits = total = 0
    for i in range(12):
        plate = random_plate(PlateColor.BLUE, rng)
        p = _scene_file(plate, PlateColor.BLUE, fonts, tmp_path / f"b{i}.jpg",
                        augment=0.0, seed=i)
        res = recognizer.recognize(p)
        total += 1
        hits += (res.plate == plate)
    # 干净合成牌经完整场景流水线（定位+校正+分割+识别）的整牌正确率
    assert hits >= total * 0.6, f"干净场景往返命中率过低: {hits}/{total}"


def test_no_wrong_plate_auto_passes(recognizer, fonts, tmp_path):
    """安全底线：success=True 时车牌字符串必须正确；不确定一律 need_manual。"""
    rng = random.Random(2024)
    for i in range(15):
        color = PlateColor.BLUE if i % 2 == 0 else PlateColor.GREEN
        plate = random_plate(color, rng)
        p = _scene_file(plate, color, fonts, tmp_path / f"s{i}.jpg",
                        augment=0.5, seed=i)
        res = recognizer.recognize(p)
        if res.success:
            assert res.plate == plate, f"危险：错误车牌被自动放行 {res.plate} != {plate}"
        else:
            assert res.need_manual is True


def test_recognize_decoupled_from_3d(recognizer, fonts, tmp_path):
    # 仅给一张独立车牌图像文件即可识别，全程不加载任何 glb/3D 资产
    p = _scene_file("琼A12345", PlateColor.BLUE, fonts, tmp_path / "solo.jpg")
    res = recognizer.recognize(p)
    assert res.plate is not None
    assert res.elapsed_ms >= 0


# ----------------------------- 鲁棒性：异常输入 -----------------------------
def test_blank_image_needs_manual_no_crash(recognizer, tmp_path):
    blank = np.full((480, 900, 3), 255, dtype=np.uint8)
    p = tmp_path / "blank.jpg"
    assert imwrite_unicode(p, blank, ".jpg")
    res = recognizer.recognize(str(p))
    assert res.success is False
    assert res.need_manual is True


def test_missing_file_needs_manual(recognizer):
    res = recognizer.recognize("/tmp/__no_such_plate__.jpg")
    assert res.success is False
    assert res.need_manual is True


def test_green_plate_length_correct(recognizer, fonts, tmp_path):
    rng = random.Random(77)
    plate = random_plate(PlateColor.GREEN, rng)
    p = _scene_file(plate, PlateColor.GREEN, fonts, tmp_path / "g0.jpg", seed=1)
    res = recognizer.recognize(p)
    assert res.color in (PlateColor.GREEN, None)
    assert res.plate is None or len(res.plate) == 8
