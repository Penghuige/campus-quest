/**
 * The unified login page's pure view seam (unified-login spec §4):
 * the ONE identifier field's two validation bands and the challenge
 * family's error-code copy — pinned before the panel mounts them.
 *
 * - validateChallengeIdentifier: phone (E.164 CN) OR email — the code
 *   layer's band (spec §3.1: a student number is a FORMAT-layer
 *   refusal here, mirroring the backend's 422);
 * - validatePasswordIdentifier: the password layer widens to the
 *   THREE-state identifier (phone | email | student number — spec §3.2);
 * - describeChallengeError: the CHALLENGE_CODE_INVALID uniform verdict
 *   (one copy across the email/SMS streams), the RATE_LIMITED cooldown
 *   minute, and the ACCOUNT_LOCKED envelope's whole minutes — the
 *   family's verbatim discipline (§29: copy branches on code only).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";

import {
  describeChallengeError,
  validateChallengeIdentifier,
  validatePasswordIdentifier,
} from "../features/auth/unifiedLoginView";

function apiError(
  code: string,
  message: string,
  details: unknown = null,
  requestId: string | null = "req-1",
): ApiError {
  return new ApiError({ code, message, details, status: 400, requestId });
}

describe("validateChallengeIdentifier (the code layer's band)", () => {
  test("a CN mobile passes", () => {
    assert.equal(validateChallengeIdentifier("13900001234"), undefined);
  });

  test("an email passes", () => {
    assert.equal(validateChallengeIdentifier("teacher@school.edu"), undefined);
  });

  test("a student number is REFUSED at the format layer — the code stream never takes it", () => {
    assert.match(validateChallengeIdentifier("20250101")!, /手机号或邮箱/);
  });

  test("garbage refuses", () => {
    assert.match(validateChallengeIdentifier("not-an-identifier")!, /手机号或邮箱/);
    assert.match(validateChallengeIdentifier("")!, /手机号或邮箱/);
  });
});

describe("validatePasswordIdentifier (the three-state band)", () => {
  test("all three identifier classes pass", () => {
    assert.equal(validatePasswordIdentifier("13900001234"), undefined);
    assert.equal(validatePasswordIdentifier("teacher@school.edu"), undefined);
    assert.equal(validatePasswordIdentifier("20250101"), undefined);
  });

  test("garbage refuses with the three-state copy", () => {
    assert.match(validatePasswordIdentifier("abc")!, /手机号、邮箱或学号/);
  });
});

describe("describeChallengeError (the §29 code-branched copy)", () => {
  test("CHALLENGE_CODE_INVALID is the uniform wrong/expired verdict", () => {
    const view = describeChallengeError(
      apiError("CHALLENGE_CODE_INVALID", "x"),
      "fallback",
    );
    assert.equal(view.message, "验证码错误或已过期");
    assert.equal(view.requestId, "req-1");
  });

  test("ACCOUNT_LOCKED rounds the envelope's seconds up to whole minutes", () => {
    const view = describeChallengeError(
      apiError("ACCOUNT_LOCKED", "x", { retry_after_seconds: 61 }, null),
      "fallback",
    );
    assert.equal(view.message, "尝试次数过多，请 2 分钟后再试");
  });

  test("ACCOUNT_LOCKED without the envelope answers the honest short copy", () => {
    const view = describeChallengeError(
      apiError("ACCOUNT_LOCKED", "x", null, null),
      "fallback",
    );
    assert.equal(view.message, "尝试次数过多，请稍后再试");
  });

  test("RATE_LIMITED is the 60s resend cooldown", () => {
    const view = describeChallengeError(
      apiError("RATE_LIMITED", "x", null, null),
      "fallback",
    );
    assert.equal(view.message, "发送过于频繁，请 1 分钟后再试");
  });

  test("anything else falls back to the server message, then the given fallback", () => {
    const view = describeChallengeError(
      apiError("SURPRISE", "服务端消息", null, null),
      "fallback",
    );
    assert.equal(view.message, "服务端消息");

    const fallbackView = describeChallengeError(
      apiError("SURPRISE", "", null, null),
      "fallback",
    );
    assert.equal(fallbackView.message, "fallback");
  });

  test("a non-ApiError cause answers the network copy", () => {
    const view = describeChallengeError(new Error("transport"), "fallback");
    assert.equal(view.message, "网络异常，请检查连接后重试");
    assert.equal(view.requestId, null);
  });
});
