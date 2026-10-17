/**
 * Staff email-code login view logic (V1.1 §2.1/§2.2; plan Task 8) —
 * pure decisions for the StaffEmailCodeLogin island.
 *
 * - the code gate mirrors the backend's 6-digit band;
 * - the resend button rides the SHARED resendButtonLabel family
 *   (errors.ts — the register form's precedent): a text-only countdown,
 *   nothing animates, so the reduced-motion contract holds by
 *   construction;
 * - error copy branches on `error.code` only (§29): EMAIL_CODE_INVALID
 *   is the uniform wrong/expired/exhausted verdict, ACCOUNT_LOCKED
 *   rounds the envelope's retry_after_seconds up to whole minutes,
 *   RATE_LIMITED is the 60s resend cooldown;
 * - classifyVerifyResponse discriminates the T5 frozen contract's two
 *   verify shapes: a full TokenPairResponse body (login complete) vs
 *   the ADMIN {status:"totp_required", pending_token} body (second
 *   leg). Anything else throws — the contract admits exactly two.
 */
import { isApiError } from "@/lib/errors";

import { NETWORK_ERROR_TEXT } from "./errors";

const INVALID_CODE_TEXT = "请输入 6 位数字验证码";

/** The 6-digit band (the backend's LoginEmailChallenge code shape). */
export function validateEmailCode(value: string): string | undefined {
  if (!/^\d{6}$/.test(value)) {
    return INVALID_CODE_TEXT;
  }
  return undefined;
}

/** retry_after_seconds rounded UP to whole minutes, 1-minute floor. */
export function lockedWaitMinutes(retryAfterSeconds: number): number {
  return Math.max(1, Math.ceil(retryAfterSeconds / 60));
}

export interface EmailLoginErrorView {
  message: string;
  requestId: string | null;
}

/** Code-branched copy for the email-code flow (§29: never by status). */
export function describeEmailLoginError(
  error: unknown,
  fallback: string,
): EmailLoginErrorView {
  if (!isApiError(error)) {
    return { message: NETWORK_ERROR_TEXT, requestId: null };
  }
  if (error.code === "EMAIL_CODE_INVALID") {
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

/**
 * The mobile channel's mirror (login-iteration item ①): the same
 * verdict-code branches with the SMS family's invalid-code key — the
 * lockout and cooldown copy are the family's verbatim text.
 */
export function describeMobileStaffLoginError(
  error: unknown,
  fallback: string,
): EmailLoginErrorView {
  if (!isApiError(error)) {
    return { message: NETWORK_ERROR_TEXT, requestId: null };
  }
  if (error.code === "MOBILE_CODE_INVALID") {
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

/** The two verify outcomes the T5 contract defines. */
export type EmailVerifyOutcome =
  | { kind: "tokens" }
  | { kind: "totp_required"; pendingToken: string };

/**
 * Discriminate a 200 verify body: a TokenPairResponse completes the
 * login; the ADMIN {status:"totp_required"} body hands the second leg.
 * The token VALUES are consumed by the api wrapper (which owns the
 * memory-only manager); this classifier only picks the branch.
 */
export function classifyVerifyResponse(body: unknown): EmailVerifyOutcome {
  if (typeof body === "object" && body !== null) {
    const record = body as Record<string, unknown>;
    if (record.status === "totp_required" && typeof record.pending_token === "string") {
      return { kind: "totp_required", pendingToken: record.pending_token };
    }
    if (typeof record.access_token === "string") {
      return { kind: "tokens" };
    }
  }
  throw new Error("email-code verify: unrecognized response body");
}
