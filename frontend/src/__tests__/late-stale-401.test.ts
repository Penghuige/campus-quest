/**
 * The targeted re-review P0 regression: a LATE stale-401 (a 401 for a
 * request that was sent with a token another request has SINCE rotated
 * past) must ride the current token — never start a second rotation,
 * which would revoke the session the first retry is riding (the
 * backend's rotate-once semantics revoke the predecessor session).
 *
 * Same harness style as access-token.test.ts: a URL-routing fetch fake
 * with per-URL response queues (entries may return promises — the
 * deferred 401 IS the late arrival), no mocking library.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  getAccessToken,
  refreshAccessToken,
  resetAccessTokenManagerForTests,
  setAccessToken,
} from "../lib/accessToken";
import { apiRequest } from "../lib/api";

type RecordedRequest = {
  url: string;
  headers: Headers;
};

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

function envelope401(): () => Response {
  return json(
    {
      error: {
        code: "AUTHENTICATION_REQUIRED",
        message: "未登录或登录状态已失效",
        details: null,
        request_id: null,
      },
    },
    401,
  );
}

function deferred401(): { promise: Promise<Response>; resolve: () => void } {
  const late = Promise.withResolvers<Response>();
  return {
    promise: late.promise,
    resolve: () => {
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
    },
  };
}

function tokenPair(access: string): unknown {
  return { access_token: access, csrf_token: "ct", token_type: "bearer" };
}

const PROTECTED = "/api/v1/protected";
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

describe("late stale-401 must not rotate twice (targeted re-review P0)", () => {
  test("owner's sequence: A rotates; B's OLD-token 401 arrives after — one refresh total, both retries ride token-1", async () => {
    setAccessToken("token-0");
    const lateB = deferred401();
    route({
      [PROTECTED]: [
        envelope401(), // A's attempt (token-0)
        () => lateB.promise, // B's attempt (token-0), deferred
        json({ ok: "A" }), // A's retry (token-1)
        json({ ok: "B" }), // B's retry (token-1) — NO second rotation
      ],
      [REFRESH]: [json(tokenPair("token-1"))],
    });

    const requestA = apiRequest<{ ok: string }>(PROTECTED);
    const requestB = apiRequest<{ ok: string }>(PROTECTED);

    // A completes its full recovery FIRST (401 -> refresh -> retry); the
    // single-flight slot is freed when A's rotation settles.
    assert.equal((await requestA).ok, "A");

    // NOW B's 401 lands — the manager already holds token-1.
    lateB.resolve();
    assert.equal((await requestB).ok, "B");

    assert.equal(calls.filter((call) => call.url === REFRESH).length, 1);
    const tokenOneRetries = calls.filter(
      (call) => call.url === PROTECTED && call.headers.get("Authorization") === "Bearer token-1",
    );
    assert.equal(tokenOneRetries.length, 2);
    assert.equal(getAccessToken(), "token-1");
  });

  test("a 401 landing after a DIRECT manager rotation rides the new token without refreshing", async () => {
    setAccessToken("token-0");
    const late = deferred401();
    route({
      [PROTECTED]: [
        json({ ok: "direct" }), // an earlier request on token-0
        () => late.promise, // this request (token-0), deferred 401
        json({ ok: "rode-token-1" }), // its retry
      ],
      [REFRESH]: [json(tokenPair("token-1"))],
    });
    assert.equal((await apiRequest<{ ok: string }>(PROTECTED)).ok, "direct");

    const second = apiRequest<{ ok: string }>(PROTECTED);
    await refreshAccessToken(); // the rotation the 401 will land after
    late.resolve();
    assert.equal((await second).ok, "rode-token-1");
    assert.equal(calls.filter((call) => call.url === REFRESH).length, 1);
  });
});
