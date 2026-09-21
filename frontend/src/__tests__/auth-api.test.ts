/**
 * Task 2 (Plan 09): auth API wrappers against a stubbed fetch transport.
 *
 * API-mocking decision (task brief): `docs/quality/frontend-patterns.md`
 * prescribes NO mocking library (no MSW mention anywhere in the doc), so the
 * tests stub `globalThis.fetch` — the api client's own test seam, the same
 * approach Task 1's api-errors tests established — instead of adding an MSW
 * dependency.
 *
 * Pins the wire contract the forms depend on:
 * - request shapes and paths for all five identity auth endpoints
 *   (field names exactly as the backend schemas define them);
 * - the envelope -> ApiError -> code-branch chain end to end: a 429
 *   OTP_RESEND_COOLDOWN response drives the resend countdown, a 401 maps to
 *   the uniform login failure copy, an unknown code degrades to generic +
 *   request id;
 * - 204 password-reset confirmation resolves to undefined.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  confirmPasswordReset,
  loginStudent,
  registerStudent,
  requestPasswordReset,
  requestPhoneChallenge,
  verifyPhoneChallenge,
} from "../features/auth/api";
import { cooldownSecondsFrom, describeAuthError, resendButtonLabel } from "../features/auth/errors";
import { isApiError } from "../lib/errors";

type RecordedRequest = {
  url: string;
  method: string;
  headers: Headers;
  credentials: RequestCredentials | undefined;
  body: string | null;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status: number, headers: Record<string, string> = {}) {
  // 204/205 are null-body statuses: undici rejects a Response constructed
  // with an explicit (even empty) body for them.
  responseFor = () => new Response(body.length > 0 ? body : null, { status, headers });
}

function envelope(code: string, message: string, extra: Record<string, unknown> = {}) {
  return JSON.stringify({
    error: { code, message, details: null, request_id: null, ...extra },
  });
}

async function rejectionOf(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => assert.fail("expected rejection"),
    (error: unknown) => error,
  );
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      headers: new Headers(init?.headers),
      credentials: init?.credentials,
      body: typeof init?.body === "string" ? init.body : null,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("login", () => {
  test("posts username/password to /api/v1/auth/login with cookie credentials", async () => {
    stubFetch(JSON.stringify({ access_token: "at", csrf_token: "ct", token_type: "bearer" }), 200);
    const tokens = await loginStudent("20240001", "correct-horse");
    assert.equal(tokens.access_token, "at");
    assert.equal(recorded?.url, "/api/v1/auth/login");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.credentials, "include");
    assert.equal(recorded?.headers.get("Content-Type"), "application/json");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      username: "20240001",
      password: "correct-horse",
    });
  });

  test("401 envelope maps to the uniform wrong-credentials copy", async () => {
    stubFetch(envelope("AUTHENTICATION_REQUIRED", "未登录或登录状态已失效"), 401);
    const error = await rejectionOf(loginStudent("20240001", "wrong"));
    assert.ok(isApiError(error));
    const view = describeAuthError(error as never);
    assert.equal(view.summary, "学号或密码不正确");
    assert.deepEqual(view.fieldErrors, {});
  });
});

describe("registration OTP chain", () => {
  test("requests a challenge with {phone} only", async () => {
    stubFetch(
      JSON.stringify({
        challenge_id: "11111111-1111-4111-8111-111111111111",
        expires_at: "2026-09-21T12:05:00Z",
      }),
      201,
    );
    const challenge = await requestPhoneChallenge("13800138000");
    assert.equal(challenge.challenge_id, "11111111-1111-4111-8111-111111111111");
    assert.equal(recorded?.url, "/api/v1/auth/phone/challenges");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), { phone: "13800138000" });
  });

  test("server cooldown envelope (no retry hint) -> 60s countdown on the button", async () => {
    stubFetch(envelope("OTP_RESEND_COOLDOWN", "验证码发送过于频繁，请稍后再试"), 429);
    const error = await rejectionOf(requestPhoneChallenge("13800138000"));
    assert.ok(isApiError(error));
    const seconds = cooldownSecondsFrom(error as never);
    assert.equal(seconds, 60);
    // Exactly what RegisterForm renders on the resend control.
    assert.equal(resendButtonLabel(seconds, "重新发送", false), "重新发送（60 秒）");
  });

  test("server cooldown envelope with details.retry_after -> that countdown", async () => {
    stubFetch(
      JSON.stringify({
        error: {
          code: "OTP_RESEND_COOLDOWN",
          message: "验证码发送过于频繁",
          details: { retry_after: 37 },
          request_id: "req-1",
        },
      }),
      429,
    );
    const error = await rejectionOf(requestPhoneChallenge("13800138000"));
    assert.ok(isApiError(error));
    assert.equal(resendButtonLabel(cooldownSecondsFrom(error as never), "重新发送", false), "重新发送（37 秒）");
  });

  test("verifies the code against the challenge URL and returns the proof", async () => {
    stubFetch(
      JSON.stringify({
        phone_token: "proof-token",
        expires_at: "2026-09-21T12:10:00Z",
      }),
      200,
    );
    const verified = await verifyPhoneChallenge(
      "11111111-1111-4111-8111-111111111111",
      "123456",
    );
    assert.equal(verified.phone_token, "proof-token");
    assert.equal(
      recorded?.url,
      "/api/v1/auth/phone/challenges/11111111-1111-4111-8111-111111111111/verify",
    );
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), { code: "123456" });
  });

  test("wrong-code envelope stays an OTP_CODE_INVALID branch, message untouched", async () => {
    stubFetch(envelope("OTP_CODE_INVALID", "验证码不正确"), 400);
    const error = await rejectionOf(
      verifyPhoneChallenge("11111111-1111-4111-8111-111111111111", "000000"),
    );
    assert.ok(isApiError(error));
    const view = describeAuthError(error as never);
    assert.equal(view.summary, "验证码不正确");
    assert.equal(view.requestId, null);
  });

  test("register posts the exact backend field names", async () => {
    stubFetch(
      JSON.stringify({
        id: "22222222-2222-4222-8222-222222222222",
        username: "20240001",
        nickname: "小明",
        role: "STUDENT",
        status: "ACTIVE",
      }),
      201,
    );
    const user = await registerStudent({
      student_number: "20240001",
      nickname: "小明",
      phone_token: "proof-token",
      password: "correct-horse",
    });
    assert.equal(user.status, "ACTIVE");
    assert.equal(recorded?.url, "/api/v1/auth/register");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      student_number: "20240001",
      nickname: "小明",
      phone_token: "proof-token",
      password: "correct-horse",
    });
  });

  test("whitelist rejection keeps its stable copy and code branch", async () => {
    stubFetch(envelope("STUDENT_NOT_WHITELISTED", "该学号不在注册白名单中，无法注册"), 403);
    const error = await rejectionOf(
      registerStudent({
        student_number: "999999",
        nickname: "小明",
        phone_token: "proof-token",
        password: "correct-horse",
      }),
    );
    assert.ok(isApiError(error));
    const view = describeAuthError(error as never);
    assert.equal(view.summary, "该学号不在注册白名单中，无法注册");
  });
});

describe("password reset", () => {
  test("forgot posts {username} and yields the (decoy-or-real) challenge", async () => {
    stubFetch(
      JSON.stringify({
        challenge_id: "33333333-3333-4333-8333-333333333333",
        expires_at: "2026-09-21T12:05:00Z",
      }),
      200,
    );
    const challenge = await requestPasswordReset("20240001");
    assert.equal(challenge.challenge_id, "33333333-3333-4333-8333-333333333333");
    assert.equal(recorded?.url, "/api/v1/auth/password/forgot");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), { username: "20240001" });
  });

  test("confirm posts challenge_id/code/new_password and 204 resolves undefined", async () => {
    stubFetch("", 204);
    const result = await confirmPasswordReset({
      challenge_id: "33333333-3333-4333-8333-333333333333",
      code: "123456",
      new_password: "new-correct-horse",
    });
    assert.equal(result, undefined);
    assert.equal(recorded?.url, "/api/v1/auth/password/reset");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      challenge_id: "33333333-3333-4333-8333-333333333333",
      code: "123456",
      new_password: "new-correct-horse",
    });
  });
});

describe("error-code branching pin at the transport level", () => {
  test("unknown envelope code from any endpoint -> generic view + request id", async () => {
    stubFetch(
      JSON.stringify({
        error: { code: "BRAND_NEW_CODE", message: "未来新增的错误", details: null, request_id: "req-77" },
      }),
      400,
    );
    const error = await rejectionOf(loginStudent("20240001", "whatever"));
    assert.ok(isApiError(error));
    const view = describeAuthError(error as never);
    assert.equal(view.summary, "操作失败，请稍后重试");
    assert.equal(view.requestId, "req-77");
  });

  test("out-of-surface business code -> backend message as fallback text", async () => {
    stubFetch(envelope("ASSIGNMENT_LIMIT_REACHED", "当前进行中的任务已达到上限"), 409);
    const error = await rejectionOf(loginStudent("20240001", "whatever"));
    assert.ok(isApiError(error));
    const view = describeAuthError(error as never);
    assert.equal(view.summary, "当前进行中的任务已达到上限");
    assert.equal(view.requestId, null);
  });
});
