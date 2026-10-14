/**
 * Task 8 (Plan 09): staff API wrappers against a stubbed fetch transport.
 *
 * Same harness decision as auth-api.test.ts (no mocking library — the
 * stream's documented stance): `globalThis.fetch` is stubbed, and every
 * request is recorded. Pins the wire contract the staff forms depend on:
 * - paths + exact backend field names (`token`, `password`, `email`,
 *   `totp_code`, `code`) for all four staff endpoints;
 * - the Authorization: Bearer header on the two TOTP-setup calls (the
 *   pending staff session's body access token — the sanctioned exception)
 *   and cookie credentials everywhere;
 * - the envelope -> ApiError -> per-surface copy chain end to end
 *   (403 TOTP_SETUP_REQUIRED drives the login form's setup panel state,
 *   a dead pending session renders the totp-session copy);
 * - recovery codes pass through verbatim into the one-time display.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  acceptStaffInvitation,
  beginTotpSetup,
  confirmTotpSetup,
  emailLoginTotp,
  loginStaff,
  readMe,
  requestEmailLoginCode,
  verifyEmailLoginCode,
} from "../features/auth/api";
import { describeStaffError } from "../features/auth/staffAuthView";
import { isApiError } from "../lib/errors";
import { clearAccessToken, getAccessToken } from "../lib/accessToken";

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

describe("acceptStaffInvitation", () => {
  test("posts {token, password} with cookie credentials", async () => {
    stubFetch(JSON.stringify({ access_token: "at", csrf_token: "ct", token_type: "bearer" }), 200);
    const tokens = await acceptStaffInvitation("invite-token", "correct-horse");
    assert.equal(tokens.access_token, "at");
    assert.equal(recorded?.url, "/api/v1/auth/staff/invitations/accept");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.credentials, "include");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      token: "invite-token",
      password: "correct-horse",
    });
  });

  test("the uniform 401 (unknown/expired/used token) renders the invite copy", async () => {
    stubFetch(envelope("AUTHENTICATION_REQUIRED", "登录状态已失效或凭证不正确"), 401);
    const error = await rejectionOf(acceptStaffInvitation("dead", "correct-horse"));
    assert.ok(isApiError(error));
    const view = describeStaffError(error as never, "invite-accept");
    assert.equal(view.summary, "邀请链接无效或已被使用");
  });
});

describe("loginStaff", () => {
  test("posts {email, password, totp_code} to the dedicated staff endpoint", async () => {
    stubFetch(JSON.stringify({ access_token: "at", csrf_token: "ct", token_type: "bearer" }), 200);
    await loginStaff("teacher@school.edu", "correct-horse", "234567");
    assert.equal(recorded?.url, "/api/v1/auth/staff/login");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.credentials, "include");
    assert.equal(recorded?.headers.get("Content-Type"), "application/json");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      email: "teacher@school.edu",
      password: "correct-horse",
      totp_code: "234567",
    });
  });

  test("403 TOTP_SETUP_REQUIRED stays a code branch for the form's setup panel", async () => {
    stubFetch(envelope("TOTP_SETUP_REQUIRED", "该账号必须先绑定 TOTP 动态验证码"), 403);
    const error = await rejectionOf(loginStaff("teacher@school.edu", "correct-horse", "234567"));
    assert.ok(isApiError(error));
    assert.equal((error as { code: string }).code, "TOTP_SETUP_REQUIRED");
    const view = describeStaffError(error as never, "staff-login");
    assert.match(view.summary!, /尚未完成动态口令绑定/);
  });
});

describe("TOTP setup calls (pending-session bearer)", () => {
  test("begin posts with Authorization: Bearer and no body", async () => {
    stubFetch(
      JSON.stringify({
        secret: "JBSWY3DPEHPK3PXP",
        otpauth_uri: "otpauth://totp/CampusQuest:t%40s?secret=JBSWY3DPEHPK3PXP&issuer=CampusQuest",
      }),
      200,
    );
    const setup = await beginTotpSetup("pending-access-token");
    assert.equal(setup.secret, "JBSWY3DPEHPK3PXP");
    assert.equal(recorded?.url, "/api/v1/staff/totp/begin");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.headers.get("Authorization"), "Bearer pending-access-token");
    assert.equal(recorded?.credentials, "include");
    assert.equal(recorded?.body, null);
  });

  test("confirm posts {code} with the same bearer header", async () => {
    stubFetch(JSON.stringify({ recovery_codes: ["0f1e2-d3c4b", "1a2b3-c4d5e"] }), 200);
    const confirmed = await confirmTotpSetup("pending-access-token", "234567");
    assert.deepEqual(confirmed.recovery_codes, ["0f1e2-d3c4b", "1a2b3-c4d5e"]);
    assert.equal(recorded?.url, "/api/v1/staff/totp/confirm");
    assert.equal(recorded?.headers.get("Authorization"), "Bearer pending-access-token");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), { code: "234567" });
  });

  test("a dead pending session (401 at begin) renders the totp-session copy", async () => {
    stubFetch(envelope("AUTHENTICATION_REQUIRED", "未登录或登录状态已失效"), 401);
    const error = await rejectionOf(beginTotpSetup("expired-token"));
    assert.ok(isApiError(error));
    const view = describeStaffError(error as never, "totp-session");
    assert.match(view.summary!, /绑定会话已失效/);
  });

  test("a wrong confirm code (401) renders the retryable wrong-code copy", async () => {
    stubFetch(envelope("AUTHENTICATION_REQUIRED", "动态验证码错误"), 401);
    const error = await rejectionOf(confirmTotpSetup("pending-access-token", "000000"));
    assert.ok(isApiError(error));
    const view = describeStaffError(error as never, "totp-confirm");
    assert.match(view.summary!, /动态验证码错误/);
  });
});

<<<<<<< HEAD
describe("readMe (the post-login role source; owner report 2026-10-15)", () => {
  test("rides a bare GET /api/v1/me with cookie credentials", async () => {
    stubFetch(
      JSON.stringify({
        id: "u1",
        username: "a@b.edu",
        nickname: "A",
        role: "ADMIN",
        status: "ACTIVE",
        has_avatar: false,
        phone_e164: null,
        email_normalized: null,
        email_verified_at: null,
        created_at: "2030-01-01T00:00:00Z",
      }),
      200,
    );
    const me = await readMe();
    assert.equal(me.role, "ADMIN");
    assert.equal(recorded?.url, "/api/v1/me");
    assert.equal(recorded?.method, "GET");
    assert.equal(recorded?.credentials, "include");
  });
});

describe("email-code login (V1.1 §2.1/§2.2; plan T5 frozen contract)", () => {
  test("request POSTs {email} and resolves the uniform 202 {}", async () => {
    stubFetch("{}", 202);
    await requestEmailLoginCode("teacher@school.edu");
    assert.equal(recorded?.url, "/api/v1/auth/staff/email-code/request");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.credentials, "include");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      email: "teacher@school.edu",
    });
  });

  test("verify POSTs {email, code} — NO remember_device from the code step — and records the teacher login", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({ access_token: "teacher-at", csrf_token: "ct", token_type: "bearer" }),
      200,
    );
    const outcome = await verifyEmailLoginCode("teacher@school.edu", "234567");
    assert.equal(recorded?.url, "/api/v1/auth/staff/email-code/verify");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      email: "teacher@school.edu",
      code: "234567",
    });
    assert.equal(outcome.kind, "tokens");
    assert.equal(getAccessToken(), "teacher-at");
  });

  test("verify classifies the ADMIN totp_required body and adopts NO token", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({ status: "totp_required", pending_token: "pending-at" }),
      200,
    );
    const outcome = await verifyEmailLoginCode("admin@school.edu", "234567");
    assert.deepEqual(outcome, { kind: "totp_required", pendingToken: "pending-at" });
    assert.equal(getAccessToken(), null);
  });

  test("the totp leg POSTs {code, remember_device} under the pending bearer and records the login", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({ access_token: "admin-at", csrf_token: "ct", token_type: "bearer" }),
      200,
    );
    const tokens = await emailLoginTotp("pending-at", "234567", true);
    assert.equal(tokens.access_token, "admin-at");
    assert.equal(recorded?.url, "/api/v1/auth/staff/email-code/totp");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.headers.get("Authorization"), "Bearer pending-at");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      code: "234567",
      remember_device: true,
    });
    assert.equal(getAccessToken(), "admin-at");
  });

  test("the totp leg omits remember_device when unchecked (presence-based)", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({ access_token: "admin-at", csrf_token: "ct", token_type: "bearer" }),
      200,
    );
    await emailLoginTotp("pending-at", "234567", false);
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), { code: "234567" });
  });
});
