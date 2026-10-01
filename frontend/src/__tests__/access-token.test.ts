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
 * Cross-tab wave coordination (rounds 1-6): Web Locks serialization,
 * BroadcastChannel mint adoption, the context-reset fence, the round-5
 * PROBE/ANSWER liveness — a marker's wave is resolved by an active
 * request/response (a live holder with a USABLE bearer re-posts the
 * mint; the bounded window is a FAILURE DETECTOR, so a suspended holder
 * may be superseded and self-heals) — never by a marker-age guess or a
 * per-tab generation counter, and never re-advertising an expired
 * credential (the round-6 boundary).
 *
 * Harness decision (the stream's documented stance): stub globalThis
 * fetch with a URL-routing fake — no mocking library.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  broadcastContextReset,
  deliverAdoptionForTests,
  getAccessToken,
  receiveAdoptionForTests,
  receiveChannelMessageForTests,
  refreshAccessToken,
  resetAccessTokenManagerForTests,
  setRefreshLockTimeoutForTests,
} from "../lib/accessToken";
import { apiRequest } from "../lib/api";
import { isApiError } from "../lib/errors";
import { observeServerDateHeader, resetServerClockForTests } from "../lib/serverClock";
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

// --- cross-tab coordination: Web Locks + BroadcastChannel adoption (QA #1) -----

describe("cold-start coordination (locks + adoption)", () => {
  type LockFn = (
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<boolean>,
  ) => Promise<boolean>;

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
    resetAccessTokenManagerForTests();
  });

  test("the rotation POST runs INSIDE the lock callback", async () => {
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
    globalThis.fetch = (async () => {
      assert.equal(insideLock, true, "fetch must run inside the lock");
      return new Response(JSON.stringify({ access_token: "tok-1" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(lockName, "cq:auth-refresh");
  });

  test("a lock WAIT timeout surfaces false — never fires unlocked into the race", async () => {
    resetAccessTokenManagerForTests();
    setRefreshLockTimeoutForTests(30);
    installLocks((_name, options) =>
      new Promise((_resolve, reject) => {
        options.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      }),
    );
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, false);
    assert.equal(posts, 0, "no unlocked POST after a lock timeout");
  });

  test("the winner's rotation BROADCASTS the mint (wave-tagged) on the adoption channel", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const seen: unknown[] = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    ear.onmessage = (event: MessageEvent) => seen.push(event.data);
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ access_token: "mint-abc" }), { status: 200 })) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(seen.length, 1);
    const message = seen[0] as { kind: string; waveId: string; token: string };
    assert.equal(message.kind, "mint");
    assert.ok(message.waveId.length > 0, "the mint carries its wave identity");
    assert.equal(message.token, "mint-abc");
    ear.close();
  });

  test("a sibling's fresh same-context broadcast is ADOPTED — zero POSTs", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // No document.cookie in Node -> our contextId is null; a broadcast
    // minted with contextId null matches.
    receiveAdoptionForTests({ waveId: "w-sibling", token: "sibling-mint", contextId: null, sentAt: Date.now(), expiresAt: null });
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(posts, 0, "adoption must not rotate");
    assert.equal(getAccessToken(), "sibling-mint");
  });

  test("a FOREIGN-context broadcast is never adopted (login/logout boundary)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    receiveAdoptionForTests({ waveId: "w-foreign", token: "foreign", contextId: "other-session", sentAt: Date.now(), expiresAt: null });
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "own" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(posts, 1, "own rotation for own context");
    assert.equal(getAccessToken(), "own");
  });

  test("an EXPIRED same-context broadcast is not adopted (TTL)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    receiveAdoptionForTests({ waveId: "w-stale", token: "stale", contextId: null, sentAt: Date.now() - 30_000, expiresAt: null });
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "fresh" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(posts, 1, "expired broadcast must not be adopted");
    assert.equal(getAccessToken(), "fresh");
  });
});

// --- review fix round: P0 fence, P1a response-bound label, P1b handoff barrier ---

