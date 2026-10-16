/**
 * Staff email-code login (V1.1 §2.1/§2.2; plan Task 8) — pins for the
 * pure view module `features/auth/staffEmailLoginView.ts`:
 *
 * - the 6-digit code gate mirrors the backend band (client-rejected
 *   values never reach the wire);
 * - the resend button's cooldown label family (text-only countdown —
 *   nothing animates, so the reduced-motion contract holds by
 *   construction);
 * - error copy branches on `error.code` only (§29): EMAIL_CODE_INVALID
 *   is the uniform wrong/expired/exhausted verdict, ACCOUNT_LOCKED
 *   rounds the envelope's retry_after_seconds up to whole minutes,
 *   RATE_LIMITED is the 60s resend cooldown;
 * - the verify-response classifier is the T5 frozen contract's two
 *   shapes: a full TokenPairResponse body vs the ADMIN
 *   {status:"totp_required", pending_token} body.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";

import { resendButtonLabel } from "../features/auth/errors";
import {
  classifyVerifyResponse,
  describeEmailLoginError,
  lockedWaitMinutes,
  validateEmailCode,
} from "../features/auth/staffEmailLoginView";

function apiError(
  code: string,
  message: string,
  status: number,
  details: unknown = null,
): ApiError {
  return new ApiError({ code, message, details, status, requestId: "req-1" });
}

describe("validateEmailCode (the 6-digit band)", () => {
  test("accepts exactly 6 digits", () => {
    assert.equal(validateEmailCode("234567"), undefined);
  });

  test("rejects short/long/non-digit/empty values with actionable copy", () => {
    assert.ok(validateEmailCode("") !== undefined);
    assert.ok(validateEmailCode("23456") !== undefined);
    assert.ok(validateEmailCode("2345678") !== undefined);
    assert.ok(validateEmailCode("23a567") !== undefined);
    assert.equal(
      validateEmailCode("23456"),
      validateEmailCode(""),
    );
  });
});

describe("the resend button rides the shared text-only countdown family", () => {
  test("counts down in seconds while cooling, then the plain resend label", () => {
    assert.equal(resendButtonLabel(60, "重新发送", false), "重新发送（60 秒）");
    assert.equal(resendButtonLabel(1, "重新发送", false), "重新发送（1 秒）");
    assert.equal(resendButtonLabel(0, "重新发送", false), "重新发送");
    assert.equal(resendButtonLabel(0, "重新发送", true), "发送中…");
  });
});

describe("lockedWaitMinutes (retry_after_seconds → whole minutes)", () => {
  test("rounds up with a 1-minute floor", () => {
    assert.equal(lockedWaitMinutes(1), 1);
    assert.equal(lockedWaitMinutes(60), 1);
    assert.equal(lockedWaitMinutes(61), 2);
    assert.equal(lockedWaitMinutes(900), 15);
  });
});

describe("describeEmailLoginError (code-branched copy, §29)", () => {
  test("EMAIL_CODE_INVALID is the uniform wrong/expired/exhausted copy", () => {
    const view = describeEmailLoginError(
      apiError("EMAIL_CODE_INVALID", "验证码错误或已失效", 401),
      "登录失败，请稍后重试",
    );
    assert.equal(view.message, "验证码错误或已过期");
    assert.equal(view.requestId, "req-1");
  });

  test("ACCOUNT_LOCKED names the cooldown minutes from the envelope details", () => {
    const view = describeEmailLoginError(
      apiError("ACCOUNT_LOCKED", "尝试次数过多", 423, {
        retry_after_seconds: 900,
      }),
      "登录失败，请稍后重试",
    );
    assert.equal(view.message, "尝试次数过多，请 15 分钟后再试");
  });

  test("ACCOUNT_LOCKED without details degrades to the no-number copy", () => {
    const view = describeEmailLoginError(
      apiError("ACCOUNT_LOCKED", "尝试次数过多", 423),
      "登录失败，请稍后重试",
    );
    assert.equal(view.message, "尝试次数过多，请稍后再试");
  });

  test("RATE_LIMITED names the 60s resend cooldown", () => {
    const view = describeEmailLoginError(
      apiError("RATE_LIMITED", "请求过于频繁", 429),
      "登录失败，请稍后重试",
    );
    assert.match(view.message, /发送过于频繁/);
  });

  test("other typed refusals surface the backend message; transport failures the network line", () => {
    const typed = describeEmailLoginError(
      apiError("PERMISSION_DENIED", "仅员工账号可用此方式登录", 403),
      "登录失败，请稍后重试",
    );
    assert.equal(typed.message, "仅员工账号可用此方式登录");

    const network = describeEmailLoginError(new TypeError("fetch failed"), "登录失败，请稍后重试");
    assert.equal(network.message, "网络异常，请检查连接后重试");
    assert.equal(network.requestId, null);
  });
});

describe("classifyVerifyResponse (the T5 frozen two-shape contract)", () => {
  test("a full token body is a completed login", () => {
    const outcome = classifyVerifyResponse({
      access_token: "at",
      csrf_token: "ct",
      token_type: "bearer",
    });
    assert.equal(outcome.kind, "tokens");
  });

  test("the ADMIN {status:'totp_required'} body carries the pending token", () => {
    const outcome = classifyVerifyResponse({
      status: "totp_required",
      pending_token: "pending-at",
    });
    assert.deepEqual(outcome, { kind: "totp_required", pendingToken: "pending-at" });
  });

  test("any other shape throws (the contract admits exactly two)", () => {
    assert.throws(() => classifyVerifyResponse({}));
    assert.throws(() => classifyVerifyResponse(null));
    assert.throws(() => classifyVerifyResponse("tokens"));
  });
});
