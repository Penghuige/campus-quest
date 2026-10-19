/**
 * The inline register leg's wire pins (send-code-as-entry batch
 * 2026-10-19; path untouched by the unified-login batch — spec §3.3):
 * POST /auth/mobile-code/register {register_token, student_number,
 * password:null-when-unset} answers the ④ envelope {user, tokens} and
 * the wrapper adopts the envelope's tokens. The login wrappers that
 * lived here retired with the unified page (the challenge family in
 * api.ts carries them now — unified-challenge-api.test.ts).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { clearAccessToken, getAccessToken } from "../lib/accessToken";
import { registerViaMobileCode } from "../features/auth/mobileLoginApi";

const originalFetch = globalThis.fetch;
let responseFor: () => Response;
let recorded: {
  url: string;
  method: string;
  credentials: RequestCredentials | undefined;
  body: string | null;
} | undefined;

function stubFetch(body: string, status: number) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      credentials: init?.credentials,
      body: typeof init?.body === "string" ? init.body : null,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("the inline register leg (send-code-as-entry; the register_token now arrives via /auth/challenge/verify)", () => {
  test("register POSTs {register_token, student_number, password:null-when-unset} and adopts the ENVELOPE's tokens", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        user: { id: "u9", username: "20240099", nickname: "20240099", role: "STUDENT", status: "PENDING_APPROVAL" },
        tokens: { access_token: "reg-at", csrf_token: "ct", token_type: "bearer" },
      }),
      201,
    );
    const outcome = await registerViaMobileCode("reg-tok", "20240099", null);
    assert.equal(outcome.kind, "tokens");
    assert.equal(getAccessToken(), "reg-at");
    assert.equal(recorded?.url, "/api/v1/auth/mobile-code/register");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      register_token: "reg-tok",
      student_number: "20240099",
      password: null,
    });
  });

  test("register with a set password carries it verbatim", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        user: { id: "u9", username: "20240099", nickname: "20240099", role: "STUDENT", status: "PENDING_APPROVAL" },
        tokens: { access_token: "reg-at", csrf_token: "ct", token_type: "bearer" },
      }),
      201,
    );
    await registerViaMobileCode("reg-tok", "20240099", "e2e-correct-horse");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      register_token: "reg-tok",
      student_number: "20240099",
      password: "e2e-correct-horse",
    });
  });

  test("a 201 without the envelope is a broken contract and throws", async () => {
    clearAccessToken();
    stubFetch(JSON.stringify({ user: { id: "u9" } }), 201);
    await assert.rejects(() => registerViaMobileCode("reg-tok", "20240099", null), /envelope/);
    assert.equal(getAccessToken(), null);
  });
});