describe("cross-tab context-reset fence + deterministic handoff (review round)", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
  });

  test("P0: a sibling's context-reset fences THIS tab — stale A-mint never adopted, zero replay", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // A sibling tab logged in/out; the browser-only reset listener is
    // window-guarded, so exercise the fence at the seam the listener
    // writes: the reset sender clears the handoff + adoption locally
    // (and on the channel), so THIS tab's next refresh cannot adopt
    // the dead mint even though it was minted milliseconds ago.
    receiveAdoptionForTests({ waveId: "w-a", token: "token-A", contextId: null, sentAt: Date.now(), expiresAt: null });
    broadcastContextReset();
    await new Promise((r) => setTimeout(r, 20));
    // The reset clears the handoff + adoption: the next refresh may NOT
    // adopt A's mint even though it was fresh.
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "own-new", csrf_token: "ctx-new" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(posts, 1, "stale A-mint must NOT be adopted after a context reset");
    assert.equal(getAccessToken(), "own-new");
  });

  test("P1a: the label rides the refresh RESPONSE csrf_token; the marker carries NO bearer", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const markerWrites: string[] = [];
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => null,
      setItem: (_k: string, v: string) => markerWrites.push(v),
      removeItem: () => {},
    } as unknown as Storage;
    const ear = new BroadcastChannel("cq-auth-adoption");
    const mints: unknown[] = [];
    ear.onmessage = (event: MessageEvent) => mints.push(event.data);
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ access_token: "mint-x", csrf_token: "ctx-response" }), { status: 200 })) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.equal(markerWrites.length, 1);
    const marker = JSON.parse(markerWrites[0]) as { waveId: string; contextId: string | null };
    assert.equal(marker.contextId, "ctx-response", "label = response csrf_token");
    assert.ok(marker.waveId.length > 0, "marker carries the opaque wave identity");
    assert.ok(!("token" in marker), "marker carries no bearer");
    assert.equal(mints.length, 1);
    const mint = mints[0] as { kind: string; waveId: string; token: string; contextId: string | null };
    assert.equal(mint.kind, "mint");
    assert.equal(mint.waveId, marker.waveId, "mint and marker share the wave identity");
    assert.equal(mint.token, "mint-x");
    assert.equal(mint.contextId, "ctx-response");
    ear.close();
  });

  test("P1b: marker routes the waiter to a probe + bounded wait (mint lands DURING it — zero POSTs)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "loser" }), { status: 200 });
    }) as typeof fetch;
    // This wave's winner already published the marker (under its lock);
    // this waiter has NO lastAdoption (listener delivery delayed) — the
    // mint arrives while the bounded wait is armed.
    const waveAt = Date.now();
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-live", contextId: null, at: waveAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    // Node has no module listener (window-guarded): deliver the mint
    // through the seam the listener would drive.
    deliverAdoptionForTests({ waveId: "w-live", token: "wave-mint", contextId: null, sentAt: waveAt, expiresAt: null });
    const ok = await pending;
    assert.equal(ok, true);
    assert.equal(posts, 0, "no rotation — the mint arrived within the bounded wait");
    assert.equal(getAccessToken(), "wave-mint");
  });

});

// --- memory-only handoff + no-shadowing + fail-safe (re-review round 2) ---

describe("memory-only handoff (re-review round 2)", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(
    lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>,
  ): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
  });

  test("the bearer NEVER reaches localStorage (every persistent write is token-free)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const writes: string[] = [];
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => null,
      setItem: (_k: string, v: string) => writes.push(v),
      removeItem: () => {},
    } as unknown as Storage;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ access_token: "secret-bearer", csrf_token: "ctx" }), { status: 200 })) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.ok(writes.length >= 1, "marker written");
    for (const raw of writes) {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      assert.equal("token" in parsed, false, "no token field in any persistent write");
      assert.ok(!raw.includes("secret-bearer"), "bearer string never persisted");
    }
  });

  test("a stale lastAdoption cannot shadow the marker's newer wave (zero second POST)", async () => {
    resetAccessTokenManagerForTests();
    installLocks(async (_n, _o, cb) => cb());
    receiveAdoptionForTests({ waveId: "w-old", token: "old-wave", contextId: null, sentAt: Date.now() - 60_000, expiresAt: null });
    const marker = { waveId: "w-new", contextId: null, at: Date.now() };
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify(marker),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    deliverAdoptionForTests({ waveId: "w-new", token: "new-wave-mint", contextId: null, sentAt: marker.at, expiresAt: null });
    const ok = await pending;
    assert.equal(ok, true);
    assert.equal(getAccessToken(), "new-wave-mint", "the NEW wave's mint won — no shadowing");
  });

  test("fail-safe: fresh marker but no mint can arrive (no BroadcastChannel) -> false, zero POSTs", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-foreign", contextId: null, at: Date.now() }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const OriginalChannel = globalThis.BroadcastChannel;
    (globalThis as { BroadcastChannel: typeof BroadcastChannel }).BroadcastChannel =
      undefined as unknown as typeof BroadcastChannel;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    try {
      const ok = await refreshAccessToken();
      assert.equal(ok, false, "surface false — never a speculative rotation");
      assert.equal(posts, 0);
    } finally {
      (globalThis as { BroadcastChannel: typeof BroadcastChannel }).BroadcastChannel = OriginalChannel;
    }
  });
});

// --- marker semantics across a wave (round 3, wave-keyed in round 5) ---

