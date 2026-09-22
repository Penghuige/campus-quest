/**
 * Task 2 (Plan 09): auth error-code branching and cooldown derivation.
 *
 * Pins the §29/patterns-§6 contract for the auth forms:
 * - branching keys on `error.code` ONLY — the Chinese `message` is display
 *   fallback text and never parsed (same code + different messages take the
 *   identical branch; an out-of-surface business code and a truly unknown
 *   code take the documented fallback/generic branches);
 * - `VALIDATION_ERROR` details map onto fields in BOTH backend shapes
 *   (service `{field, reason}` and framework-422 `{field: [msgs]}`) with OUR
 *   stable zh-CN rule text, not the English developer `reason`;
 * - an `OTP_RESEND_COOLDOWN` envelope yields the resend countdown seconds
 *   (server hint when present, the 60s backend default otherwise).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";
import {
  AUTH_ERROR_TEXT,
  cooldownSecondsFrom,
  describeAuthError,
  extractValidationFieldErrors,
  resendButtonLabel,
  withFieldErrorSummary,
} from "../features/auth/errors";

function apiError(
  code: string,
  message: string,
  details: unknown = null,
  requestId: string | null = null,
  status = 400,
): ApiError {
  return new ApiError({ code, message, status, details, requestId });
}

describe("describeAuthError branches by code", () => {
  test("auth-mapped codes get stable zh-CN copy regardless of the message", () => {
    const messages = ["该学号不在注册白名单中，无法注册", "server reworded", ""];
    for (const message of messages) {
      const view = describeAuthError(apiError("STUDENT_NOT_WHITELISTED", message));
      assert.equal(view.summary, AUTH_ERROR_TEXT.STUDENT_NOT_WHITELISTED);
      assert.deepEqual(view.fieldErrors, {});
      assert.equal(view.requestId, null);
    }
  });

  test("login failures: uniform AUTHENTICATION_REQUIRED copy, no user oracle", () => {
    const view = describeAuthError(
      apiError("AUTHENTICATION_REQUIRED", "未登录或登录状态已失效", null, null, 401),
    );
    assert.equal(view.summary, "学号或密码不正确");
    const inactive = describeAuthError(
      apiError("ACCOUNT_NOT_ACTIVE", "账号已停用", null, null, 403),
    );
    assert.equal(inactive.summary, "该账号当前无法登录，请联系管理员");
  });

  test("VALIDATION_ERROR service shape {field, reason} maps to stable field text", () => {
    const view = describeAuthError(
      apiError("VALIDATION_ERROR", "请求参数校验失败", {
        field: "nickname",
        reason: "nickname too long (English developer text)",
      }),
    );
    assert.equal(view.fieldErrors.nickname, "昵称最长 16 个字符");
    assert.equal(view.summary, null); // field errors suffice for the base view
    // The English reason never reaches display material.
    assert.ok(!JSON.stringify(view).includes("developer text"));
  });

  test("VALIDATION_ERROR 422 shape {field: [msgs]} maps the same way", () => {
    const view = describeAuthError(
      apiError(
        "VALIDATION_ERROR",
        "请求参数校验失败",
        { password: ["String should have at least 10 characters"] },
        "req-422",
        422,
      ),
    );
    assert.equal(view.fieldErrors.password, "密码长度需为 10-128 个字符");
    assert.equal(view.summary, null);
  });

  test("VALIDATION_ERROR with an unmapped field falls back to the summary", () => {
    const view = describeAuthError(
      apiError("VALIDATION_ERROR", "请求参数校验失败", { field: "phone_token" }),
    );
    assert.deepEqual(view.fieldErrors, {});
    assert.equal(view.summary, "请求参数校验失败");
  });

  test("withFieldErrorSummary announces field-only failures", () => {
    const base = describeAuthError(
      apiError("VALIDATION_ERROR", "请求参数校验失败", {
        field: "password",
        reason: "band",
      }),
    );
    const announced = withFieldErrorSummary(base);
    assert.ok(announced.summary !== null);
    assert.equal(announced.fieldErrors.password, "密码长度需为 10-128 个字符");
  });

  test("known out-of-surface business code: backend zh message as fallback text", () => {
    // ASSIGNMENT_LIMIT_REACHED-style pin: a code this surface has no mapping
    // for never crashes and never invents branches — the backend's own
    // Chinese copy is the sanctioned fallback display text.
    const view = describeAuthError(
      apiError(
        "ASSIGNMENT_LIMIT_REACHED",
        "当前进行中的任务已达到上限",
        { limit: 3 },
        null,
        409,
      ),
    );
    assert.equal(view.summary, "当前进行中的任务已达到上限");
    assert.deepEqual(view.fieldErrors, {});
    assert.equal(view.requestId, null);
  });

  test("unknown code (registry drift): generic line + request id", () => {
    const view = describeAuthError(
      apiError("BRAND_NEW_CODE", "未来新增的错误", null, "req-999"),
    );
    assert.equal(view.summary, "操作失败，请稍后重试");
    assert.equal(view.requestId, "req-999");
  });

  test("system codes: infrastructure copy + request id, no domain branch", () => {
    const view = describeAuthError(
      apiError("INTERNAL_ERROR", "服务器内部错误", null, "req-500", 500),
    );
    assert.equal(view.summary, "服务暂时不可用，请稍后重试");
    assert.equal(view.requestId, "req-500");
  });

  test("details of both VALIDATION_ERROR shapes extract without throwing", () => {
    assert.deepEqual(extractValidationFieldErrors(null), {});
    assert.deepEqual(extractValidationFieldErrors("string"), {});
    assert.deepEqual(extractValidationFieldErrors({ field: 42 }), {});
    assert.deepEqual(extractValidationFieldErrors({ unknown_field: ["x"] }), {});
  });
});

describe("cooldown: server OTP_RESEND_COOLDOWN envelope -> countdown seconds", () => {
  test("envelope without details uses the 60s backend default", () => {
    const error = apiError(
      "OTP_RESEND_COOLDOWN",
      "验证码发送过于频繁，请稍后再试",
      null,
      null,
      429,
    );
    assert.equal(cooldownSecondsFrom(error), 60);
    // The display chain the form renders: countdown seconds on the button.
    assert.equal(
      resendButtonLabel(cooldownSecondsFrom(error), "重新发送", false),
      "重新发送（60 秒）",
    );
  });

  test("details.retry_after is honored when the envelope carries it", () => {
    assert.equal(
      cooldownSecondsFrom(apiError("OTP_RESEND_COOLDOWN", "x", { retry_after: 37 }, null, 429)),
      37,
    );
    assert.equal(
      cooldownSecondsFrom(apiError("OTP_RESEND_COOLDOWN", "x", { retry_after: "42" }, null, 429)),
      42,
    );
    // Fractional ceilings up; zero/negative/garbage fall back; huge clamps.
    assert.equal(
      cooldownSecondsFrom(apiError("OTP_RESEND_COOLDOWN", "x", { retry_after: 0.5 }, null, 429)),
      1,
    );
    assert.equal(
      cooldownSecondsFrom(apiError("OTP_RESEND_COOLDOWN", "x", { retry_after: -5 }, null, 429)),
      60,
    );
    assert.equal(
      cooldownSecondsFrom(apiError("OTP_RESEND_COOLDOWN", "x", { retry_after: "abc" }, null, 429)),
      60,
    );
    assert.equal(
      cooldownSecondsFrom(apiError("OTP_RESEND_COOLDOWN", "x", { retry_after: 9000 }, null, 429)),
      900,
    );
  });

  test("non-cooldown errors start no countdown", () => {
    assert.equal(cooldownSecondsFrom(apiError("RATE_LIMITED", "尝试过多", null, null, 429)), 0);
    assert.equal(cooldownSecondsFrom(apiError("OTP_CODE_INVALID", "验证码不正确")), 0);
  });
});

describe("resend button label (cooldown display)", () => {
  test("counts down as visible text, never color-only state", () => {
    assert.equal(resendButtonLabel(45, "重新发送", false), "重新发送（45 秒）");
    assert.equal(resendButtonLabel(1, "重新发送", false), "重新发送（1 秒）");
    assert.equal(resendButtonLabel(0, "获取验证码", false), "获取验证码");
    assert.equal(resendButtonLabel(0, "获取验证码", true), "发送中…");
  });
});
