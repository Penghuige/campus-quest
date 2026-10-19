/**
 * The unified login page's pure view seam (unified-login spec §4):
 * decisions the page's progressive-disclosure layers mount, kept pure
 * for unit pinning — the identifier field's TWO bands and the
 * challenge family's error copy.
 *
 * - the CODE layer's identifier band is phone|email only: a student
 *   number is a format-layer refusal (spec §3.1 — the code stream
 *   never takes it; the backend's 421/422 mirrors this client band);
 * - the PASSWORD layer widens to the three-state identifier (phone |
 *   email | student number — spec §3.2);
 * - the copy branches on the §29 error code only, verbatim from the
 *   family's existing verdicts: CHALLENGE_CODE_INVALID is ONE uniform
 *   wrong/expired/exhausted verdict across the email and SMS streams
 *   (the unified page never reveals which stream answered).
 */
import { isApiError } from "@/lib/errors";

import { NETWORK_ERROR_TEXT } from "./errors";
import { validateStudentNumber } from "./validation";

const CHALLENGE_IDENTIFIER_TEXT = "请输入手机号或邮箱";
const PASSWORD_IDENTIFIER_TEXT = "请输入手机号、邮箱或学号";
const INVALID_CODE_TEXT = "请输入 6 位数字验证码";

export interface ChallengeErrorView {
  message: string;
  requestId: string | null;
}

/** The code layer's band: a CN mobile OR an email address. */
export function validateChallengeIdentifier(value: string): string | undefined {
  const trimmed = value.trim();
  if (/^1[3-9]\d{9}$/.test(trimmed)) {
    return undefined;
  }
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
    return undefined;
  }
  return CHALLENGE_IDENTIFIER_TEXT;
}

/** The password layer's three-state band (phone | email | student number). */
export function validatePasswordIdentifier(value: string): string | undefined {
  if (validateChallengeIdentifier(value) === undefined) {
    return undefined;
  }
  if (validateStudentNumber(value.trim()) === null) {
    return undefined;
  }
  return PASSWORD_IDENTIFIER_TEXT;
}

/** The 6-digit code band (the challenge family's code shape). */
export function validateChallengeCode(value: string): string | undefined {
  if (!/^\d{6}$/.test(value)) {
    return INVALID_CODE_TEXT;
  }
  return undefined;
}

/** retry_after_seconds rounded UP to whole minutes, 1-minute floor. */
export function lockedWaitMinutes(retryAfterSeconds: number): number {
  return Math.max(1, Math.ceil(retryAfterSeconds / 60));
}

function retryAfterSeconds(error: { details: unknown }): number | null {
  if (
    typeof error.details === "object" &&
    error.details !== null &&
    "retry_after_seconds" in error.details
  ) {
    const value = (error.details as { retry_after_seconds: unknown }).retry_after_seconds;
    if (typeof value === "number" && value > 0) {
      return value;
    }
  }
  return null;
}

/** Code-branched copy for the unified challenge family (§29: by code only). */
export function describeChallengeError(
  error: unknown,
  fallback: string,
): ChallengeErrorView {
  if (!isApiError(error)) {
    return { message: NETWORK_ERROR_TEXT, requestId: null };
  }
  // Contract-first: CHALLENGE_CODE_INVALID joins the generated ErrorCode
  // union when the backend's PR-1 regen lands (the #72 discipline); the
  // widened compare flips to a direct === with it.
  const code = error.code as string;
  if (code === "CHALLENGE_CODE_INVALID") {
    return { message: "验证码错误或已过期", requestId: error.requestId };
  }
  if (error.code === "ACCOUNT_LOCKED") {
    const retry = retryAfterSeconds(error);
    return {
      message:
        retry === null
          ? "尝试次数过多，请稍后再试"
          : `尝试次数过多，请 ${lockedWaitMinutes(retry)} 分钟后再试`,
      requestId: error.requestId,
    };
  }
  if (error.code === "RATE_LIMITED") {
    return { message: "发送过于频繁，请 1 分钟后再试", requestId: error.requestId };
  }
  return { message: error.message || fallback, requestId: error.requestId };
}