describe("marker semantics across a wave", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(
    lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>,
  ): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
  });

  test("N waiters: the marker stays observable — three sequential waiters all adopt one mint", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const waveAt = Date.now() - 30;
    let reads = 0;
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => {
        reads += 1;
        return JSON.stringify({ waveId: "w-n", contextId: null, at: waveAt });
      },
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    // Three waiter-tabs (simulated sequentially: fresh module state, one
    // shared marker, one mint delivered to each wait through the seam).
    for (let index = 0; index < 3; index += 1) {
      resetAccessTokenManagerForTests();
      const pending = refreshAccessToken();
      await new Promise((resolve) => setTimeout(resolve, 10));
      deliverAdoptionForTests({ waveId: "w-n", token: `mint-${index}`, contextId: null, sentAt: waveAt, expiresAt: null });
      const ok = await pending;
      assert.equal(ok, true, `waiter ${index + 1} adopted`);
      assert.equal(getAccessToken(), `mint-${index}`);
    }
    assert.equal(posts, 0, "zero rotations for the whole N-tab wave");
    assert.ok(reads >= 3, "the marker served every waiter");
  });

  test("missed-message race: a mint landing right after arming is caught by the RESOLVER", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const waveAt = Date.now();
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-race", contextId: null, at: waveAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const pending = refreshAccessToken();
    // Deliver in the microtask window after adoptOrRotate read the
    // marker: by then the resolver is already armed (the arm-then-probe
    // span is one synchronous task), so ANY delivery in this window —
    // or later, up to the timeout — still reaches the armed resolver.
    await Promise.resolve();
    deliverAdoptionForTests({ waveId: "w-race", token: "race-mint", contextId: null, sentAt: waveAt, expiresAt: null });
    const ok = await pending;
    assert.equal(ok, true, "the armed resolver caught the in-window mint");
    assert.equal(posts, 0);
    assert.equal(getAccessToken(), "race-mint");
  });

  test("a FOREIGN-context marker is cleared and this tab rotates as the wave leader", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // A marker from ANOTHER auth context (pre-login/logout): not ours —
    // this tab is the new wave's leader: clear it, rotate, publish.
    let removed = false;
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-foreign-ctx", contextId: "other", at: Date.now() }),
      setItem: () => {},
      removeItem: () => { removed = true; },
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "fresh-winner" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(posts, 1);
    assert.equal(getAccessToken(), "fresh-winner");
    assert.equal(removed, true, "foreign-context marker cleared before rotating");
  });
});

// --- round 5: probe/answer liveness + opaque wave ownership ---

