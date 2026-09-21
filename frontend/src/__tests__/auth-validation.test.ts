/**
 * Task 2 (Plan 09): client-side convenience validation mirrors.
 *
 * Pins the backend bands the forms rely on for instant feedback
 * (identity/validation.py, core/security.py):
 * - nickname length counts GRAPHENE clusters via Intl.Segmenter (a ZWJ
 *   family emoji is one user-perceived character, spec §5.3), cap 16;
 * - student numbers are ASCII digits only — full-width digits and padded
 *   input are rejected (spec §5.2; never `isdigit()` semantics);
 * - passwords count CODE POINTS (Python `len()` unit), band 10-128 — an
 *   astral-plane repeat of 10 chars is 20 UTF-16 units but only 10 code
 *   points and must PASS, while 5 emoji (10 UTF-16 units) must FAIL;
 * - OTP code is exactly 6 ASCII digits.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  countCodePoints,
  countGraphemes,
  isAsciiDigitString,
  validateNickname,
  validateOtpCode,
  validatePassword,
  validatePhone,
  validateStudentNumber,
  NICKNAME_MAX_GRAPHEME_CLUSTERS,
} from "../features/auth/validation";

/** ZWJ-joined family: 4 emoji + 3 ZWJ = 7 code points, ONE grapheme. */
const FAMILY_EMOJI = "\u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}";
/** Thumbs-up + skin tone: 2 code points, ONE grapheme. */
const SKIN_TONE = "\u{1F44D}\u{1F3FD}";
/** Regional-indicator flag: 2 code points, ONE grapheme. */
const FLAG = "\u{1F1E8}\u{1F1F3}";
/** e + combining acute: 2 code points, ONE grapheme. */
const COMBINING = "é";

describe("grapheme counting (Intl.Segmenter)", () => {
  test("emoji sequences collapse to single user-perceived characters", () => {
    assert.equal(countCodePoints(FAMILY_EMOJI), 7);
    assert.equal(countGraphemes(FAMILY_EMOJI), 1);
    assert.equal(countCodePoints(SKIN_TONE), 2);
    assert.equal(countGraphemes(SKIN_TONE), 1);
    assert.equal(countCodePoints(FLAG), 2);
    assert.equal(countGraphemes(FLAG), 1);
    assert.equal(countCodePoints(COMBINING), 2);
    assert.equal(countGraphemes(COMBINING), 1);
  });

  test("plain CJK and ASCII count one cluster per character", () => {
    assert.equal(countGraphemes("小明同学"), 4);
    assert.equal(countGraphemes("abcd"), 4);
    assert.equal(countGraphemes(""), 0);
  });

  test("grapheme counting is the unit the nickname cap uses, not code points", () => {
    // 16 family emoji = 112 code points but exactly 16 graphemes: accepted.
    const sixteen = FAMILY_EMOJI.repeat(16);
    assert.equal(countGraphemes(sixteen), NICKNAME_MAX_GRAPHEME_CLUSTERS);
    assert.equal(validateNickname(sixteen), null);
    // One more grapheme is over the cap.
    const seventeen = sixteen + FAMILY_EMOJI;
    assert.equal(countGraphemes(seventeen), 17);
    assert.match(validateNickname(seventeen) ?? "", /17/);
  });
});

describe("nickname validation", () => {
  test("accepts mixed CJK, emoji, and whitespace-inside input", () => {
    assert.equal(validateNickname("小明 " + SKIN_TONE), null);
    assert.equal(validateNickname("好好学习"), null);
  });

  test("rejects empty and whitespace-only nicknames", () => {
    assert.match(validateNickname("") ?? "", /请输入昵称/);
    assert.match(validateNickname("   ") ?? "", /请输入昵称/);
  });

  test("boundary: 16 clusters pass, 17 fail with the current count", () => {
    assert.equal(validateNickname("小".repeat(16)), null);
    const error = validateNickname("小".repeat(17));
    assert.match(error ?? "", /17/);
  });
});

describe("student number validation (ASCII digits, spec §5.2)", () => {
  test("accepts 6-20 ASCII digits and preserves leading zeros", () => {
    assert.equal(validateStudentNumber("123456"), null);
    assert.equal(validateStudentNumber("12345678901234567890"), null);
    assert.equal(validateStudentNumber("000123"), null);
    assert.equal(isAsciiDigitString("000123"), true);
  });

  test("rejects lengths outside 6-20", () => {
    assert.match(validateStudentNumber("12345") ?? "", /6-20/);
    assert.match(validateStudentNumber("123456789012345678901") ?? "", /6-20/);
  });

  test("rejects full-width digits — never isdigit() semantics", () => {
    assert.equal(isAsciiDigitString("１２３４５６"), false);
    assert.match(validateStudentNumber("１２３４５６") ?? "", /数字/);
  });

  test("rejects spaces, plus signs, hyphens, and letters", () => {
    for (const bad of ["123 456", "+8613800138000", "2024-001", "12345a"]) {
      assert.notEqual(validateStudentNumber(bad), null, bad);
    }
  });
});

describe("password validation (10-128 code points)", () => {
  test("band boundaries in ASCII", () => {
    assert.notEqual(validatePassword("a".repeat(9)), null);
    assert.equal(validatePassword("a".repeat(10)), null);
    assert.equal(validatePassword("a".repeat(128)), null);
    assert.notEqual(validatePassword("a".repeat(129)), null);
  });

  test("counts code points, not UTF-16 units (mirrors Python len())", () => {
    // 10 astral emoji = 20 UTF-16 units but 10 code points: inside the band.
    assert.equal(validatePassword("\u{1F600}".repeat(10)), null);
    // 5 astral emoji = 10 UTF-16 units but only 5 code points: too short.
    assert.notEqual(validatePassword("\u{1F600}".repeat(5)), null);
    assert.notEqual(validatePassword("\u{1F600}".repeat(129)), null);
  });

  test("rejects empty input", () => {
    assert.match(validatePassword("") ?? "", /请输入密码/);
  });
});

describe("OTP code and phone convenience rules", () => {
  test("code must be exactly 6 ASCII digits", () => {
    assert.equal(validateOtpCode("123456"), null);
    assert.notEqual(validateOtpCode("12345"), null);
    assert.notEqual(validateOtpCode("1234567"), null);
    assert.notEqual(validateOtpCode("12345a"), null);
    assert.notEqual(validateOtpCode(""), null);
  });

  test("phone accepts plain and +prefixed digit strings, rejects garbage", () => {
    assert.equal(validatePhone("13800138000"), null);
    assert.equal(validatePhone("+8613800138000"), null);
    assert.notEqual(validatePhone("abc"), null);
    assert.notEqual(validatePhone(""), null);
    assert.notEqual(validatePhone("138-0013-8000"), null);
  });
});
