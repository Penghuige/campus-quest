# backend/tests/unit/identity/test_validation.py
"""Unit tests for identity input validation (spec §5.2 student number, §5.3 nickname).

Grapheme-cluster expectations (16 user-perceived characters, not code points
and not bytes) are exercised with CJK, ZWJ-joined family emoji, combining
marks, and mixed strings, per spec §5.3's requirement to avoid byte/codepoint
miscounting.
"""

import pytest
import regex

from app.modules.identity.validation import normalize_nickname, validate_student_number

# Four emoji joined by ZWJ: 7 code points, exactly 1 grapheme cluster.
FAMILY = "👨\N{ZWJ}👩\N{ZWJ}👧\N{ZWJ}👦"

CJK_TEN = "一二三四五六七八九十"

ZWJ = "\N{ZWJ}"


def _person_chain(count: int) -> str:
    """``count`` person glyphs joined into ONE grapheme cluster by ZWJ.

    UAX #29 GB11 keeps the whole chain a single user-perceived character,
    while the code points total ``2 * count - 1``.
    """
    return "👨" + (ZWJ + "👨") * (count - 1)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("20250010001", True),
        ("000123456", True),
        ("２０２５００１", False),
        ("2025 001", False),
        ("2025-001", False),
        ("1e10", False),
        ("", False),
    ],
)
def test_student_number_format(value, ok):
    if ok:
        assert validate_student_number(value) == value
    else:
        with pytest.raises(ValueError):
            validate_student_number(value)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("12345", False),  # 5 digits: below default min_len
        ("123456", True),  # 6 digits: min boundary
        ("12345678901234567890", True),  # 20 digits: max boundary
        ("123456789012345678901", False),  # 21 digits: above max_len
        ("１２３４５６", False),  # full-width digits: length ok, format must fail
    ],
)
def test_student_number_length_and_fullwidth_boundaries(value, ok):
    if ok:
        assert validate_student_number(value) == value
    else:
        with pytest.raises(ValueError):
            validate_student_number(value)


def test_student_number_length_bounds_are_configurable():
    assert validate_student_number("123", min_len=3, max_len=3) == "123"
    with pytest.raises(ValueError):
        validate_student_number("1234", min_len=3, max_len=3)
    with pytest.raises(ValueError):
        validate_student_number("12", min_len=3, max_len=3)


def test_student_number_preserves_leading_zeros():
    # Leading zeros must survive: the number is never coerced to int (spec §5.2).
    assert validate_student_number("000123456") == "000123456"


def test_student_number_does_not_strip_outer_whitespace():
    # Trimming outer whitespace at login is the caller's job (spec §5.2); the
    # validator itself must reject a padded number.
    with pytest.raises(ValueError):
        validate_student_number(" 20250010001 ")


def test_nickname_accepts_16_chinese_characters():
    nickname = CJK_TEN + "一二三四五六"  # 10 + 6 = 16 grapheme clusters
    assert normalize_nickname(nickname) == nickname


def test_nickname_rejects_17_chinese_characters():
    nickname = CJK_TEN + "一二三四五六七"  # 10 + 7 = 17 grapheme clusters
    with pytest.raises(ValueError):
        normalize_nickname(nickname)


def test_nickname_accepts_16_zwj_family_emoji():
    nickname = FAMILY * 16  # 16 grapheme clusters but 112 code points
    assert normalize_nickname(nickname) == nickname


def test_nickname_rejects_17_zwj_family_emoji():
    with pytest.raises(ValueError):
        normalize_nickname(FAMILY * 17)


def test_nickname_mixed_cjk_and_emoji_counts_grapheme_clusters():
    # 15 CJK characters + 1 ZWJ family emoji = 16 grapheme clusters but 22
    # code points: must pass, proving grapheme (not codepoint) counting.
    nickname = CJK_TEN + "一二三四五" + FAMILY
    assert normalize_nickname(nickname) == nickname


def test_nickname_mixed_cjk_and_emoji_over_limit_rejected():
    with pytest.raises(ValueError):
        normalize_nickname(FAMILY * 16 + "字")  # 17 grapheme clusters


def test_nickname_accepts_16_combining_mark_sequences():
    # "e" + combining acute accent: 2 code points, 1 grapheme cluster each;
    # 32 code points total must still fit the 16-grapheme limit.
    nickname = "é" * 16
    assert normalize_nickname(nickname) == nickname


def test_nickname_trims_outer_whitespace():
    assert normalize_nickname("  汪小汪\t\n") == "汪小汪"


def test_nickname_keeps_inner_whitespace():
    assert normalize_nickname("汪 小汪") == "汪 小汪"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("汪\x00小\x1f汪", "汪小汪"),  # Cc control characters
        ("汪­小汪", "汪小汪"),  # soft hyphen: format character (Cf)
        ("汪​小汪", "汪小汪"),  # zero-width space: format character (Cf)
    ],
)
def test_nickname_removes_invisible_characters_inside(value, expected):
    assert normalize_nickname(value) == expected


def test_nickname_rejects_whitespace_only():
    with pytest.raises(ValueError):
        normalize_nickname("   ")


def test_nickname_rejects_control_characters_only():
    with pytest.raises(ValueError):
        normalize_nickname("\x00\x0b\x7f")


# Storage-width cap (whole-branch review fix): grapheme counting alone lets
# a single multi-hundred-code-point cluster through; users.nickname is
# varchar(255), so the INSERT would fail with a 500 AFTER the registration
# OTP token is already consumed (spec §5.3 read together with the §35 column).


def test_nickname_rejects_single_grapheme_beyond_storage_width():
    # 130 person glyphs ZWJ-joined: 1 grapheme cluster, 259 code points —
    # passes the 16-grapheme rule yet cannot fit varchar(255).
    nickname = _person_chain(130)
    assert len(nickname) == 259
    assert len(regex.findall(r"\X", nickname)) == 1
    with pytest.raises(ValueError):
        normalize_nickname(nickname)


def test_nickname_accepts_255_code_point_boundary():
    # 14 CJK (14 cp) + one skin-tone emoji (2 cp) + a 120-glyph ZWJ chain
    # (239 cp) = exactly the varchar(255) width in 16 grapheme clusters:
    # the code-point cap is inclusive.
    nickname = "一二三四五六七八九十甲乙丙丁" + "👨🏻" + _person_chain(120)
    assert len(nickname) == 255
    assert len(regex.findall(r"\X", nickname)) == 16
    assert normalize_nickname(nickname) == nickname


def test_nickname_rejects_257_code_points_within_grapheme_limit():
    # One glyph more in the chain: 16 graphemes still (so the grapheme rule
    # alone accepts it), 257 code points — over the column width.
    nickname = "一二三四五六七八九十甲乙丙丁" + "👨🏻" + _person_chain(121)
    assert len(nickname) == 257
    assert len(regex.findall(r"\X", nickname)) == 16
    with pytest.raises(ValueError):
        normalize_nickname(nickname)


def test_nickname_rejects_zwj_only_padding():
    # ZWJ survives cleaning (it glues emoji sequences) and consecutive ZWJs
    # merge into one grapheme cluster, so a ZWJ-only string passes both
    # length rules while rendering nothing: visually empty, must reject.
    nickname = ZWJ * 16
    assert len(regex.findall(r"\X", nickname)) == 1
    with pytest.raises(ValueError):
        normalize_nickname(nickname)


def test_nickname_rejects_combining_marks_without_base():
    # Bare combining acute accents (no base letter): every cluster is
    # combining-only, so nothing visible would render.
    with pytest.raises(ValueError):
        normalize_nickname("\N{COMBINING ACUTE ACCENT}" * 16)
