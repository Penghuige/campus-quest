/**
 * PR #4 hardening Task 1: the memory-only access-token lifecycle — the
 * four owner-named regressions plus the manager's own pins.
 *
 * 1. login stores the body access token; the NEXT protected request
 *    carries it as `Authorization: Bearer …`.
 * 2. cold-start bootstrap: a token-less /me 401 rotates the HttpOnly
 *    refresh cookie once, retries with the NEW bearer, and succeeds.
 * 3. concurrent bootstraps share ONE rotation (single-flight).
 * 4. logout forgets the memory token (even when the revoke call fails).
 *
 * Plus: no token -> no Authorization header (the backend's 401 owns the
 * bootstrap); the rotation POST carries the CSRF double-submit pair and
 * cookie credentials but no bearer; a failed rotation keeps the 401 as
 * the session hook's anonymous signal; the token NEVER reaches
 * localStorage/sessionStorage; the pending staff invitation token is
 * deliberately NOT promoted into the shared manager.
 *
 * Harness decision (the stream's documented stance): stub globalThis
 * fetch with a URL-routing fake — no mocking library.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  getAccessToken,
  refreshAccessToken,
  resetAccessTokenManagerForTests,
} from "../lib/accessToken";
import { apiRequest } from "../lib/api";
import { isApiError } from "../lib/errors";
import {
  acceptStaffInvitation,
  loginStaff,
  loginStudent,
  logout,
} from "../features/auth/api";

type RecordedRequest = {
  url: string;
  method: string;
  headers: Headers;
  credentials: RequestCredentials | undefined;
};

let calls: RecordedRequest[];
let respondWith: (url: string) => Response;
const originalFetch = globalThis.fetch;

/** Responses per call for one URL (shift per request). */
function route(responses: Record<string, Array<() => Response>>): void {
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
    { error: { code: "AUTHENTICATION_REQUIRED", message: "未登录或登录状态已失效", details: null, request_id: null } },
    401,
  );
}

function tokenPair(access: string): unknown {
  return { access_token: access, csrf_token: "ct", token_type: "bearer" };
}

/** Storage-write recorder: the pin that nothing ever persists the token. */
const storageWrites: string[] = [];
function installStorageRecorders(): void {
  const recorder: Storage = {
    get length() {
      return 0;
    },
    clear() {},
    getItem() {
      return null;
    },
    key() {
      return null;
    },
    removeItem() {},
    setItem(key: string) {
      storageWrites.push(key);
    },
  } as Storage;
  (globalThis as { localStorage?: Storage }).localStorage = recorder;
  (globalThis as { sessionStorage?: Storage }).sessionStorage = recorder;
}

beforeEach(() => {
  resetAccessTokenManagerForTests();
  calls = [];
  storageWrites.length = 0;
  installStorageRecorders();
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      headers: new Headers(init?.headers),
      credentials: init?.credentials,
    });
    return respondWith(String(input));
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  delete (globalThis as { localStorage?: Storage }).localStorage;
  delete (globalThis as { sessionStorage?: Storage }).sessionStorage;
});

describe("regression 1: login stores the token; the next request carries it", () => {
  test("student login -> Bearer on the following protected request", async () => {
    route({
      "/api/v1/auth/login": [json(tokenPair("at-student"))],
      "/api/v1/points/me": [json({ available_points: 1, earned_points: 1, spendable_points: 1, point_debt: 0 })],
    });
    await loginStudent("20240001", "correct-horse");
    assert.equal(getAccessToken(), "at-student");

    await apiRequest("/api/v1/points/me");
    assert.equal(calls[1]?.headers.get("Authorization"), "Bearer at-student");
    assert.equal(calls[1]?.credentials, "include");
    assert.equal(storageWrites.length, 0); // memory only, never storage
  });

  test("staff login -> Bearer too (the staff session is a full session)", async () => {
    route({
      "/api/v1/auth/staff/login": [json(tokenPair("at-staff"))],
      "/api/v1/me": [json({ id: "u1" })],
    });
    await loginStaff("teacher@school.edu", "correct-horse", "234567");
    await apiRequest("/api/v1/me");
    assert.equal(calls[1]?.headers.get("Authorization"), "Bearer at-staff");
  });

  test("no token in memory -> NO Authorization header (the 401 owns the bootstrap)", async () => {
    route({ "/api/v1/me": [json({ id: "u1" })] });
    await apiRequest("/api/v1/me");
    assert.equal(calls[0]?.headers.get("Authorization"), null);
  });

  test("the pending staff invitation token is NOT promoted into the manager", async () => {
    route({
      "/api/v1/auth/staff/invitations/accept": [json(tokenPair("pending-only"))],
    });
    await acceptStaffInvitation("invite-token", "correct-horse");
    assert.equal(getAccessToken(), null); // confined to /staff/totp/* by React state
  });
});

