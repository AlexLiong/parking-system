"""车牌字符集与规则校验单元测试（lpr/charset.py）。"""
from __future__ import annotations

from server.lpr import charset


def test_lengths_and_sets():
    assert charset.BLUE_LEN == 7
    assert charset.GREEN_LEN == 8
    assert len(charset.PROVINCES) == 31
    # A–Z 去 I/O = 24
    assert len(charset.LETTERS) == 24
    assert "I" not in charset.LETTERS and "O" not in charset.LETTERS
    # 序号位 10 数字 + 24 字母 = 34
    assert len(charset.ALNUM) == 34


def test_validate_grammar_blue_ok():
    ok, reason = charset.validate_grammar("琼A12345", "blue")
    assert ok, reason


def test_validate_grammar_green_ok():
    # 小型新能源：序号首位 D/F
    ok, _ = charset.validate_grammar("浙FD12345", "green")
    assert ok


def test_validate_grammar_wrong_length():
    ok, reason = charset.validate_grammar("琼A1234", "blue")
    assert not ok and "长度" in reason


def test_validate_grammar_bad_province():
    ok, reason = charset.validate_grammar("ZA12345", "blue")
    assert not ok and "省份" in reason


def test_validate_grammar_bad_letter_position():
    # 第 2 位不允许 I/O
    ok, reason = charset.validate_grammar("琼I12345", "blue")
    assert not ok


def test_validate_grammar_green_needs_df():
    ok, reason = charset.validate_grammar("浙A123456", "green")
    assert not ok and "D/F" in reason


def test_expected_len():
    assert charset.expected_len("green") == 8
    assert charset.expected_len("blue") == 7
    assert charset.expected_len(None) == 7


def test_char_set_for_position():
    assert charset.char_set_for_position(0, "blue") == charset.PROVINCE_SET
    assert charset.char_set_for_position(1, "blue") == charset.LETTER_SET
    assert charset.char_set_for_position(2, "blue") == charset.ALNUM_SET
    # 绿牌序号首位并入 D/F
    s = charset.char_set_for_position(2, "green")
    assert "D" in s and "F" in s


def test_confusable_pairs():
    assert charset.is_confusable("0", "O")
    assert charset.is_confusable("1", "7")
    assert not charset.is_confusable("A", "H")


def test_is_ne_green():
    assert charset.is_ne_green("浙FD12345")
    assert not charset.is_ne_green("浙A123456")
