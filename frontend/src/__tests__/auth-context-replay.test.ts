/**
 * The final re-review P0 regression: after an EXPLICIT auth-context
 * switch (logout A -> login B), a request that was sent under A's
 * context must NEVER auto-replay with B's token — the "token changed"
 * signal alone cannot distinguish a same-session refresh rotation from
 * a context switch, and replaying A's mutation intent against B's
 * account is the worst outcome. The authEpoch separates the two.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  getAccessToken,
  recordLogin,
  recordLogout,
  resetAccessTokenManagerForTests,
} from "../lib/accessToken";
import { apiRequest } from "../lib/api";
import { isApiError } from "../lib/errors";

type RecordedRequest = { url: string; headers: Headers };

let calls: RecordedRequest[];
let respondWith: (url: string) => Response | Promise<Response>;
const originalFetch = globalThis.fetch;

function route(responses: Record<string, Array<() => Response | Promise<Response>>>): void {
  respondWith = (url: string) => {
    const queue = responses[url];
    assert.ok(queue !== undefined && queue.length > 0, `unexpected fetch: ${url}`);
    return queue.shift()!();
  };
}

function json(body: unknown, status = 200): () => Response {
  return () => new Response(JSON.stringify(body), { status });
}

const REDEEM = "/api/v1/rewards/r1/redeem";
const REFRESH = "/api/v1/auth/refresh";

beforeEach(() => {
  resetAccessTokenManagerForTests();
  calls = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), headers: new Headers(init?.headers) });
    return respondWith(String(input));
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("an auth-context switch must not replay an old context's request (final re-review P0)", () => {
  test("logout A -> login B; A's mutation 401 lands late — no replay onto B, no rotation", async () => {
    recordLogin("token-A"); // A is logged in
    const lateA401 = Promise.withResolvers<Response>();
    route({
      [REDEEM]: [
        () => lateA401.promise, // A's redeem attempt (token-A), deferred
      ],
      [REFRESH]: [
        json({ access_token: "must-never-happen", csrf_token: "ct", token_type: "bearer" }),
      ],
    });

    // A's mutation is in flight when the human logs out and B logs in.
    const aRedeem = apiRequest(REDEEM, { method: "POST", body: { pin: 1 } });
    recordLogout(); // A logs out (epoch bump)
    recordLogin("token-B"); // B logs in (epoch bump again)

    // A's 401 arrives in B's epoch.
    lateA401.resolve(
      new Response(
        JSON.stringify({
          error: {
            code: "AUTHENTICATION_REQUIRED",
            message: "未登录或登录状态已失效",
            details: null,
            request_id: null,
          },
        }),
        { status: 401 },
      ),
    );

    // The original 401 surfaces — NOT a replay with token-B.
    await assert.rejects(aRedeem, (error: unknown) => {
      assert.ok(isApiError(error));
      assert.equal(error.status, 401);
      return true;
    });

    // Exactly ONE redeem call ever happened (A's attempt); nothing was
    // replayed under B's token, and no refresh rotated B's session for
    // A's dead request.
    const redeemCalls = calls.filter((call) => call.url === REDEEM);
    assert.equal(redeemCalls.length, 1);
    assert.equal(redeemCalls[0].headers.get("Authorization"), "Bearer token-A");
    assert.equal(calls.filter((call) => call.url === REFRESH).length, 0);
    assert.equal(getAccessToken(), "token-B"); // B's session untouched
  });

  test("same-context rotation still replays (the epoch did NOT move)", async () => {
    recordLogin("token-0");
    const late = Promise.withResolvers<Response>();
    route({
      ["/api/v1/protected"]: [
        () => late.promise, // sent under token-0
        json({ ok: true }), // replay under token-1
      ],
      [REFRESH]: [json({ access_token: "token-1", csrf_token: "ct", token_type: "bearer" })],
    });
    const request = apiRequest<{ ok: boolean }>("/api/v1/protected");
    await new Promise<void>((resolve) => {
      // A rotation WITHIN the same auth context (no epoch bump).
      import("../lib/accessToken").then(({ refreshAccessToken }) => {
        refreshAccessToken().then(() => resolve());
      });
    });
    late.resolve(
      new Response(
        JSON.stringify({
          error: {
            code: "AUTHENTICATION_REQUIRED",
            message: "未登录或登录状态已失效",
            details: null,
            request_id: null,
          },
        }),
        { status: 401 },
      ),
    );
    assert.equal((await request).ok, true); // same-context replay still works
  });
});