describe("regression 2: cold-start bootstrap -> refresh -> new Bearer -> /me succeeds", () => {
  test("a token-less 401 rotates once and retries the original request", async () => {
    route({
      "/api/v1/me": [envelope401(), json({ id: "u1", nickname: "小明" })],
      "/api/v1/auth/refresh": [json(tokenPair("at-boot"))],
    });

    const me = await apiRequest<{ id: string }>("/api/v1/me");
    assert.equal(me.id, "u1");

    // Wire order: bare /me -> rotation (cookie + CSRF, no bearer) -> /me
    // with the NEW bearer.
    assert.deepEqual(
      calls.map((call) => call.url),
      ["/api/v1/me", "/api/v1/auth/refresh", "/api/v1/me"],
    );
    assert.equal(calls[0]?.headers.get("Authorization"), null);
    assert.equal(calls[1]?.method, "POST");
    assert.equal(calls[1]?.credentials, "include");
    assert.equal(calls[1]?.headers.get("Authorization"), null);
    assert.equal(calls[2]?.headers.get("Authorization"), "Bearer at-boot");
    assert.equal(getAccessToken(), "at-boot");
  });

  test("the rotation POST carries the CSRF double-submit header when the cookie exists", async () => {
    const globe = globalThis as { document?: unknown };
    globe.document = { cookie: "theme=dark; csrf_token=tok-boot" };
    try {
      route({
        "/api/v1/me": [envelope401(), json({ id: "u1" })],
        "/api/v1/auth/refresh": [json(tokenPair("at-csrf"))],
      });
      await apiRequest("/api/v1/me");
      assert.equal(calls[1]?.headers.get("X-CSRF-Token"), "tok-boot");
    } finally {
      delete globe.document;
    }
  });

  test("a failed rotation (no refresh cookie) keeps the 401 as the anonymous signal", async () => {
    route({
      "/api/v1/me": [envelope401()],
      "/api/v1/auth/refresh": [envelope401()],
    });
    const error = await apiRequest("/api/v1/me").then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
    );
    assert.ok(isApiError(error));
    assert.equal(error.status, 401);
    assert.equal(error.code, "AUTHENTICATION_REQUIRED");
    assert.equal(getAccessToken(), null);
  });

  test("a login 401 never triggers the rotation loop (verdict, not expiry)", async () => {
    route({
      "/api/v1/auth/login": [envelope401()],
    });
    await loginStudent("20240001", "wrong").then(
      () => assert.fail("expected rejection"),
      () => {},
    );
    assert.equal(calls.length, 1); // no /auth/refresh attempt
  });
});

describe("regression 3: concurrent bootstraps rotate exactly once (single-flight)", () => {
  test("three concurrent 401 recoveries share ONE rotation", async () => {
    let rotations = 0;
    respondWith = (url: string) => {
      if (url === "/api/v1/auth/refresh") {
        rotations += 1;
        return json(tokenPair(`at-${rotations}`))();
      }
      // /me answers 401 until a rotation has happened, 200 after.
      return rotations === 0 ? envelope401()() : json({ id: "u1" })();
    };

    const results = await Promise.all([
      apiRequest<{ id: string }>("/api/v1/me"),
      apiRequest<{ id: string }>("/api/v1/me"),
      apiRequest<{ id: string }>("/api/v1/me"),
    ]);
    assert.deepEqual(results.map((me) => me.id), ["u1", "u1", "u1"]);
    assert.equal(rotations, 1);
    assert.equal(getAccessToken(), "at-1");
    // 3 failed attempts + 3 retries — and only one rotation call.
    assert.equal(calls.filter((call) => call.url === "/api/v1/auth/refresh").length, 1);
    assert.equal(calls.filter((call) => call.url === "/api/v1/me").length, 6);
  });

  test("the manager itself dedupes concurrent refreshAccessToken calls", async () => {
    let rotations = 0;
    respondWith = () => {
      rotations += 1;
      return json(tokenPair("at-dedup"))();
    };
    const [a, b, c] = await Promise.all([
      refreshAccessToken(),
      refreshAccessToken(),
      refreshAccessToken(),
    ]);
    assert.deepEqual([a, b, c], [true, true, true]);
    assert.equal(rotations, 1);
    assert.equal(getAccessToken(), "at-dedup");
  });

  test("a settled rotation frees the slot — a LATER 401 wave may rotate again", async () => {
    let rotations = 0;
    respondWith = () => {
      rotations += 1;
      return json(tokenPair(`at-${rotations}`))();
    };
    await refreshAccessToken();
    await refreshAccessToken();
    assert.equal(rotations, 2); // sequential, not deduped — by design
  });
});

