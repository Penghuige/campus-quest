/**
 * The remembered-device hint probe (owner feedback ③, 2026-10-17;
 * frozen backend contract): GET /api/v1/auth/mobile-code/remembered is
 * the login page's ONE anonymous probe — a valid cq_staff_device
 * cookie bound to an ACTIVE/PENDING_APPROVAL student answers
 * 200 {phone_masked}; every other shape (no cookie, expired, staff
 * binding) answers a uniform 204 with no existence leak. The wrapper
 * turns both non-answer shapes (and transport failure) into `null`
 * quietly: a probe must never break or delay the login form.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { clearAccessToken, getAccessToken } from "../lib/accessToken";
import {
  fastLoginCookieOnly,
  fetchRememberedHint,
  registerViaMobileCode,
  verifyMobileLoginCode,
} from "../features/auth/mobileLoginApi";

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

describe("fetchRememberedHint (the login page's anonymous probe)", () => {
  test("GETs the remembered endpoint with cookie credentials; 200 answers the mask", async () => {
    stubFetch(JSON.stringify({ phone_masked: "+86****1234" }), 200);
    const mask = await fetchRememberedHint();
    assert.equal(mask, "+86****1234");
    assert.equal(recorded?.url, "/api/v1/auth/mobile-code/remembered");
    assert.equal(recorded?.method, "GET");
    assert.equal(recorded?.credentials, "include");
  });

  test("the uniform 204 (no cookie / expired / staff binding) resolves null", async () => {
    stubFetch("", 204);
    assert.equal(await fetchRememberedHint(), null);
  });

  test("a transport failure resolves null quietly — the probe never breaks the login page", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("offline");
    }) as typeof fetch;
    assert.equal(await fetchRememberedHint(), null);
  });
});

describe("fastLoginCookieOnly (the banner's click leg)", () => {
  test("POSTs the verify endpoint with an EMPTY body + cookie; adopts the returned tokens", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({ access_token: "fast-at", csrf_token: "ct", token_type: "bearer" }),
      200,
    );
    const tokens = await fastLoginCookieOnly();
    assert.equal(tokens.access_token, "fast-at");
    assert.equal(getAccessToken(), "fast-at");
    assert.equal(recorded?.url, "/api/v1/auth/mobile-code/verify");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.credentials, "include");
    // The banner holds only the MASK — the cookie alone locates the
    // user (it is the full single-factor credential), so the body
    // carries neither phone nor code.
    assert.equal(recorded?.body, "{}");
  });
});

// --- the send-code-IS-the-entry flow (production experience batch
// 2026-10-19; the frozen contract): an UNREGISTERED phone's verify
// answers {status:"unregistered", register_token} — the login page's
// inline third step consumes it; POST /auth/mobile-code/register
// {register_token, student_number, password} (the register page's
// optional-password semantics: null when unset) completes the login.
describe("the inline register legs (send-code-as-entry)", () => {
  test("verify: the unregistered body classifies WITHOUT adopting any token", async () => {
    clearAccessToken();
    stubFetch(JSON.stringify({ status: "unregistered", register_token: "reg-tok" }), 200);
    const outcome = await verifyMobileLoginCode("13900001234", "234567", false);
    assert.deepEqual(outcome, { kind: "unregistered", registerToken: "reg-tok" });
    assert.equal(getAccessToken(), null);
  });

  test("verify: the tokens body still completes the login (the union's other arm)", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({ access_token: "at", csrf_token: "ct", token_type: "bearer" }),
      200,
    );
    const outcome = await verifyMobileLoginCode("13900001234", "234567", false);
    assert.equal(outcome.kind, "tokens");
    assert.equal(getAccessToken(), "at");
  });

  test("register POSTs {register_token, student_number, password:null-when-unset} and adopts", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({ access_token: "reg-at", csrf_token: "ct", token_type: "bearer" }),
      200,
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
      JSON.stringify({ access_token: "reg-at", csrf_token: "ct", token_type: "bearer" }),
      200,
    );
    await registerViaMobileCode("reg-tok", "20240099", "e2e-correct-horse");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      register_token: "reg-tok",
      student_number: "20240099",
      password: "e2e-correct-horse",
    });
  });
});