describe("round-5 protocol (probe/answer liveness)", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(
    lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>,
  ): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
    (globalThis as { sessionStorage?: Storage }).sessionStorage = undefined;
    (globalThis as { window?: unknown }).window = undefined;
  });

  test("P1b deadlock: a provably DEAD marker (no live holder answers the probe) lets this tab rotate as new leader", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Hours-old wave: the publisher tab is long gone, no listener holds
    // the mint, but the marker persists. Fail-closed-forever stranded
    // every later cold start at the anonymous shell (reload repeats
    // forever). The probe is the failure detector: nobody answers
    // within the bounded window -> the wave is declared failed ->
    // rotate as the new leader.
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-debris", contextId: null, at: Date.now() - 3_600_000 }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "recovery" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true, "a provably dead wave must not strand this tab");
    assert.equal(posts, 1, "rotated once as the new leader");
    assert.equal(getAccessToken(), "recovery");
  });

  test("a 5s-old marker is resolved by the SAME probe (no age heuristic anywhere)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // The old round-4 scenario — a waiter throttled seconds past the
    // wave — is no longer classified by age at all: the probe decides.
    // The probe must actually be POSTED for this wave, and with no
    // holder answering (the wave ended) the waiter rotates.
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-throttled", contextId: null, at: Date.now() - 5_000 }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const probes: Array<{ kind: string; waveId: string }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    ear.onmessage = (event: MessageEvent) => probes.push(event.data as { kind: string; waveId: string });
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "after-throttle" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.ok(
      probes.some((p) => p.kind === "probe" && p.waveId === "w-throttled"),
      "the marker's age never decides — the wave is probed",
    );
    assert.equal(ok, true, "probe verdict, not an age cutoff");
    assert.equal(posts, 1);
    ear.close();
  });

  test("round-5 review P1: a holder whose mint is >10s old still ANSWERS (re-stamped) — no second POST kills the live wave", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Tab A minted W1 eleven seconds ago and is still USING it (access
    // tokens live for minutes). Tab B cold-starts now. If the answer
    // were gated on the ORIGINAL mint's age, A would stay silent, B's
    // probe would "prove" death, and B's rotation would kill A's live
    // token — the exact QA #1 failure. A live holder answering IS the
    // liveness proof; the answer is re-stamped.
    const waveAt = Date.now() - 11_000;
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-old-mint", contextId: null, at: waveAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    // The live holder (11s-old mint for exactly this wave) answers with
    // a RE-STAMPED mint through the listener seam.
    deliverAdoptionForTests({ waveId: "w-old-mint", token: "still-live", contextId: null, sentAt: Date.now(), expiresAt: null });
    const ok = await pending;
    assert.equal(ok, true, "an aged-but-live wave is adoptable via a re-stamped answer");
    assert.equal(posts, 0, "the holder's live token is never rotated away");
    assert.equal(getAccessToken(), "still-live");
  });

  test("round-5 review P2: a TTL-fresh mint from a SUPERSEDED wave is not adopted (marker wave check)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // A sibling already rotated out W-old (its marker names W-new for
    // OUR context) but this tab's memory still holds W-old's fresh
    // mint. Adopting it would replay a server-side-dead token; the
    // marker's wave identity must gate the adoption. With no holder
    // answering the probe, this tab rotates as the new leader.
    receiveAdoptionForTests({ waveId: "w-old", token: "superseded", contextId: null, sentAt: Date.now() - 1_000, expiresAt: null });
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-new", contextId: null, at: Date.now() }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "own-fresh" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(posts, 1, "superseded mint not adopted — own rotation");
    assert.equal(getAccessToken(), "own-fresh");
  });

  test("P1a collision: a sibling's LIVE wave is answered by its holder — the waiter adopts, zero POSTs", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Two tabs once published "generation 1" each (tab-local counters
    // collide). Round-5 identity is an opaque uuid, and liveness is
    // active: this waiter PROBES the wave; the live holder's answer
    // re-posts the mint and the waiter ADOPTS — the sibling's wave is
    // never rotated away.
    const waveAt = Date.now();
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-sib-live", contextId: null, at: waveAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const probes: Array<{ kind: string; waveId: string }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    ear.onmessage = (event: MessageEvent) => probes.push(event.data as { kind: string; waveId: string });
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    // The waiter must have PROBED exactly this wave...
    assert.ok(
      probes.some((p) => p.kind === "probe" && p.waveId === "w-sib-live"),
      "the waiter actively probes the marker's wave",
    );
    // ...and the live holder's answer (re-posted mint) resolves the wait.
    deliverAdoptionForTests({ waveId: "w-sib-live", token: "sib-mint", contextId: null, sentAt: waveAt, expiresAt: null });
    const ok = await pending;
    assert.equal(ok, true);
    assert.equal(posts, 0, "the sibling's live wave was adopted, never rotated");
    assert.equal(getAccessToken(), "sib-mint");
    ear.close();
  });

  test("ANSWERER: a tab holding a TTL-fresh mint RE-POSTS it when its wave is probed", async () => {
    resetAccessTokenManagerForTests();
    receiveAdoptionForTests({ waveId: "w-held", token: "held-mint", contextId: null, sentAt: Date.now(), expiresAt: null });
    const seen: unknown[] = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    ear.onmessage = (event: MessageEvent) => seen.push(event.data);
    // The long-lived listener dispatch (browser-only) driven at its seam.
    receiveChannelMessageForTests({ kind: "probe", waveId: "w-held" });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(seen.length, 1, "the holder answered the probe");
    const answer = seen[0] as { kind: string; waveId: string; token: string };
    assert.equal(answer.kind, "mint");
    assert.equal(answer.waveId, "w-held");
    assert.equal(answer.token, "held-mint");
    ear.close();
  });

  test("ANSWERER: an aged mint with a KNOWN usable expiry answers RE-STAMPED — the vouch is about the credential, not the message", async () => {
    resetAccessTokenManagerForTests();
    // sentAt 30s old (past the transport TTL), exp 15min out: the
    // holder may vouch — message age is not credential age.
    receiveAdoptionForTests({
      waveId: "w-known",
      token: "held",
      contextId: null,
      sentAt: Date.now() - 30_000,
      expiresAt: Date.now() + 900_000,
    });
    const seen: unknown[] = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    try {
      ear.onmessage = (event: MessageEvent) => seen.push(event.data);
      receiveChannelMessageForTests({ kind: "probe", waveId: "w-other" }); // not held
      receiveChannelMessageForTests({ kind: "probe", waveId: "w-known" }); // held, aged but usable
      await new Promise((resolve) => setTimeout(resolve, 30));
      assert.equal(seen.length, 1, "only the HELD wave is answered");
      const answer = seen[0] as { kind: string; waveId: string; token: string; sentAt: number; expiresAt: number };
      assert.equal(answer.kind, "mint");
      assert.equal(answer.waveId, "w-known");
      assert.equal(answer.token, "held");
      assert.ok(
        Date.now() - answer.sentAt < 1_000,
        "known-expiry answer is re-stamped — transport freshness is the answer time",
      );
      assert.ok(
        answer.expiresAt > Date.now(),
        "the credential boundary rides the answer untouched",
      );
    } finally {
      ear.close();
    }
  });

  test("ANSWERER: an aged mint with NO decodable expiry is answered AS-IS — the TTL, not a vouch, bounds it", async () => {
    resetAccessTokenManagerForTests();
    // Unknown expiry cannot be vouched: re-stamping would let an
    // opaque (possibly dead) bearer re-qualify forever. The answer
    // carries the ORIGINAL sentAt; the receiver's TTL decides.
    const originalSentAt = Date.now() - 30_000;
    receiveAdoptionForTests({
      waveId: "w-opaque",
      token: "opaque",
      contextId: null,
      sentAt: originalSentAt,
      expiresAt: null,
    });
    const seen: unknown[] = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    try {
      ear.onmessage = (event: MessageEvent) => seen.push(event.data);
      receiveChannelMessageForTests({ kind: "probe", waveId: "w-opaque" });
      await new Promise((resolve) => setTimeout(resolve, 30));
      assert.equal(seen.length, 1, "the held wave is still answered");
      const answer = seen[0] as { kind: string; sentAt: number };
      assert.equal(answer.kind, "mint");
      assert.equal(answer.sentAt, originalSentAt, "unknown-expiry mints are NEVER re-stamped");
    } finally {
      ear.close();
    }
  });

  test("SAME-tab reload over its OWN dead marker (BC present): probe unanswered -> rotate", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // The round-4 reload regression in round-5 shape: the tab's previous
    // heap published this marker and died with the reload; no holder
    // answers the probe; the tab rotates as the new leader.
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-own-dead", contextId: null, at: Date.now() - 1_000 }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "reload-winner" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true, "reload must not be stranded anonymous by its own dead marker");
    assert.equal(posts, 1, "rotated as the new wave leader");
    assert.equal(getAccessToken(), "reload-winner");
  });

  test("reset clears the published-wave record (ownership must not survive account changes)", async () => {
    resetAccessTokenManagerForTests();
    const removed: string[] = [];
    (globalThis as { sessionStorage?: Storage }).sessionStorage = {
      getItem: () => "w-published",
      setItem: () => {},
      removeItem: (key: string) => removed.push(key),
    } as unknown as Storage;
    broadcastContextReset();
    await new Promise((r) => setTimeout(r, 20));
    assert.ok(
      removed.includes("cq:auth-published-wave"),
      "the published-wave record dies with the auth context",
    );
  });
});

