"""车牌规则/外部模板/城市码表测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from server.lpr import charset
from server.lpr.render import (EXTERNAL_GENERATOR_ROOT, external_render_plate,
                                render_plate)


ROOT = Path(__file__).resolve().parents[1]
CITY_FILE = ROOT / "server/lpr/plate_city_codes.json"


def test_city_code_table_is_complete_and_legal():
    assert CITY_FILE.exists()
    raw = json.loads(CITY_FILE.read_text(encoding="utf-8"))
    assert set(raw) == set(charset.PROVINCES) and len(raw) == 31
    for province, codes in raw.items():
        assert codes and "A" in codes, province
        assert "I" not in codes and "O" not in codes, province
        assert all("A" <= ch <= "Z" for ch in codes), province
        assert set(codes) == set(charset.PROVINCE_CITY_CODES[province])
    assert sum(len(c) for c in charset.PROVINCE_CITY_CODES.values()) >= 300
    # 省会 A 必须合法；未列入的城市字母必须拒绝
    for province in charset.PROVINCES:
        assert charset.validate_plate_type(province + "A12345" if province not in
                                           "沪渝" else province + "A12345",
                                           "blue")[0], province
    assert charset.validate_plate_type("京Z12345", "blue")[0] is False
    assert charset.validate_plate_type("沪Q12345", "blue")[0] is False


def test_four_types_lengths_and_new_energy_bits():
    cases = [
        ("blue", 7, "A12345"),
        ("yellow", 7, "A12345"),
        ("green", 8, "AF12345"),
        ("green_large", 8, "A12345D"),
    ]
    for plate_type, _length, suffix in cases:
        plate = "粤" + suffix
        ok, reason = charset.validate_plate_type(plate, plate_type)
        assert ok, (plate_type, plate, reason)
    assert charset.validate_plate_type("粤A1234567", "blue")[0] is False
    assert charset.validate_plate_type("粤A123456", "green")[0] is False       # 首位非 D/F
    assert charset.validate_plate_type("粤A12345D", "green")[0] is False      # 小绿末位 D 不算
    assert charset.validate_plate_type("粤A12345", "green_large")[0] is False  # 末位非 D/F
    assert charset.validate_plate_type("粤A12345D", "green_large")[0] is True


def test_special_plate_content_rejected():
    for plate, plate_type in [
        ("京A123挂", "blue"), ("京A1234挂", "blue"),
        ("京A12345学", "blue"), ("京A12345警", "blue"),
        ("京A12345使", "blue"), ("京A12345领", "blue"),
        ("京A12345港", "blue"), ("京A12345澳", "blue"),
        ("京A12345D", "yellow"),   # 黄牌不能 8 位
    ]:
        ok, _reason = charset.validate_plate_type(plate, plate_type)
        assert not ok, (plate, plate_type)


def test_generate_only_uses_legal_city_codes():
    import random
    rng = random.Random(20261025)
    for type_ in charset.PLATE_TYPES:
        for _ in range(100):
            plate = charset.generate_plate_type(type_, rng)
            assert charset.validate_plate_type(plate, type_)[0]
            assert plate[1] in charset.valid_city_codes(plate[0])


def test_external_templates_are_used_for_all_four_types():
    root = EXTERNAL_GENERATOR_ROOT
    for base in ("blue", "yellow", "green_car", "green_truck"):
        assert (root / "plate_model" / f"{base}_140.PNG").exists()
    assert (root / "font_model" / "green_A.jpg").exists()
    assert (root / "generate_multi_plate.py").exists()

    sys.path.insert(0, str(root))
    from generate_multi_plate import MultiPlateGenerator, get_location_data
    generator = MultiPlateGenerator(str(root / "plate_model"), str(root / "font_model"))
    bases = {"blue": "blue", "yellow": "yellow", "green": "green_car",
             "green_large": "green_truck"}
    for type_ in charset.PLATE_TYPES:
        plate = charset.generate_plate_type(type_, __import__("random").Random(9))
        direct = generator.generate_plate_special(plate, bases[type_], False,
                                                  enhance=False)
        rendered = external_render_plate(plate, type_)
        assert rendered.shape == direct.shape
        assert rendered.shape[0] == 140
        assert rendered.shape[1] == (440 if type_ in ("blue", "yellow") else 480)
        assert np.array_equal(rendered, direct)
    # 外部位置函数：7/8 位单层均为 y=25..115，字符宽度 45
    for length in (7, 8):
        xy = get_location_data(length, split_id=2, height=140)
        assert xy.shape == (length, 4)
        assert all(y0 == 25 and y1 == 115 for _, y0, _, y1 in xy)
        for index, (x0, _, x1, _) in enumerate(xy):
            assert x1 - x0 == (45 if length == 7 or index == 0 else 43)


def test_demo_gallery_external_960x640_18_images():
    demo = ROOT / "datasets/demo-lpr"
    manifest = json.loads((demo / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["samples"]) == 18
    counts = {t: sum(1 for s in manifest["samples"] if s["plate_type"] == t)
              for t in charset.PLATE_TYPES}
    assert counts == {"blue": 5, "green": 5, "green_large": 4, "yellow": 4}
    for sample in manifest["samples"]:
        assert sample["width"] == 960 and sample["height"] == 640
        assert (demo / sample["file"]).exists()
        assert charset.validate_plate_type(sample["plate"], sample["plate_type"])[0]
        assert sample["source_kind"] == "synthetic"
        assert sample["pr2_eligible"] is False


def test_web_sim3d_plate_index_matches_demo():
    demo = ROOT / "datasets/demo-lpr"
    manifest = json.loads((demo / "manifest.json").read_text(encoding="utf-8"))
    web_plates = ROOT / "web/sim3d/assets/plates"
    index = json.loads((web_plates / "index.json").read_text(encoding="utf-8"))
    assert index["version"] == 2
    assert set(index["plates"]) == {s["plate"] for s in manifest["samples"]}
    assert len(index["plates"]) == 18
    for sample in manifest["samples"]:
        entry = index["plates"][sample["plate"]]
        assert entry["plate_type"] == sample["plate_type"]
        assert entry["color"] == sample["color"]
        assert entry["crop_size"] in ([440, 140], [480, 140])
        assert (ROOT / "web/sim3d/assets" / entry["file"]).exists()
    web_manifest = json.loads((web_plates / "manifest.json").read_text(encoding="utf-8"))
    assert web_manifest["count"] == 18 and len(web_manifest["plates"]) == 18
