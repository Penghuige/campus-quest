/**
 * The targeted re-review P1 regression: a successful login must
 * SYNCHRONOUSLY invalidate the module-level session cache. Before the
 * fix, the login page's `useSession` cached an anonymous /me for the
 * 30s fresh window, `refreshSession()` bumped only the local hook, and
 * the freshly mounted shell rendered "未登录" from the stale entry —
 * and a late-settling anonymous fetch could repopulate the cache after
 * the login landed. The generation fence closes both halves.
 *
 * Same harness style as the other API tests: a URL-routing fetch fake
 * with per-URL response queues; login goes through the REAL
 * `loginStudent` (whose wiring calls invalidateSessionCache).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { loginStudent } from "../features/auth/api";
import {
  loadSessionForTests,
  peekSessionCacheForTests,
  resetSessionCacheForTests,
} from "../features/auth/session";
import { resetAccessTokenManagerForTests } from "../lib/accessToken";

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

const ME = "/api/v1/me";
const LOGIN = "/api/v1/auth/login";

beforeEach(() => {
  resetAccessTokenManagerForTests();
  resetSessionCacheForTests();
  calls = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), headers: new Headers(init?.headers) });
    return respondWith(String(input));
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("login synchronously invalidates the anonymous session cache (targeted re-review P1)", () => {
  test("owner's sequence: fresh anonymous cache -> login -> new mount refetches with the bearer", async () => {
    route({
      [ME]: [
        json(
          {
            error: {
              code: "AUTHENTICATION_REQUIRED",
              message: "未登录",
              details: null,
              request_id: null,
            },
          },
          401,
        ), // the login page's earlier /me (anonymous)
        json({ id: "u1", username: "s", nickname: "同学", role: "STUDENT" }), // post-login /me
      ],
      [LOGIN]: [
        json({ access_token: "token-1", csrf_token: "ct", token_type: "bearer" }),
      ],
    });

    // The login page HAD fetched /me anonymously; the fresh window
    // holds it (this is exactly what a new shell would consult).
    const anonymous = await loadSessionForTests();
    assert.equal(anonymous.kind, "anonymous");
    const cached = peekSessionCacheForTests();
    assert.ok(cached.fresh);
    assert.equal(cached.result?.kind, "anonymous");

    // Login succeeds — the component navigates/unmounts immediately.
    await loginStudent("student01", "correct-horse");

    // The cache a freshly mounted shell would consult is GONE: the
    // stale anonymous entry must not survive the transition.
    assert.equal(peekSessionCacheForTests().result, null);

    // The new shell loads with the new bearer and reads authenticated.
    const after = await loadSessionForTests();
    assert.equal(after.kind, "authenticated");
    const meCalls = calls.filter((call) => call.url === ME);
    assert.equal(meCalls.length, 2);
    assert.equal(meCalls[1].headers.get("Authorization"), "Bearer token-1");
  });

  test("a late-settling anonymous fetch cannot repopulate the cache after login", async () => {
    const late = Promise.withResolvers<Response>();
    const anonymous401 = () =>
      new Response(
        JSON.stringify({
          error: {
            code: "AUTHENTICATION_REQUIRED",
            message: "未登录",
            details: null,
            request_id: null,
          },
        }),
        { status: 401 },
      );
    route({
      [ME]: [
        () => late.promise, // the login page's anonymous /me, in flight
        anonymous401, // its bearer retry ALSO 401s (the session dies)
        json({ id: "u1", username: "s", nickname: "同学", role: "STUDENT" }),
      ],
      [LOGIN]: [
        json({ access_token: "token-1", csrf_token: "ct", token_type: "bearer" }),
      ],
    });

    const pending = loadSessionForTests();
    await loginStudent("student01", "correct-horse"); // lands while /me is in flight

    // The anonymous result settles AFTER the invalidation — note the
    // P0 guard first retries it with the new bearer (a late anonymous
    // 401 self-heals when a session now exists); the retry ALSO 401s
    // here, so the load still concludes anonymous. The generation
    // fence must drop that post-invalidation cache write.
    late.resolve(anonymous401());
    assert.equal((await pending).kind, "anonymous");
    assert.equal(peekSessionCacheForTests().result, null);

    // The next mount refetches with the bearer instead.
    assert.equal((await loadSessionForTests()).kind, "authenticated");
  });
});
