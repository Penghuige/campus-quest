/**
 * Client-side convenience validation for the auth forms (spec §5.2, §5.3,
 * §5.6; patterns §6: "Client validation improves feedback. It never replaces
 * backend validation").
 *
 * The rules below MIRROR the backend bands so obvious mistakes get instant
 * zh-CN feedback instead of a round trip; the backend stays the single
 * authority (identity/validation.py, core/security.py):
 *
 * - student number: ASCII digits `0-9` only (never `isdigit()`, which accepts
 *   full-width digits), length 6-20, leading zeros preserved;
 * - nickname: at most 16 grapheme clusters ("user-perceived characters" — a
 *   ZWJ-joined emoji family is ONE), counted with `Intl.Segmenter`;
 * - password: 10-128 code points, no composition rules;
 * - SMS OTP code: exactly 6 ASCII digits (backend `_CODE_DIGITS = 6`).
 *
 * Every validator returns a zh-CN error string or `null` when acceptable.
 */

/** Backend `STUDENT_NUMBER_DEFAULT_MIN_LEN` / `..._MAX_LEN`. */
export const STUDENT_NUMBER_MIN_LENGTH = 6;
export const STUDENT_NUMBER_MAX_LENGTH = 20;

/** Backend `NICKNAME_MAX_GRAPHEME_CLUSTERS` (spec §5.3). */
export const NICKNAME_MAX_GRAPHEME_CLUSTERS = 16;

/** Backend `PASSWORD_MIN_LENGTH` / `PASSWORD_MAX_LENGTH` (§5.6 band). */
export const PASSWORD_MIN_LENGTH = 10;
export const PASSWORD_MAX_LENGTH = 128;

/** Backend `_CODE_DIGITS` (identity/otp.py). */
export const OTP_CODE_LENGTH = 6;

const ASCII_DIGITS = /^[0-9]+$/;

/** True when `value` consists solely of ASCII digits 0-9 (spec §5.2). */
export function isAsciiDigitString(value: string): boolean {
  return ASCII_DIGITS.test(value);
}

// `Intl.Segmenter` exists in every target browser and in Node >= 16, but the
// fallback keeps the module usable in exotic runtimes instead of crashing.
let graphemeSegmenter: Intl.Segmenter | null | undefined;

function getGraphemeSegmenter(): Intl.Segmenter | null {
  if (graphemeSegmenter === undefined) {
    graphemeSegmenter =
      typeof Intl !== "undefined" && "Segmenter" in Intl
        ? new Intl.Segmenter("zh-Hans", { granularity: "grapheme" })
        : null;
  }
  return graphemeSegmenter;
}

/**
 * Count user-perceived characters (grapheme clusters), mirroring the
 * backend's `regex` `\X` counting (spec §5.3): a skin-toned or ZWJ-joined
 * emoji sequence counts as one.
 */
export function countGraphemes(value: string): number {
  const segmenter = getGraphemeSegmenter();
  if (segmenter !== null) {
    return [...segmenter.segment(value)].length;
  }
  // Fallback: code points (closer than UTF-16 units; still a lower bound on
  // grapheme correctness — only reachable without Intl.Segmenter).
  return countCodePoints(value);
}

/** Length in Unicode code points — the unit Python's `len()` uses. */
export function countCodePoints(value: string): number {
  return [...value].length;
}

/** Student-number convenience rule; returns the error text or `null`. */
export function validateStudentNumber(value: string): string | null {
  if (value.length === 0) {
    return "请输入学号";
  }
  if (!isAsciiDigitString(value)) {
    return "学号只能包含 0-9 的数字";
  }
  if (
    value.length < STUDENT_NUMBER_MIN_LENGTH ||
    value.length > STUDENT_NUMBER_MAX_LENGTH
  ) {
    return `学号需为 ${STUDENT_NUMBER_MIN_LENGTH}-${STUDENT_NUMBER_MAX_LENGTH} 位数字`;
  }
  return null;
}

/** Nickname convenience rule (grapheme cap; visually-empty is backend's call). */
export function validateNickname(value: string): string | null {
  if (value.trim().length === 0) {
    return "请输入昵称";
  }
  const count = countGraphemes(value);
  if (count > NICKNAME_MAX_GRAPHEME_CLUSTERS) {
    return `昵称最长 ${NICKNAME_MAX_GRAPHEME_CLUSTERS} 个字符（当前 ${count} 个）`;
  }
  return null;
}

/** Password convenience rule: the 10-128 code-point band (spec §5.6). */
export function validatePassword(value: string): string | null {
  if (value.length === 0) {
    return "请输入密码";
  }
  const length = countCodePoints(value);
  if (length < PASSWORD_MIN_LENGTH || length > PASSWORD_MAX_LENGTH) {
    return `密码长度需为 ${PASSWORD_MIN_LENGTH}-${PASSWORD_MAX_LENGTH} 个字符`;
  }
  return null;
}

/** SMS OTP code convenience rule: exactly 6 ASCII digits. */
export function validateOtpCode(value: string): string | null {
  if (value.length === 0) {
    return "请输入短信验证码";
  }
  if (!isAsciiDigitString(value) || value.length !== OTP_CODE_LENGTH) {
    return `验证码为 ${OTP_CODE_LENGTH} 位数字`;
  }
  return null;
}

/**
 * Phone convenience rule. The backend normalizes to E.164 with the
 * configured default region (CN) and owns the real validation
 * (`otp.InvalidPhoneError`); this only catches obvious typos so the
 * OTP button does not fire on garbage.
 */
export function validatePhone(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    return "请输入手机号";
  }
  if (!/^\+?[0-9]{5,20}$/.test(trimmed)) {
    return "请输入正确的手机号";
  }
  return null;
}

/** Username convenience rule for the login/reset forms (student number). */
export function validateUsername(value: string): string | null {
  return validateStudentNumber(value);
}