// --- round 5: no-BroadcastChannel fail-safe (probe impossible) ---

describe("round-5 no-channel fail-safe", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(
    lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>,
  ): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
    (globalThis as { sessionStorage?: Storage }).sessionStorage = undefined;
    (globalThis as { window?: unknown }).window = undefined;
  });

  test("browser-present + channel-absent: a FOREIGN wave fails CLOSED (the probe cannot exist)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Simulate the BROWSER shape: window exists, BroadcastChannel does
    // not. Without a channel there is no probe, no proof of death, and
    // no way to adopt: surface false — never a speculative rotation.
    (globalThis as { window?: unknown }).window = {};
    const OriginalChannel = globalThis.BroadcastChannel;
    (globalThis as { BroadcastChannel: typeof BroadcastChannel }).BroadcastChannel =
      undefined as unknown as typeof BroadcastChannel;
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-live-elsewhere", contextId: null, at: Date.now() }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    try {
      const ok = await refreshAccessToken();
      assert.equal(ok, false, "browser no-BC must fail closed, not fall through to rotation");
      assert.equal(posts, 0);
    } finally {
      (globalThis as { BroadcastChannel: typeof BroadcastChannel }).BroadcastChannel = OriginalChannel;
    }
  });

  test("browser-present + channel-absent: THIS tab's own published wave still recovers (reload)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // No channel anywhere, but sessionStorage (survives reload, dies
    // with the tab) proves THIS tab published the marker's wave and its
    // holder is dead: rotating retires nothing. The reload-recovery
    // unlock for the no-BC baseline.
    (globalThis as { window?: unknown }).window = {};
    const OriginalChannel = globalThis.BroadcastChannel;
    (globalThis as { BroadcastChannel: typeof BroadcastChannel }).BroadcastChannel =
      undefined as unknown as typeof BroadcastChannel;
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-mine", contextId: null, at: Date.now() - 500 }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    (globalThis as { sessionStorage?: Storage }).sessionStorage = {
      getItem: () => "w-mine",
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "no-bc-recovery" }), { status: 200 });
    }) as typeof fetch;
    try {
      const ok = await refreshAccessToken();
      assert.equal(ok, true, "own dead wave is provable without a channel");
      assert.equal(posts, 1);
      assert.equal(getAccessToken(), "no-bc-recovery");
    } finally {
      (globalThis as { BroadcastChannel: typeof BroadcastChannel }).BroadcastChannel = OriginalChannel;
    }
  });
});

// --- round 6: credential-expiry boundary on the probe/answer handoff ---

