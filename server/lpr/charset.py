"""车牌字符集与号牌语法规则（lpr/charset.py）。

对应 GA 36-2018 中国大陆号牌：
- 蓝牌 7 位：省份汉字 + 发牌机关字母 + 5 位序号；
- 绿牌 8 位：省份汉字 + 发牌机关字母 + 6 位序号（小型新能源序号首位为 D/F）。

这些硬约束是"易混字符仲裁"（RR-1.2）与规则校验（FR-1.2）的基础：
利用位置语法可直接排除非法候选（如序号位出现汉字、第 2 位出现 O/I）。
"""
from __future__ import annotations

import json
from pathlib import Path

# 31 个省级行政区简称（不含使/领等特殊牌，见需求 §9 项目边界）
PROVINCES = "京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川贵云藏陕甘青宁新"
PROVINCE_SET = set(PROVINCES)

# 发牌机关字母：A–Z 去掉 I、O（易与 1、0 混淆，规范中不使用）
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LETTERS = "".join(c for c in _UPPER if c not in "IO")
LETTER_SET = set(LETTERS)

DIGITS = "0123456789"
# 序号位字母数字：0–9 + A–Z 去 I、O = 34 类
ALNUM = DIGITS + LETTERS
ALNUM_SET = set(ALNUM)

# 新能源小型车序号首位（D=纯电，F=非纯电/混动）
NE_FIRST = "DF"

# 易混字符对（专项仲裁触发集合，RR-1.2）
CONFUSABLE_PAIRS = {
    frozenset(("0", "O")),
    frozenset(("1", "7")),
    frozenset(("8", "B")),
    frozenset(("2", "Z")),
    frozenset(("5", "S")),
    frozenset(("4", "A")),
    frozenset(("7", "T")),
    frozenset(("6", "G")),
    frozenset(("9", "P")),
    frozenset(("D", "0")),
}

BLUE_LEN = 7
GREEN_LEN = 8


def is_confusable(a: str, b: str) -> bool:
    return frozenset((a, b)) in CONFUSABLE_PAIRS


def char_set_for_position(pos: int, color: str) -> set[str]:
    """返回某位置的合法字符集（硬约束）。"""
    if pos == 0:
        return PROVINCE_SET
    if pos == 1:
        return LETTER_SET
    # 序号位
    if color == "green" and pos == 2:
        # 新能源小型车首位 D/F；也兼容"末位 D/F"的大型车布局，故并入 alnum 由仲裁决定
        return set(NE_FIRST) | ALNUM_SET
    return ALNUM_SET


def expected_len(color: str | None) -> int:
    return GREEN_LEN if color == "green" else BLUE_LEN


def validate_grammar(plate: str, color: str) -> tuple[bool, str]:
    """按位置语法校验号牌字符串，返回 (是否合法, 原因)。"""
    if not plate:
        return False, "空车牌"
    n = expected_len(color)
    if len(plate) != n:
        return False, f"长度应为 {n}，实际 {len(plate)}"
    if plate[0] not in PROVINCE_SET:
        return False, f"首位 '{plate[0]}' 非省份汉字"
    if plate[1] not in LETTER_SET:
        return False, f"第 2 位 '{plate[1]}' 非合法发牌机关字母"
    for i in range(2, n):
        if plate[i] not in ALNUM_SET:
            return False, f"序号位 '{plate[i]}' 非法"
    if color == "green":
        # 小型新能源：首位序号 D/F；或末位序号 D/F（大型车布局）
        if plate[2] not in NE_FIRST and plate[-1] not in NE_FIRST:
            return False, "绿牌序号缺少 D/F 新能源标识位"
    return True, ""


def normalize_valid_plate(raw: str, color: str | None = None) -> tuple[str, str]:
    """Normalize a mainland blue/green plate or raise ValueError with a reason."""
    plate = raw.strip().upper().replace("·", "")
    normalized_color = color.strip().lower() if color is not None else (
        "green" if len(plate) == GREEN_LEN else "blue")
    if normalized_color not in ("blue", "green"):
        raise ValueError("仅支持蓝牌或新能源绿牌")
    valid, reason = validate_grammar(plate, normalized_color)
    if not valid:
        raise ValueError(reason)
    return plate, normalized_color


def is_ne_green(plate: str) -> bool:
    """判断绿牌是否为小型新能源布局（序号首位 D/F）。"""
    return len(plate) == GREEN_LEN and plate[2] in NE_FIRST


# ===========================================================================
# 统一 plate_type（演示生成 / 校验；blue/green 仍被识别链路兼容使用）
# ===========================================================================
PLATE_TYPES = ("blue", "green", "green_large", "yellow")

# 省份 → 完整合法发牌机关字母（31 省/直辖市；排除 I/O，广东省 Z 等特殊入口
# 车辆码不列入常规生成表）。数据来自同目录 plate_city_codes.json，便于维护。
_CITY_CODES_PATH = Path(__file__).with_name("plate_city_codes.json")
_CITY_CODES_RAW: dict[str, str] = json.loads(
    _CITY_CODES_PATH.read_text(encoding="utf-8"))
