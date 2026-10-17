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
import { fastLoginCookieOnly, fetchRememberedHint } from "../features/auth/mobileLoginApi";

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
