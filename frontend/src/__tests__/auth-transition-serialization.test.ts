/**
 * The final re-review P0 regression: an EXPLICIT auth transition
 * (login/logout) must serialize against in-flight refresh rotations.
 * The invariant: once the login/logout request goes out, no older
 * refresh response can still arrive in the future — its Set-Cookie was
 * applied BEFORE the transition's own request, so the transition is
 * the last auth-cookie writer; and a drained rotation from a closed
 * context never writes the memory token either.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  getAccessToken,
  recordLogin,
  refreshAccessToken,
  resetAccessTokenManagerForTests,
} from "../lib/accessToken";
import { apiRequest } from "../lib/api";
import { isApiError } from "../lib/errors";
import { loginStudent, logout } from "../features/auth/api";

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

function tokenPair(access: string): unknown {
  return { access_token: access, csrf_token: "ct", token_type: "bearer" };
}

const REFRESH = "/api/v1/auth/refresh";
const LOGOUT = "/api/v1/auth/logout";
const LOGIN = "/api/v1/auth/login";
const PROTECTED = "/api/v1/protected";

function urls(): string[] {
  return calls.map((call) => call.url);
}

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

describe("auth-transition serialization (final re-review P0)", () => {
  test("A: logout drains the in-flight refresh before its request — and the drained rotation cannot restore the token", async () => {
    recordLogin("token-A");
    const refreshA = Promise.withResolvers<Response>();
    route({
      [REFRESH]: [() => refreshA.promise],
      [LOGOUT]: [json(undefined, 204)],
      [PROTECTED]: [json({ ok: true })],
    });

    // refresh-A is in flight (a 401 recovery started it) and stays
    // pending.
    const refreshOutcome = refreshAccessToken();
    const loggingOut = logout();

    // The logout request must NOT race ahead of refresh-A: while the
    // rotation is pending, no logout fetch has gone out.
    await Promise.resolve(); // let the transition begin drain
    assert.ok(!urls().includes(LOGOUT), "logout must wait for the refresh drain");

    // refresh-A settles (its Set-Cookie applies NOW — before logout's).
    refreshA.resolve(new Response(JSON.stringify(tokenPair("token-A2")), { status: 200 }));
    assert.equal(await refreshOutcome, false); // epoch-fenced: no memory write

    // NOW the logout request goes out and completes against the
    // current session; its cookies are the last writers.
    await loggingOut;
    assert.ok(urls().indexOf(LOGOUT) > urls().indexOf(REFRESH));
    assert.equal(getAccessToken(), null); // A2 never restored

    // And no FUTURE older refresh exists to restore anything: a fresh
    // protected request in the new (anonymous) epoch bootstraps only
    // through a NEW refresh, which is the next writer by construction.
    assert.equal(urls().filter((url) => url === REFRESH).length, 1);
  });

  test("B: login-B does not complete before refresh-A is drained; afterwards no old refresh can overwrite token-B", async () => {
    recordLogin("token-A");
    const refreshA = Promise.withResolvers<Response>();
    route({
      [REFRESH]: [() => refreshA.promise],
      [LOGIN]: [json(tokenPair("token-B"))],
    });

    const refreshOutcome = refreshAccessToken();
    const login = loginStudent("student-b", "correct-horse");
    await Promise.resolve();
    assert.ok(!urls().includes(LOGIN), "login must wait for the refresh drain");

    refreshA.resolve(new Response(JSON.stringify(tokenPair("token-A2")), { status: 200 }));
    assert.equal(await refreshOutcome, false); // A2 write fenced

    const tokens = await login;
    assert.ok(urls().indexOf(LOGIN) > urls().indexOf(REFRESH));
    assert.equal(tokens.access_token, "token-B");
    assert.equal(getAccessToken(), "token-B"); // old A2 never overwrote B
  });

  test("C: a 401 arriving while a transition is active starts NO new refresh", async () => {
    recordLogin("token-A");
    const login = Promise.withResolvers<Response>();
    route({
      [LOGIN]: [() => login.promise],
      [REFRESH]: [
        json(tokenPair("must-never-start"), ),
      ],
      [PROTECTED]: [
        json(
          {
            error: {
              code: "AUTHENTICATION_REQUIRED",
              message: "x",
              details: null,
              request_id: null,
            },
          },
          401,
        ),
      ],
    });

    // A login is in progress (transition window open)...
    const loggingIn = loginStudent("student-b", "correct-horse");
    await Promise.resolve(); // transition began; request pending

    // An unrelated protected request 401s during the window: its
    // recovery must not start a rotation.
    await assert.rejects(
      apiRequest<{ ok: boolean }>(PROTECTED),
      (error: unknown) => isApiError(error) && error.status === 401,
    );
    assert.equal(urls().filter((url) => url === REFRESH).length, 0);

    login.resolve(new Response(JSON.stringify(tokenPair("token-B")), { status: 200 }));
    await loggingIn;
    assert.equal(getAccessToken(), "token-B");
  });
});