describe("regression 4: logout forgets the memory token", () => {
  test("logout POSTs the revoke and clears the token", async () => {
    route({
      "/api/v1/auth/login": [json(tokenPair("at-session"))],
      "/api/v1/auth/logout": [() => new Response(null, { status: 204 })],
      "/api/v1/me": [json({ id: "u1" })],
    });
    await loginStudent("20240001", "correct-horse");
    await logout();

    assert.equal(calls[1]?.url, "/api/v1/auth/logout");
    assert.equal(calls[1]?.method, "POST");
    assert.equal(getAccessToken(), null);

    await apiRequest("/api/v1/me");
    assert.equal(calls[2]?.headers.get("Authorization"), null);
    assert.equal(storageWrites.length, 0);
  });

  test("a failed revoke still clears the token (the client never keeps it)", async () => {
    route({
      "/api/v1/auth/login": [json(tokenPair("at-session"))],
      "/api/v1/auth/logout": [
        json({ error: { code: "INTERNAL_ERROR", message: "服务暂不可用", details: null, request_id: null } }, 500),
      ],
    });
    await loginStudent("20240001", "correct-horse");
    await logout().then(
      () => assert.fail("expected rejection"),
      () => {},
    );
    assert.equal(getAccessToken(), null);
  });
});

// --- cross-tab rotation lock (concurrent-cold-start fix) --------------------------

describe("refresh rotation serializes across tabs (Web Locks)", () => {
  type LockFn = (
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<boolean>,
  ) => Promise<boolean>;

  // Node 22 exposes `navigator` as a getter-only property — install the
  // fake via defineProperty and restore the original descriptor after.
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installNavigator(value: unknown): void {
    Object.defineProperty(globalThis, "navigator", {
      value,
      configurable: true,
      writable: true,
    });
  }

  function installLocks(lock: LockFn): void {
    installNavigator({ locks: { request: lock } });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    } else {
      installNavigator(undefined);
    }
  });

  test("the rotation POST runs INSIDE navigator.locks.request", async () => {
    resetAccessTokenManagerForTests();
    let insideLock = false;
    let lockName = "";
    installLocks(async (name, _options, callback) => {
      lockName = name;
      insideLock = true;
      const result = await callback();
      insideLock = false;
      return result;
    });
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      assert.equal(insideLock, true, "fetch must run inside the lock callback");
      calls.push(String(input));
      return new Response(JSON.stringify({ access_token: "tok-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;

    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.deepEqual(calls, ["/api/v1/auth/refresh"]);
    assert.equal(lockName, "cq:auth-refresh");
    assert.equal(getAccessToken(), "tok-1");
  });

  test("a lock-rejection degrades to the unlocked rotation (never a false anonymous)", async () => {
    resetAccessTokenManagerForTests();
    installLocks(async () => {
      throw new DOMException("aborted", "AbortError");
    });
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ access_token: "tok-2" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })) as typeof fetch;

    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(getAccessToken(), "tok-2");
  });

  test("without navigator.locks the rotation runs unlocked (legacy browsers)", async () => {
    resetAccessTokenManagerForTests();
    installNavigator({});
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ access_token: "tok-3" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })) as typeof fetch;

    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(getAccessToken(), "tok-3");
  });
});
