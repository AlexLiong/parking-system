"""规则校验与易混字符仲裁测试（lpr/validator.py, RR-1.2）。

验证三层策略：位置语法硬约束过滤、专项仲裁（结合在场上下文）、综合置信度门槛。
"""
from __future__ import annotations

from server.core.domain import PlateContext
from server.lpr.validator import PlateValidator


def _opt(char, conf):
    return (char, conf)


def test_happy_path_valid_blue():
    v = PlateValidator()
    chars = [
        [_opt("琼", 0.99)], [_opt("A", 0.98)], [_opt("1", 0.97)],
        [_opt("2", 0.96)], [_opt("3", 0.95)], [_opt("4", 0.94)], [_opt("5", 0.93)],
    ]
    r = v.validate(chars, "blue")
    assert r.valid is True
    assert r.plate == "琼A12345"
    assert r.overall_conf >= v.conf_threshold
    assert r.suspected is False


def test_position_hard_constraint_filters_illegal_candidate():
    # 序号位（pos=2）给出汉字 top1，应被硬约束过滤，退回合法 alnum
    v = PlateValidator()
    chars = [
        [_opt("琼", 0.99)], [_opt("A", 0.98)],
        [_opt("琼", 0.995), _opt("1", 0.90)],   # 汉字非法 → 过滤后选 "1"
        [_opt("2", 0.96)], [_opt("3", 0.95)], [_opt("4", 0.94)], [_opt("5", 0.93)],
    ]
    r = v.validate(chars, "blue")
    assert r.plate == "琼A12345"
    assert r.valid is True


def test_second_position_excludes_I_O():
    # 发牌机关字母位不含 I/O，若识别给出 O 应被过滤
    v = PlateValidator()
    chars = [
        [_opt("琼", 0.99)],
        [_opt("O", 0.99), _opt("A", 0.95)],     # O 非法 → 选 A
        [_opt("1", 0.97)], [_opt("2", 0.96)], [_opt("3", 0.95)],
        [_opt("4", 0.94)], [_opt("5", 0.93)],
    ]
    r = v.validate(chars, "blue")
    assert r.plate == "琼A12345"
    assert r.valid is True


def test_confusable_resolved_by_in_lot_context():
    # pos=2 的 1/7 置信度接近且易混，出场时用在场库仲裁为 7
    v = PlateValidator(disambig_gap=0.15)
    chars = [
        [_opt("琼", 0.99)], [_opt("A", 0.98)],
        [_opt("1", 0.90), _opt("7", 0.88)],     # gap 0.02 < 0.15 且易混
        [_opt("2", 0.96)], [_opt("3", 0.95)], [_opt("4", 0.94)], [_opt("5", 0.93)],
    ]
    ctx = PlateContext(gate="out", in_lot_plates=["琼A72345"])
    r = v.validate(chars, "blue", ctx)
    assert r.plate == "琼A72345"       # 仲裁到在场车牌
    assert r.suspected is True         # 经仲裁仍标记疑似
    assert "琼A72345" in r.candidates  # 解析结果保留在候选首位


def test_confusable_without_context_keeps_top1_but_suspected():
    v = PlateValidator(disambig_gap=0.15)
    chars = [
        [_opt("琼", 0.99)], [_opt("A", 0.98)],
        [_opt("1", 0.90), _opt("7", 0.88)],
        [_opt("2", 0.96)], [_opt("3", 0.95)], [_opt("4", 0.94)], [_opt("5", 0.93)],
    ]
    r = v.validate(chars, "blue", PlateContext(gate="in"))
    assert r.plate == "琼A12345"       # 无在场库 → 保持 top1
    assert r.suspected is True


def test_low_overall_confidence_flagged():
    v = PlateValidator(conf_threshold=0.85)
    chars = [
        [_opt("琼", 0.5)], [_opt("A", 0.5)], [_opt("1", 0.5)],
        [_opt("2", 0.5)], [_opt("3", 0.5)], [_opt("4", 0.5)], [_opt("5", 0.5)],
    ]
    r = v.validate(chars, "blue")
    assert r.plate == "琼A12345"       # 语法仍合法
    assert r.valid is True
    assert r.overall_conf < 0.85
    assert r.reason == "低置信度"


def test_length_mismatch_fails():
    v = PlateValidator()
    chars = [[_opt("琼", 0.9)], [_opt("A", 0.9)], [_opt("1", 0.9)],
             [_opt("2", 0.9)], [_opt("3", 0.9)], [_opt("4", 0.9)]]   # 6 位
    r = v.validate(chars, "blue")
    assert r.valid is False
    assert r.suspected is True
    assert "分割字符数" in r.reason


def test_green_ne_requires_df_marker():
    v = PlateValidator()
    # 绿牌 8 位，序号首位应为 D/F
    chars = [
        [_opt("琼", 0.99)], [_opt("A", 0.98)], [_opt("D", 0.97)],
        [_opt("1", 0.96)], [_opt("2", 0.95)], [_opt("3", 0.94)],
        [_opt("4", 0.93)], [_opt("5", 0.92)],
    ]
    r = v.validate(chars, "green")
    assert r.valid is True
    assert r.plate == "琼AD12345"

    # 缺少 D/F 标识 → 语法非法
    chars2 = [
        [_opt("琼", 0.99)], [_opt("A", 0.98)], [_opt("1", 0.97)],
        [_opt("1", 0.96)], [_opt("2", 0.95)], [_opt("3", 0.94)],
        [_opt("4", 0.93)], [_opt("5", 0.92)],
    ]
    r2 = v.validate(chars2, "green")
    assert r2.valid is False
    assert "D/F" in r2.reason