describe("round-6 credential boundary", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(
    lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>,
  ): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  /** A fake bearer JWT whose payload carries `exp` (the advisory decode
   *  target — no signature: the decode only gates OFFERS, never requests). */
  function fakeJwt(expiresAtMs: number): string {
    const b64 = (obj: unknown) =>
      Buffer.from(JSON.stringify(obj)).toString("base64url");
    return `${b64({ alg: "HS256", typ: "JWT" })}.${b64({ exp: Math.floor(expiresAtMs / 1000) })}.sig`;
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
    (globalThis as { sessionStorage?: Storage }).sessionStorage = undefined;
  });

  test("P0 cross-tab: a live holder with an EXPIRED bearer stays SILENT — exactly one real refresh POST, new wave adopted", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Tab A minted wave W an hour ago and is still open — its bearer is
    // long expired but lastAdoption still names W. Tab B cold-starts,
    // sees W's marker and probes. A may NOT answer with the expired
    // credential (re-stamping is transport freshness, never renewal);
    // the probe times out and B rotates a genuinely new wave.
    const expiredAt = Date.now() - 3_600_000;
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-expired", contextId: null, at: expiredAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const seen: Array<{ kind: string; waveId?: string }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    ear.onmessage = (event: MessageEvent) => seen.push(event.data as { kind: string; waveId?: string });
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "genuinely-new", csrf_token: "ctx" }), { status: 200 });
    }) as typeof fetch;
    // The holder tab's state (this process's seam): it holds W's mint,
    // expired an hour ago. Driving its listener with the probe must
    // produce NO answer.
    receiveAdoptionForTests({ waveId: "w-expired", token: "expired-bearer", contextId: null, sentAt: expiredAt, expiresAt: expiredAt });
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    // The probe arrives at the (simulated) live holder; it stays silent.
    receiveChannelMessageForTests({ kind: "probe", waveId: "w-expired" });
    const ok = await pending;
    assert.equal(ok, true, "the waiter recovered by rotating");
    assert.equal(posts, 1, "exactly one real refresh POST");
    assert.equal(getAccessToken(), "genuinely-new");
    assert.ok(
      seen.some((m) => m.kind === "probe" && m.waveId === "w-expired"),
      "the wave was probed",
    );
    assert.ok(
      !seen.some((m) => m.kind === "mint" && m.waveId === "w-expired"),
      "an expired bearer is never re-advertised",
    );
    ear.close();
  });

  test("P0 same-tab: the holder's own EXPIRED token rotates instead of self-adopting forever", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // The same tab's 401 recovery: it probes its own wave; its own
    // long-lived listener receives the probe but must not answer with
    // the expired self. The timeout lets the tab rotate as the new
    // leader — the self-adopt loop of round-6 review P0 is broken.
    const expiredAt = Date.now() - 3_600_000;
    receiveAdoptionForTests({ waveId: "w-self", token: "own-expired", contextId: null, sentAt: expiredAt, expiresAt: expiredAt });
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-self", contextId: null, at: expiredAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "fresh-after-loop", csrf_token: "ctx" }), { status: 200 });
    }) as typeof fetch;
    const seen: Array<{ kind: string; waveId?: string }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    try {
      ear.onmessage = (event: MessageEvent) => seen.push(event.data as { kind: string; waveId?: string });
      const pending = refreshAccessToken();
      await new Promise((resolve) => setTimeout(resolve, 30));
      // The tab's own listener hears its own probe and must stay silent.
      receiveChannelMessageForTests({ kind: "probe", waveId: "w-self" });
      await new Promise((resolve) => setTimeout(resolve, 30));
      const ok = await pending;
      assert.equal(ok, true, "rotation, not self-adoption of the expired token");
      assert.equal(posts, 1);
      assert.equal(getAccessToken(), "fresh-after-loop");
      assert.ok(
        !seen.some((m) => m.kind === "mint" && m.waveId === "w-self"),
        "the own listener never answers with the expired self (load-bearing silence)",
      );
    } finally {
      ear.close();
    }
  });

  test("margin: a bearer within the expiry margin is spent — the holder stays silent", async () => {
    resetAccessTokenManagerForTests();
    receiveAdoptionForTests({
      waveId: "w-margin",
      token: "almost-dead",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() + 2_000, // inside the 5s margin
    });
    const seen: unknown[] = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    ear.onmessage = (event: MessageEvent) => seen.push(event.data);
    receiveChannelMessageForTests({ kind: "probe", waveId: "w-margin" });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(seen.length, 0, "a near-expiry credential is not advertised");
    ear.close();
  });

  test("tryAdopt refuses a transport-fresh mint whose bearer is expired (no marker)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    receiveAdoptionForTests({
      waveId: "w-fresh-transport",
      token: "expired-anyway",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() - 1_000,
    });
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "own-rotation", csrf_token: "ctx" }), { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.equal(posts, 1, "expired bearer not adopted from memory");
    assert.equal(getAccessToken(), "own-rotation");
  });

  test("receiver gate: an answer whose bearer is EXPIRED is refused — the waiter rotates (mutation-pinned)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // The receiving side of the credential boundary: a holder answers
    // with a re-stamped TRANSPORT time but an expired credential. The
    // resolver must NOT adopt it; the wait times out and this tab
    // rotates as the new leader. (Removing the valid() expiry gate
    // must turn this red — the mutation the review pass proved nothing
    // else covered.)
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-late", contextId: null, at: Date.now() }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "rotated-new", csrf_token: "ctx" }), { status: 200 });
    }) as typeof fetch;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    deliverAdoptionForTests({
      waveId: "w-late",
      token: "expired-anyway",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() - 1_000,
    });
    const ok = await pending;
    assert.equal(ok, true, "the expired answer was not adopted");
    assert.equal(posts, 1, "the waiter rotated exactly once");
    assert.equal(getAccessToken(), "rotated-new");
  });

  test("publishMint rides the bearer JWT's exp at birth (advisory decode)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const expiresAt = Date.now() + 900_000;
    const mints: Array<{ kind: string; waveId: string; expiresAt: number | null }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    ear.onmessage = (event: MessageEvent) => mints.push(event.data as { kind: string; waveId: string; expiresAt: number | null });
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ access_token: fakeJwt(expiresAt), csrf_token: "ctx-e" }), { status: 200 })) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.equal(mints.length, 1);
    assert.ok(mints[0].expiresAt !== null, "the mint carries the decoded exp");
    assert.ok(
      Math.abs((mints[0].expiresAt ?? 0) - expiresAt) < 2_000,
      "expiresAt = the JWT exp in ms",
    );
    ear.close();
  });
});

