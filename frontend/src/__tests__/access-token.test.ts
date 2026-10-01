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
  broadcastContextReset,
  deliverAdoptionForTests,
  getAccessToken,
  receiveAdoptionForTests,
  refreshAccessToken,
  resetAccessTokenManagerForTests,
  setRefreshLockTimeoutForTests,
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

  test("the winner's rotation BROADCASTS the mint on the adoption channel", async () => {
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
    const message = seen[0] as { token: string };
    assert.equal(message.token, "mint-abc");
    ear.close();
  });

  test("a sibling's fresh same-context broadcast is ADOPTED — zero POSTs", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // No document.cookie in Node -> our contextId is null; a broadcast
    // minted with contextId null matches.
    receiveAdoptionForTests({ token: "sibling-mint", contextId: null, at: Date.now() });
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
    receiveAdoptionForTests({ token: "foreign", contextId: "other-session", at: Date.now() });
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
    receiveAdoptionForTests({ token: "stale", contextId: null, at: Date.now() - 30_000 });
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
  });

  test("P0: a sibling's context-reset fences THIS tab — stale A-mint never adopted, zero replay", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    // A sibling tab logged in/out; the browser-only reset listener is
    // window-guarded, so exercise the fence at the seam the listener
    // writes: the reset sender clears the handoff + adoption locally
    // (and on the channel), so THIS tab's next refresh cannot adopt
    // the dead mint even though it was minted milliseconds ago.
    receiveAdoptionForTests({ token: "token-A", contextId: null, at: Date.now() });
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
    const marker = JSON.parse(markerWrites[0]) as { contextId: string | null };
    assert.equal(marker.contextId, "ctx-response", "label = response csrf_token");
    assert.ok(!("token" in marker), "marker carries no bearer");
    assert.equal(mints.length, 1);
    const mint = mints[0] as { token: string; contextId: string | null };
    assert.equal(mint.token, "mint-x");
    assert.equal(mint.contextId, "ctx-response");
    ear.close();
  });

  test("P1b: marker routes the waiter to a bounded channel wait (mint lands DURING it — zero POSTs)", async () => {
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
      getItem: () => JSON.stringify({ contextId: null, at: waveAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    // Node has no module listener (window-guarded): deliver the mint
    // through the seam the listener would drive.
    deliverAdoptionForTests({ token: "wave-mint", contextId: null, at: waveAt });
    const ok = await pending;
    assert.equal(ok, true);
    assert.equal(posts, 0, "no rotation — the mint arrived within the bounded wait");
    assert.equal(getAccessToken(), "wave-mint");
    (globalThis as { localStorage?: Storage }).localStorage = undefined;
  });

});

// --- re-review round 2: memory-only handoff, no shadowing, fail-safe ---

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
    receiveAdoptionForTests({ token: "old-wave", contextId: null, at: Date.now() - 60_000 });
    const marker = { contextId: null, at: Date.now() };
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify(marker),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    const pending = refreshAccessToken();
    await new Promise((resolve) => setTimeout(resolve, 30));
    deliverAdoptionForTests({ token: "new-wave-mint", contextId: null, at: marker.at });
    const ok = await pending;
    assert.equal(ok, true);
    assert.equal(getAccessToken(), "new-wave-mint", "the NEW wave's mint won — no shadowing");
  });

  test("fail-safe: fresh marker but no mint can arrive (no BroadcastChannel) -> false, zero POSTs", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ contextId: null, at: Date.now() }),
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

// --- round 3: fresh-generation fail-closed + N-waiter marker + missed-message recheck ---

describe("round-3 marker semantics", () => {
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

  test("FRESH marker + missed message -> fail CLOSED (no rotation retires the winner)", async () => {
    resetAccessTokenManagerForTests();
    installLocks(async (_n, _o, cb) => cb());
    // Marker written ~now; the mint NEVER arrives (delivery anomaly).
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ contextId: null, at: Date.now() - 50 }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const ok = await refreshAccessToken();
    assert.equal(ok, false, "fresh-generation timeout fails closed");
    assert.equal(posts, 0, "no rotation — the winner's live generation survives");
  });

  test("N waiters: the marker stays observable — three sequential waiters all adopt one mint", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const waveAt = Date.now() - 30;
    let reads = 0;
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => {
        reads += 1;
        return JSON.stringify({ contextId: null, at: waveAt });
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
      deliverAdoptionForTests({ token: `mint-${index}`, contextId: null, at: waveAt });
      const ok = await pending;
      assert.equal(ok, true, `waiter ${index + 1} adopted`);
      assert.equal(getAccessToken(), `mint-${index}`);
    }
    assert.equal(posts, 0, "zero rotations for the whole N-tab wave");
    assert.ok(reads >= 3, "the marker served every waiter");
  });

  test("missed-message race: the mint lands BETWEEN entry check and resolver registration (recheck catches it)", async () => {
    resetAccessTokenManagerForTests();
    installLocks((_n, _o, cb) => cb());
    const waveAt = Date.now();
    (globalThis as { localStorage?: Storage }).localStorage = {
      getItem: () => JSON.stringify({ contextId: null, at: waveAt }),
      setItem: () => {},
      removeItem: () => {},
    } as unknown as Storage;
    let posts = 0;
    globalThis.fetch = (async () => {
      posts += 1;
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const pending = refreshAccessToken();
    // Deliver exactly in the microtask window after adoptOrRotate read
    // the marker but before waitForMint's resolver is armed... the arm-
    // then-recheck order means ANY delivery in this window still wins.
    await Promise.resolve();
    deliverAdoptionForTests({ token: "race-mint", contextId: null, at: waveAt });
    const ok = await pending;
    assert.equal(ok, true, "recheck-after-register caught the in-window mint");
    assert.equal(posts, 0);
    assert.equal(getAccessToken(), "race-mint");
  });
});