PROVINCE_CITY_CODES: dict[str, frozenset[str]] = {}
for _province in PROVINCES:
    _codes = _CITY_CODES_RAW.get(_province)
    if not _codes:
        raise RuntimeError(f"plate_city_codes.json 缺少省份 {_province}")
    if set(_codes) & set("IO"):
        raise RuntimeError(f"plate_city_codes.json 省份 {_province} 含非法 I/O")
    if any(ch not in LETTER_SET for ch in _codes):
        raise RuntimeError(f"plate_city_codes.json 省份 {_province} 含非 A–Z 字符")
    PROVINCE_CITY_CODES[_province] = frozenset(_codes)

# plate_type → 展示/渲染属性；vehicle_kind 对齐 3D 现有车型族（sedan/hatch/van）。
PLATE_TYPE_SPECS: dict[str, dict] = {
    "blue": {"length": BLUE_LEN, "color": "blue", "vehicle_kind": "sedan",
             "label": "蓝牌（普通小型车）", "new_energy": False},
    "green": {"length": GREEN_LEN, "color": "green", "vehicle_kind": "hatch",
              "label": "小型新能源绿牌", "new_energy": True},
    "green_large": {"length": GREEN_LEN, "color": "green", "vehicle_kind": "van",
                    "label": "大型新能源黄绿双拼牌", "new_energy": True},
    "yellow": {"length": BLUE_LEN, "color": "yellow", "vehicle_kind": "van",
               "label": "黄牌（大型车）", "new_energy": False},
}


def normalize_plate_type(value: str | None) -> str:
    """规范化 plate_type，非法类型抛 ValueError（不随机猜测）。"""
    if value is None:
        raise ValueError("缺少 plate_type（blue/green/green_large/yellow）")
    normalized = str(value).strip().lower()
    if normalized not in PLATE_TYPES:
        raise ValueError(f"plate_type 必须为 {'/'.join(PLATE_TYPES)}，实际 {value!r}")
    return normalized


def plate_type_spec(plate_type: str) -> dict:
    return dict(PLATE_TYPE_SPECS[normalize_plate_type(plate_type)])


def valid_city_codes(province: str) -> frozenset[str]:
    return PROVINCE_CITY_CODES.get(province, frozenset())


def city_code_valid(province: str, city_letter: str) -> bool:
    return city_letter in valid_city_codes(province)


def validate_plate_type(plate: str, plate_type: str) -> tuple[bool, str]:
    """严格按 plate_type + 省份/发牌机关城市码校验（用于生成/API/剧本）。

    识别链路的 ``validate_grammar`` 保持旧兼容；本函数是演示生成/API 的严格入口。
    """
    try:
        plate_type = normalize_plate_type(plate_type)
    except ValueError as exc:
        return False, str(exc)
    spec = PLATE_TYPE_SPECS[plate_type]
    n = int(spec["length"])
    if not plate:
        return False, "空车牌"
    if len(plate) != n:
        return False, f"长度应为 {n}，实际 {len(plate)}"
    if plate[0] not in PROVINCE_SET:
        return False, f"首位 '{plate[0]}' 非省份汉字"
    if plate[1] not in LETTER_SET:
        return False, f"第 2 位 '{plate[1]}' 非合法发牌机关字母"
    if not city_code_valid(plate[0], plate[1]):
        return False, (f"省份 {plate[0]} 无发牌机关城市码 {plate[1]}"
                       f"（合法：{''.join(sorted(valid_city_codes(plate[0]))) or '无'}）")
    for i in range(2, n):
        if plate[i] not in ALNUM_SET:
            return False, f"序号位 '{plate[i]}' 非法"
    if plate_type == "green" and plate[2] not in NE_FIRST:
        return False, "小型新能源绿牌序号首位必须为 D/F"
    if plate_type == "green_large" and plate[-1] not in NE_FIRST:
        return False, "大型新能源绿牌序号末位必须为 D/F"
    return True, ""


def normalize_plate_type_plate(raw: str, plate_type: str) -> tuple[str, str]:
    """规范化并严格校验某 plate_type 的车牌；失败抛 ValueError(带 reason)。"""
    normalized_type = normalize_plate_type(plate_type)
    plate = str(raw or "").strip().upper().replace("·", "")
    valid, reason = validate_plate_type(plate, normalized_type)
    if not valid:
        raise ValueError(reason)
    return plate, normalized_type


def generate_plate_type(plate_type: str, rng=None, province: str | None = None) -> str:
    """生成严格合法的演示车牌（城市码从映射取，不再随机字母）。"""
    import random as _random
    normalized_type = normalize_plate_type(plate_type)
    spec = PLATE_TYPE_SPECS[normalized_type]
    rng = rng or _random.Random()
    if province is not None:
        if province not in PROVINCE_SET:
            raise ValueError(f"未知省份: {province!r}")
        provinces = [province]
    else:
        provinces = list(PROVINCES)
    province = rng.choice(provinces)
    city_codes = sorted(valid_city_codes(province)) or ["A"]
    city = rng.choice(city_codes)
    length = int(spec["length"])
    if normalized_type == "green":
        body = rng.choice(NE_FIRST) + "".join(
            rng.choice(ALNUM) for _ in range(length - 3))
    elif normalized_type == "green_large":
        body = "".join(rng.choice(ALNUM) for _ in range(length - 3)) + rng.choice(NE_FIRST)
    else:
        body = "".join(rng.choice(ALNUM) for _ in range(length - 2))
    plate = province + city + body
    ok, reason = validate_plate_type(plate, normalized_type)
    if not ok:
        raise ValueError(f"生成车牌校验失败: {plate}: {reason}")
    return plate

