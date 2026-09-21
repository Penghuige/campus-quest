/**
 * Task 5 (Plan 09): account settings — email-panel state derivation,
 * the password-confirmation rule, the account-surface error mappings
 * (new codes + VALIDATION_ERROR field placement), the T2 validator
 * mirror the settings forms reuse, and the own-account wrappers' wire
 * contract (request bodies exactly as the backend schemas define them:
 * password/new_phone, challenge_id/code, token, current_password/
 * new_password).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  changePassword,
  confirmEmailVerification,
  confirmPhoneChange,
  requestEmailVerification,
  requestPhoneChange,
  unbindEmail,
  updateNickname,
  type MeDto,
} from "../features/auth/api";
import {
  emailPanelView,
  validatePasswordConfirmation,
} from "../features/auth/accountView";
import { describeAuthError } from "../features/auth/errors";
import {
  countGraphemes,
  validateNickname,
  validateOtpCode,
  validatePassword,
  validatePhone,
} from "../features/auth/validation";
import { ApiError } from "../lib/errors";

type RecordedRequest = {
  url: string;
  method: string;
  body: string | null;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

const ME: MeDto = {
  id: "77777777-7777-4777-8777-777777777777",
  username: "20240001",
  nickname: "小明",
  role: "STUDENT",
  status: "ACTIVE",
  phone_e164: "+8613800138000",
  email_normalized: null,
  email_verified_at: null,
};

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      body: typeof init?.body === "string" ? init.body : null,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("email panel state (MePublic -> panel)", () => {
  test("unbound -> none state with bind-first hint", () => {
    const view = emailPanelView(ME);
    assert.equal(view.state, "none");
    assert.equal(view.email, null);
    assert.equal(view.statusLabel, "未绑定");
  });

  test("bound but unverified -> unverified state", () => {
    const view = emailPanelView({
      ...ME,
      email_normalized: "ming@example.edu",
      email_verified_at: null,
    });
    assert.equal(view.state, "unverified");
    assert.equal(view.email, "ming@example.edu");
    assert.equal(view.statusLabel, "待验证");
  });

  test("verified -> verified state", () => {
    const view = emailPanelView({
      ...ME,
      email_normalized: "ming@example.edu",
      email_verified_at: "2026-09-01T00:00:00Z",
    });
    assert.equal(view.state, "verified");
    assert.equal(view.statusLabel, "已验证");
  });
});

describe("client validation mirrors the backend bands (T2 validators)", () => {
  test("nickname grapheme cap: 16 clusters pass, 17 fail; a ZWJ family is ONE", () => {
    const family = "👨‍👩‍👧‍👦"; // ZWJ-joined: one grapheme cluster
    assert.equal(countGraphemes(family.repeat(16)), 16);
    assert.equal(validateNickname(family.repeat(16)), null);
    assert.match(validateNickname(family.repeat(17)) ?? "", /最长 16/);
  });

  test("password band 10-128 code points; confirmation must agree", () => {
    assert.match(validatePassword("short") ?? "", /10-128/);
    assert.equal(validatePassword("correct-horse"), null);
    assert.equal(validatePasswordConfirmation("correct-horse", "correct-horse"), null);
    assert.match(validatePasswordConfirmation("correct-horse", "other") ?? "", /不一致/);
    assert.match(validatePasswordConfirmation("correct-horse", "") ?? "", /再次输入/);
  });

  test("OTP stays exactly 6 ASCII digits; phone stays digit-ish", () => {
    assert.match(validateOtpCode("12345") ?? "", /6 位/);
    assert.match(validateOtpCode("12345ｆ") ?? "", /6 位/);
    assert.equal(validateOtpCode("123456"), null);
    assert.equal(validatePhone("13800138000"), null);
    assert.match(validatePhone("not-a-phone") ?? "", /手机号/);
  });
});

describe("account-surface error mapping (code-keyed)", () => {
  test("the email codes map to stable copy", () => {
    assert.equal(
      describeAuthError(new ApiError({ code: "EMAIL_ALREADY_BOUND", message: "x", status: 409 }))
        .summary,
      "该邮箱已绑定其他账号",
    );
    assert.equal(
      describeAuthError(new ApiError({ code: "INVALID_EMAIL_TOKEN", message: "x", status: 422 }))
        .summary,
      "邮箱验证已失效，请重新发送验证邮件",
    );
  });

  test("VALIDATION_ERROR field errors cover the settings field names", () => {
    const shape1 = describeAuthError(
      new ApiError({
        code: "VALIDATION_ERROR",
        message: "bad input",
        status: 422,
        details: { field: "new_phone", reason: "invalid phone" },
      }),
    );
    assert.equal(shape1.fieldErrors.new_phone, "请输入正确的新手机号");

    const shape2 = describeAuthError(
      new ApiError({
        code: "VALIDATION_ERROR",
        message: "bad input",
        status: 422,
        details: { email: ["not a valid email"] },
      }),
    );
    assert.equal(shape2.fieldErrors.email, "请输入正确的邮箱地址");
  });
});

describe("own-account wrappers wire contract", () => {
  test("nickname change rides PATCH /me/nickname", async () => {
    stubFetch(JSON.stringify(ME));
    await updateNickname("新昵称");
    assert.equal(recorded?.url, "/api/v1/me/nickname");
    assert.equal(recorded?.method, "PATCH");
    assert.equal(recorded?.body, JSON.stringify({ nickname: "新昵称" }));
  });

  test("phone change two-step bodies match the backend schemas", async () => {
    stubFetch(
      JSON.stringify({
        challenge_id: "88888888-8888-4888-8888-888888888888",
        expires_at: "2026-09-21T09:00:00Z",
      }),
      201,
    );
    await requestPhoneChange("current-pass", "13900139000");
    assert.equal(recorded?.url, "/api/v1/me/phone/change");
    assert.equal(recorded?.method, "POST");
    assert.equal(
      recorded?.body,
      JSON.stringify({ password: "current-pass", new_phone: "13900139000" }),
    );

    stubFetch(JSON.stringify({ ...ME, phone_e164: "+8613900139000" }));
    await confirmPhoneChange("88888888-8888-4888-8888-888888888888", "123456");
    assert.equal(recorded?.url, "/api/v1/me/phone/change/confirm");
    assert.equal(
      recorded?.body,
      JSON.stringify({
        challenge_id: "88888888-8888-4888-8888-888888888888",
        code: "123456",
      }),
    );
  });

  test("email bind/verify/unbind bodies match the backend schemas", async () => {
    stubFetch(JSON.stringify({ expires_at: "2026-09-21T09:00:00Z" }));
    await requestEmailVerification("ming@example.edu");
    assert.equal(recorded?.url, "/api/v1/me/email");
    assert.equal(recorded?.body, JSON.stringify({ email: "ming@example.edu" }));

    stubFetch(JSON.stringify(ME));
    await confirmEmailVerification("token-from-mail");
    assert.equal(recorded?.url, "/api/v1/me/email/verify");
    assert.equal(recorded?.body, JSON.stringify({ token: "token-from-mail" }));

    stubFetch("", 204);
    await unbindEmail("current-pass");
    assert.equal(recorded?.url, "/api/v1/me/email/unbind");
    assert.equal(recorded?.body, JSON.stringify({ password: "current-pass" }));
  });

  test("password rotation sends current/new and resolves 204 to undefined", async () => {
    stubFetch("", 204);
    const result = await changePassword("current-pass", "new-strong-pass");
    assert.equal(result, undefined);
    assert.equal(recorded?.url, "/api/v1/me/password");
    assert.equal(
      recorded?.body,
      JSON.stringify({ current_password: "current-pass", new_password: "new-strong-pass" }),
    );
  });
});
