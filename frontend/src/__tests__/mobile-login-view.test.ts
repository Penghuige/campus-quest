/**
 * Student mobile-code login + approval-pending view pins
 * (auth-extension-II spec §2-§4; TDD red-first).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";

import {
  describeApprovalPendingError,
  describeMobileLoginError,
  isPendingApproval,
  validateMobileCode,
  validateMobileLoginPhone,
} from "../features/auth/mobileLoginView";

function apiError(
  code: string,
  message: string,
  status: number,
  details: unknown = null,
): ApiError {
  return new ApiError({ code, message, details, status, requestId: "req-1" });
}

describe("validateMobileLoginPhone / validateMobileCode bands", () => {
  test("CN mobile shape accepted; malformed rejected with copy", () => {
    assert.equal(validateMobileLoginPhone("13800138000"), undefined);
    assert.ok(validateMobileLoginPhone("12345") !== undefined);
    assert.ok(validateMobileLoginPhone("23800138000") !== undefined); // not 1[3-9]
    assert.ok(validateMobileLoginPhone("") !== undefined);
    assert.equal(validateMobileLoginPhone(" 13800138000 "), undefined); // trimmed
  });

  test("6-digit code band", () => {
    assert.equal(validateMobileCode("234567"), undefined);
    assert.ok(validateMobileCode("") !== undefined);
    assert.ok(validateMobileCode("23456") !== undefined);
    assert.ok(validateMobileCode("a23456") !== undefined);
  });
});

describe("describeMobileLoginError (code-branched, §29)", () => {
  test("MOBILE_CODE_INVALID is the uniform code copy", () => {
    const view = describeMobileLoginError(
      apiError("MOBILE_CODE_INVALID", "验证码错误", 401),
      "登录失败",
    );
    assert.equal(view.message, "验证码错误或已过期");
    assert.equal(view.suggestMobileTab, false);
  });

  test("RATE_LIMITED names the 60s cooldown", () => {
    const view = describeMobileLoginError(apiError("RATE_LIMITED", "频繁", 429), "登录失败");
    assert.match(view.message, /发送过于频繁/);
  });

  test("ACCOUNT_LOCKED rounds retry_after_seconds to whole minutes", () => {
    const locked = describeMobileLoginError(
      apiError("ACCOUNT_LOCKED", "次数过多", 423, { retry_after_seconds: 900 }),
      "登录失败",
    );
    assert.equal(locked.message, "尝试次数过多，请 15 分钟后再试");

    const noDetails = describeMobileLoginError(
      apiError("ACCOUNT_LOCKED", "次数过多", 423),
      "登录失败",
    );
    assert.equal(noDetails.message, "尝试次数过多，请稍后再试");
  });

  test("the password channel's 401 GUIDES to the mobile tab (spec §4)", () => {
    const view = describeMobileLoginError(
      apiError("AUTHENTICATION_REQUIRED", "凭证不正确", 401),
      "登录失败",
    );
    assert.equal(view.suggestMobileTab, true);
    assert.match(view.message, /手机验证码/);
    assert.match(view.message, /未设置密码/);
  });

  test("transport failures keep the network line, never the tab flag", () => {
    const view = describeMobileLoginError(new TypeError("fetch failed"), "登录失败");
    assert.equal(view.message, "网络异常，请检查连接后重试");
    assert.equal(view.suggestMobileTab, false);
    assert.equal(view.requestId, null);
  });
});

describe("the approval-pending family (spec §2)", () => {
  test("APPROVAL_PENDING maps to the freeze copy; other errors pass null", () => {
    assert.match(
      describeApprovalPendingError(apiError("APPROVAL_PENDING", "待审核", 403))!,
      /待管理员审核/,
    );
    assert.equal(describeApprovalPendingError(apiError("PERMISSION_DENIED", "无权", 403)), null);
    assert.equal(describeApprovalPendingError(new TypeError("x")), null);
  });

  test("isPendingApproval keys on the /me status string", () => {
    assert.equal(isPendingApproval("PENDING_APPROVAL"), true);
    assert.equal(isPendingApproval("ACTIVE"), false);
    assert.equal(isPendingApproval(""), false);
  });
});