// --- round 7: resolver lifecycle + server-time expiry ---

describe("round-7 resolver lifecycle + server-time expiry", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(
    lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>,
  ): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    resetServerClockForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
    (globalThis as { sessionStorage?: Storage }).sessionStorage = undefined;
  });

  test("P1: an UNRELATED mint must not disarm the active wave's resolver", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Waiting on W2 (resolver armed). A delayed mint from the superseded
    // W1 wave arrives FIRST — well-formed, wrong wave. The W2 resolver
    // rejects it and MUST STAY ARMED: the real W2 answer still arrives
    // well inside the window and is adopted with zero POSTs. (On the
    // pre-fix code the global resolver clear let W1 disarm W2; the live
    // responsive wave was then rotated away by the timeout.)
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w2", contextId: null, at: Date.now() }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response(JSON.stringify({ access_token: "loser", csrf_token: "ctx" }), { status: 200 });
    }) as typeof fetch;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    // The unrelated queued mint from the older wave (validly shaped).
    deliverAdoptionForTests({
      waveId: "w1-old",
      token: "unrelated",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() + 900_000,
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    // The REAL W2 answer, comfortably inside the window.
    deliverAdoptionForTests({
      waveId: "w2",
      token: "w2-mint",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() + 900_000,
    });
    const ok = await pending;
    assert.equal(ok, true);
    assert.equal(posts, 0, "the live responsive W2 wave must not be rotated away");
    assert.equal(getAccessToken(), "w2-mint");
  });

  test("P2: expiry compares against the SERVER-clock estimate — a slow local clock must not vouch a server-expired bearer", async () => {
    resetAccessTokenManagerForTests();
    // Observed Date header puts the server ~30s AHEAD of our local
    // clock. A bearer with 20s of local-clock life left is ALREADY
    // expired server-side: the holder must stay silent.
    observeServerDateHeader(new Date(Date.now() + 30_000).toUTCString());
    receiveAdoptionForTests({
      waveId: "w-slowclock",
      token: "locally-alive",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() + 20_000,
    });
    const seen: unknown[] = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    try {
      ear.onmessage = (event: MessageEvent) => seen.push(event.data);
      receiveChannelMessageForTests({ kind: "probe", waveId: "w-slowclock" });
      await new Promise((resolve) => setTimeout(resolve, 30));
      assert.equal(seen.length, 0, "server-expired bearers are not vouched by a slow local clock");
    } finally {
      ear.close();
    }
  });
});

// --- round 8: observed-mint vs held-credential state split ---

