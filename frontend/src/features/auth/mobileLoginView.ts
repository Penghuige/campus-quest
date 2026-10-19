/**
 * Student mobile-code login (auth-extension-II spec §3/§4) — pure
 * decisions for the login panel and the pending-approval surfaces.
 *
 * - the mobile panel mirrors the staff email-code channel's shape
 *   (60s resend cooldown text family, 6-digit band) — students have NO
 *   second factor, so verify answers a full login or nothing;
 * - a password-channel 401 on a passwordless account must GUIDE to the
 *   mobile tab, not read as a wrong password (spec §4);
 * - the PENDING_APPROVAL banner copy and the APPROVAL_PENDING freeze
 *   copy are the typed-error-code mappings (spec §2).
 */
import { isApiError } from "@/lib/errors";

import { NETWORK_ERROR_TEXT } from "./errors";

/** Validate the phone band for the mobile-login tab (E.164 CN shape). */
export function validateMobileLoginPhone(value: string): string | undefined {
  const trimmed = value.trim();
  if (!/^1[3-9]\d{9}$/.test(trimmed)) {
    return "请输入 11 位大陆手机号";
  }
  return undefined;
}

/** The 6-digit band (shared with the staff email-code mirror). */
export function validateMobileCode(value: string): string | undefined {
  if (!/^\d{6}$/.test(value.trim())) {
    return "请输入 6 位数字验证码";
  }
  return undefined;
}

export interface MobileLoginErrorView {
  message: string;
  /** True when the honest next step is the mobile tab (spec §4). */
  suggestMobileTab: boolean;
  requestId: string | null;
}

/**
 * The mobile tab's error copy, branched on `error.code` (§29). The
 * password tab's wrong-credentials 401 on a passwordless account is
 * indistinguishable from a wrong password client-side — the uniform
 * copy carries the mobile-channel guidance for EVERYONE (no
 * enumeration), and the suggestMobileTab flag flips the panel's accent.
 */
export function describeMobileLoginError(
  error: unknown,
  fallback: string,
): MobileLoginErrorView {
  if (!isApiError(error)) {
    return { message: NETWORK_ERROR_TEXT, suggestMobileTab: false, requestId: null };
  }
  if (error.code === "MOBILE_CODE_INVALID") {
    return {
      message: "验证码错误或已过期",
      suggestMobileTab: false,
      requestId: error.requestId,
    };
  }
  if (error.code === "RATE_LIMITED") {
    return {
      message: "发送过于频繁，请 1 分钟后再试",
      suggestMobileTab: false,
      requestId: error.requestId,
    };
  }
  if (error.code === "ACCOUNT_LOCKED") {
    return {
      message: lockedCopy(error),
      suggestMobileTab: false,
      requestId: error.requestId,
    };
  }
  if (error.code === "AUTHENTICATION_REQUIRED") {
    // The password channel's uniform refusal. For a passwordless
    // account this is the ONLY outcome — guide to the mobile tab.
    return {
      message: "学号、手机号或密码不正确；未设置密码的账号请使用手机验证码登录，或先在设置中设置密码",
      suggestMobileTab: true,
      requestId: error.requestId,
    };
  }
  return { message: error.message || fallback, suggestMobileTab: false, requestId: error.requestId };
}

function lockedCopy(error: { details: unknown }): string {
  if (
    typeof error.details === "object" &&
    error.details !== null &&
    "retry_after_seconds" in error.details
  ) {
    const raw = (error.details as { retry_after_seconds: unknown }).retry_after_seconds;
    if (typeof raw === "number" && raw > 0) {
      return `尝试次数过多，请 ${Math.max(1, Math.ceil(raw / 60))} 分钟后再试`;
    }
  }
  return "尝试次数过多，请稍后再试";
}

/** The freeze copy: every APPROVAL_PENDING 403 lands here (spec §2). */
export function describeApprovalPendingError(error: unknown): string | null {
  if (isApiError(error) && error.code === "APPROVAL_PENDING") {
    return "账号待管理员审核，审核通过后即可使用此功能；期间可浏览任务与榜单";
  }
  return null;
}

/** True when /me says the account awaits approval (banner condition). */
export function isPendingApproval(status: string): boolean {
  return status === "PENDING_APPROVAL";
}