describe("round-8 holder state (observed vs held)", () => {
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");

  function installLocks(
    lock: (n: string, o: { signal?: AbortSignal }, cb: () => Promise<boolean>) => Promise<boolean>,
  ): void {
    Object.defineProperty(globalThis, "navigator", {
      value: { locks: { request: lock } },
      configurable: true,
      writable: true,
    });
  }

  afterEach(() => {
    if (navigatorDescriptor !== undefined) {
      Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    }
    resetAccessTokenManagerForTests();
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
    (globalThis as { sessionStorage?: Storage }).sessionStorage = undefined;
  });

  test("P1: an unrelated OBSERVED mint must not make the HOLDER forget its own wave", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // This tab HOLDS W2 (adopted earlier). A delayed mint from the
    // superseded W1 wave arrives on the channel — observed traffic. It
    // may wake W1 waiters, but it must never overwrite the tab's memory
    // of the wave it actually holds: a later W2 probe must still be
    // answered with the W2 bearer (on the pre-fix code lastAdoption was
    // a single shared slot, W1 silently replaced W2, and a responsive
    // live holder went mute — the waiter then rotated it away).
    receiveAdoptionForTests({
      waveId: "w2",
      token: "w2-bearer",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() + 900_000,
    });
    const seen: Array<{ kind: string; waveId?: string; token?: string }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    try {
      ear.onmessage = (event: MessageEvent) => seen.push(event.data as { kind: string; waveId?: string; token?: string });
      // The unrelated/superseded mint arrives from the channel.
      deliverAdoptionForTests({
        waveId: "w1-old",
        token: "w1-bearer",
        contextId: null,
        sentAt: Date.now(),
        expiresAt: Date.now() + 900_000,
      });
      await new Promise((resolve) => setTimeout(resolve, 10));
      // A waiter probes W2 — the holder must STILL answer with W2.
      receiveChannelMessageForTests({ kind: "probe", waveId: "w2" });
      await new Promise((resolve) => setTimeout(resolve, 30));
      const answer = seen.find((m) => m.kind === "mint" && m.waveId === "w2");
      assert.ok(answer, "the holder answered the W2 probe despite the W1 traffic");
      assert.equal(answer.token, "w2-bearer", "the answer carries the HELD credential, not the observed one");
    } finally {
      ear.close();
    }
  });

  test("PUBLISHER holds its wave: after rotating, a probe for the published wave is answered (production held-write pin)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Pins the PRODUCTION write of the held slot in publishMint: a tab
    // that rotated and published must become a holder for that wave —
    // removing `heldMint = message` there leaves later probes unanswered
    // and the live wave rotated away (the same failure shape one round
    // later; mutation-verified untested before this pin).
    const markerWrites: string[] = [];
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => null,
      setItem: (_k: string, v: string) => markerWrites.push(v),
      removeItem: () => {},
    } as unknown as Storage;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ access_token: "pub-bearer", csrf_token: "ctx" }), { status: 200 })) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, true);
    assert.ok(markerWrites.length >= 1, "marker written");
    const marker = JSON.parse(markerWrites[0]) as { waveId: string };
    const seen: Array<{ kind: string; waveId?: string; token?: string }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    try {
      ear.onmessage = (event: MessageEvent) => seen.push(event.data as { kind: string; waveId?: string; token?: string });
      receiveChannelMessageForTests({ kind: "probe", waveId: marker.waveId });
      await new Promise((resolve) => setTimeout(resolve, 30));
      const answer = seen.find((m) => m.kind === "mint" && m.waveId === marker.waveId);
      assert.ok(answer, "the publisher answers for its own live wave");
      assert.equal(answer.token, "pub-bearer");
    } finally {
      ear.close();
    }
  });

  test("ADOPTION promotes into the held slot: a tab that adopted a wave answers later probes for it", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // Pins the PRODUCTION promotion in adoptOrRotate's adoption branch:
    // taking a credential from the channel must make the tab its holder.
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ waveId: "w-ad", contextId: null, at: Date.now() }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    deliverAdoptionForTests({
      waveId: "w-ad",
      token: "ad-bearer",
      contextId: null,
      sentAt: Date.now(),
      expiresAt: Date.now() + 900_000,
    });
    const ok = await pending;
    assert.equal(ok, true);
    assert.equal(posts, 0, "adopted, not rotated");
    const seen: Array<{ kind: string; waveId?: string; token?: string }> = [];
    const ear = new BroadcastChannel("cq-auth-adoption");
    try {
      ear.onmessage = (event: MessageEvent) => seen.push(event.data as { kind: string; waveId?: string; token?: string });
      receiveChannelMessageForTests({ kind: "probe", waveId: "w-ad" });
      await new Promise((resolve) => setTimeout(resolve, 30));
      const answer = seen.find((m) => m.kind === "mint" && m.waveId === "w-ad");
      assert.ok(answer, "the adopter answers for the wave it took");
      assert.equal(answer.token, "ad-bearer");
    } finally {
      ear.close();
    }
  });

  // (The waiter-side outcome of this fix — zero POSTs, live wave adopted
  // after unrelated traffic — is pinned by the round-7 resolver test; the
  // holder/waiter split it depends on only exists across two real tabs,
  // which the opt-in wave-invariant e2e exercises as one-rotation-per-
  // wave, though it does not inject delayed superseded traffic.)
});
