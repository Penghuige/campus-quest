/**
 * Memory-only access-token manager (spec §5.6 auth model; PR #4
 * hardening Task 1).
 *
 * Session layout this module owns the client half of:
 * - the LONG-LIVED refresh token never touches JS — it lives only in the
 *   HttpOnly + Secure + SameSite=Lax cookie scoped to the auth paths;
 * - the SHORT-LIVED access token rides the login/refresh RESPONSE BODY
 *   and is kept HERE, in one module-level variable, for the tab's
 *   lifetime: `lib/api.ts` attaches it as `Authorization: Bearer …` on
 *   CampusQuest API requests. It is NEVER written to localStorage,
 *   sessionStorage, cookies, or any other persistent store — a page
 *   reload forgets it, and the refresh-cookie rotation below reissues
 *   it (the cold-start bootstrap).
 * - rotation is SINGLE-FLIGHT: concurrent 401 recoveries (several
 *   sections bootstrapping at once) share one in-flight `POST
 *   /auth/refresh`, so N concurrent callers cause exactly one cookie
 *   rotation — one refresh session, one rotation per wave, never a
 *   thundering herd against the rotate-once semantics.
 *
 * The rotation request authenticates by the HttpOnly cookie alone: no
 * `Authorization` header is attached (a stale bearer would be noise, and
 * the CSRF double-submit header is the mutation proof the endpoint's
 * guard wants when the cookie is presented).
 *
 * Server-side imports see an empty store: only browser flows call the
 * setters, so a Server Component reusing `apiRequest` simply sends no
 * bearer (the backend's 401 stays the authority there).
 */
import { readCsrfToken, CSRF_HEADER_NAME } from "./csrf";
import { observeServerDateHeader } from "./serverClock";

/** The rotation endpoint (also the recursion guard for `lib/api.ts`). */
export const AUTH_REFRESH_PATH = "/api/v1/auth/refresh";

let accessToken: string | null = null;
let refreshInFlight: Promise<boolean> | null = null;
// Auth-transition window (final re-review P0): true between
// beginAuthTransition() and endAuthTransition() — no new refresh may
// start, and the epoch bump inside begin fences every older context's
// recovery. See beginAuthTransition for the cookie-ordering invariant.
let transitionActive = false;
// Auth-context epoch (final re-review P0): bumped by every EXPLICIT
// auth transition (login, logout) and untouched by refresh rotations.
// Requests capture the epoch they were sent under; a 401 landing in a
// LATER epoch means the human behind the tab changed (logout A ->
// login B) — replaying that request with the new account's token would
// execute A's intent against B's account, so it must never happen.
let authEpoch = 0;

/**
 * Record an explicit login: remember the token AND open a new auth
 * context (epoch bump). Rotation through the refresh cookie does NOT
 * bump — it is the same logged-in context renewing its credential.
 */
export function recordLogin(token: string): void {
  accessToken = token;
  authEpoch += 1;
}

/**
 * Record an explicit logout: forget the token AND close the auth
 * context (epoch bump), so in-flight requests from the closed context
 * can be distinguished from a same-session refresh.
 */
export function recordLogout(): void {
  accessToken = null;
  authEpoch += 1;
}

/** The auth-context epoch the caller's requests are running under. */
export function getAuthEpoch(): number {
  return authEpoch;
}

/** Remember a freshly issued access token WITHOUT opening a new context (rotation). */
export function setAccessToken(token: string): void {
  accessToken = token;
}

/** Forget the access token (logout; failed rotation). */
export function clearAccessToken(): void {
  accessToken = null;
}

/** The tab's current access token, or null when none is remembered. */
export function getAccessToken(): string | null {
  return accessToken;
}

/*
 * Cross-tab cold-start coordination (QA defect #1, owner ruling on the
 * PR #10 review): under strict rotate-once the invariant is ONE
 * POST /auth/refresh per cold-start wave — the second rotation kills
 * the first tab's just-minted token (the live-session row requires
 * `replaced_by IS NULL`).
 *
 * Mechanism (round-3 shape):
 * 1. the round-trip runs inside a Web Locks request (same-origin tabs
 *    serialize on "cq:auth-refresh");
 * 2. the wave's winner ROTATES once, then publishes — still under the
 *    lock — a NON-SECRET marker {contextId, at} to localStorage (the
 *    only persistent artifact; the bearer NEVER touches storage) and
 *    the mint (token + the RESPONSE's own csrf_token) to the adoption
 *    BroadcastChannel, holding the lock a short delivery grace;
 * 3. waiters, under the lock: adopt a valid in-memory mint, else read
 *    the marker — AGE-GATED (MINT_FRESH_MS): a fresh marker routes to
 *    a bounded wait on the LONG-LIVED listener (generation-keyed
 *    resolvers + recheck-after-register; a fresh channel cannot replay
 *    an already-posted message), and a timeout there FAILS CLOSED;
 *    an older marker belongs to a finished wave — this tab rotates as
 *    the new winner. The marker serves every waiter of the generation
 *    (never consumed by the first).
 *
 * Cross-tab auth-context fence (rounds 1-3 P0): authEpoch is TAB-LOCAL
 * but cookies are origin-global, so a sibling's explicit login/logout
 * broadcasts a non-secret CONTEXT-RESET. Receivers drop the bearer,
 * bump the epoch, and notify app-level subscribers (onCrossTabAuthReset
 * — the session cache revalidates so mounted UI leaves the old account
 * BEFORE any new mutation can 401-refresh-retry as the new one).
 *
 * Supported baseline: Web Locks + BroadcastChannel (both evergreen).
 * No BroadcastChannel -> the bounded wait cannot exist -> surface false
 * (fail-safe; never a speculative rotation). The server-side grace
 * window (PR #10, default off) is the non-browser/cross-origin
 * backstop, not a substitute.
 */

/** Web Locks name serializing rotations across same-origin tabs. */
const REFRESH_LOCK_NAME = "cq:auth-refresh";

/** How long a tab may WAIT for the lock before surfacing false. */
let refreshLockTimeoutMs = 10_000;

/** Test-only: shrink the cross-tab lock wait budget. */
export function setRefreshLockTimeoutForTests(ms: number): void {
  refreshLockTimeoutMs = ms;
}

/** Adoption notification channel (open listeners hear the mint immediately). */
const ADOPTION_CHANNEL = "cq-auth-adoption";
/** Cross-tab auth-context reset channel (login/logout fence). */
const CONTEXT_RESET_CHANNEL = "cq-auth-reset";
/**
 * localStorage handoff MARKER — NON-SECRET by construction (re-review
 * P1: the bearer NEVER touches persistent storage; the memory-only
 * contract is preserved). The marker only says "a mint for context X
 * was published at T and is arriving on the adoption channel"; the
 * bearer itself moves exclusively through BroadcastChannel (memory,
 * per-tab listeners). Written under the lock, read under the lock —
 * the deterministic lock-to-lock barrier — while the mint's delivery
 * is awaited with a bounded channel wait keyed to the marker's
 * generation (a stale lastAdoption can never shadow a newer wave).
 */
const HANDOFF_MARKER_KEY = "cq:auth-handoff-marker";
/** A published mint older than this is not adoptable (wave-scale freshness). */
const ADOPTION_TTL_MS = 10_000;
/** Bounded channel wait for a marker's mint (delivery is queued while
 *  the winner still holds the lock; this only yields to the task that
 *  drains it). Exceeding it is the fail-safe path — surface false,
 *  never a speculative second rotation). */
const MINT_WAIT_MS = 400;
/**
 * A marker younger than this announces a wave whose mint delivery is
 * IMMINENT (the winner published it under the lock moments ago). A
 * bounded-wait timeout inside this window fails CLOSED — the miss is a
 * delivery anomaly, not wave staleness, and rotating would retire the
 * winner's live generation. Beyond it the wave is stale (round-3 P1b:
 * separate age from consumption; the marker serves all N waiters).
 */
const MINT_FRESH_MS = MINT_WAIT_MS + 100;

interface AdoptionMessage {
  token: string;
  /** The csrf_token from the SAME refresh response that minted the
   *  token (review P1a: never a post-response cookie read — the cookie
   *  is origin-global and a sibling could swap it mid-flight). */
  contextId: string | null;
  at: number;
}

/** The NON-SECRET marker (localStorage-safe: no bearer, no secret). */
interface HandoffMarker {
  contextId: string | null;
  at: number;
}

let lastAdoption: AdoptionMessage | null = null;

/** This tab's id — reset broadcasts from OUR tab must not fence us
 *  (BroadcastChannel delivers to other channel instances in the SAME
 *  context; without this guard the tab that just logged in would drop
 *  its own fresh bearer). */
const TAB_ID =
  typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `tab-${Math.random().toString(36).slice(2)}`;

interface WebLocksLike {
  request: (
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<boolean>,
  ) => Promise<boolean>;
}

function webLocks(): WebLocksLike | null {
  if (typeof navigator === "undefined") {
    return null;
  }
  const locks = (navigator as { locks?: unknown }).locks;
  return typeof locks === "object" && locks !== null &&
    typeof (locks as WebLocksLike).request === "function"
    ? (locks as WebLocksLike)
    : null;
}

/** Read the non-secret handoff marker (localStorage). */
function readMarker(): HandoffMarker | null {
  try {
    const raw =
      typeof localStorage === "undefined" ? null : localStorage.getItem(HANDOFF_MARKER_KEY);
    if (raw === null) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<HandoffMarker> | null;
    if (
      parsed !== null &&
      (parsed.contextId === null || typeof parsed.contextId === "string") &&
      typeof parsed.at === "number"
    ) {
      return { contextId: parsed.contextId ?? null, at: parsed.at };
    }
    return null;
  } catch {
    return null;
  }
}

/** Write the marker — UNDER the lock, BEFORE its release. */
function writeMarker(marker: HandoffMarker): void {
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(HANDOFF_MARKER_KEY, JSON.stringify(marker));
    }
  } catch {
    // Quota/private-mode: the channel mint still goes out; waiters
    // without a marker fall back to lastAdoption-or-fail-safe.
  }
}

function clearMarker(): void {
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.removeItem(HANDOFF_MARKER_KEY);
    }
  } catch {
    // best effort
  }
}

/**
 * Bounded wait for THIS marker generation's mint on the adoption
 * channel. The winner posted the mint while still holding the lock,
 * so the message is already queued; a few macrotask yields drain it.
 * Resolves the mint, or null on timeout (the caller fail-safes).
 */
type MintWait = { kind: "mint"; message: AdoptionMessage } | { kind: "timeout" } | { kind: "no-channel" };

function waitForMint(marker: HandoffMarker): Promise<MintWait> {
  const valid = (message: AdoptionMessage): boolean =>
    message.at >= marker.at - 50 &&
    message.contextId === marker.contextId &&
    Date.now() - message.at < ADOPTION_TTL_MS;
  const already = lastAdoption;
  if (already !== null && valid(already)) {
    return Promise.resolve({ kind: "mint", message: already });
  }
  if (typeof BroadcastChannel === "undefined" && typeof window === "undefined") {
    return Promise.resolve({ kind: "no-channel" });
  }
  return new Promise((resolve) => {
    let done = false;
    const finish = (value: MintWait) => {
      if (done) {
        return;
      }
      done = true;
      pendingMintResolvers.delete(onMint);
      clearTimeout(timer);
      resolve(value);
    };
    const onMint = (message: AdoptionMessage) => {
      if (valid(message)) {
        finish({ kind: "mint", message });
      }
    };
    // Arm the long-lived-listener resolver FIRST, then RECHECK
    // lastAdoption — a mint delivered between the entry check and
    // this registration is still caught (round-3 P1a race).
    pendingMintResolvers.add(onMint);
    if (lastAdoption !== null && valid(lastAdoption)) {
      finish({ kind: "mint", message: lastAdoption });
      return;
    }
    const timer = setTimeout(() => finish({ kind: "timeout" }), MINT_WAIT_MS);
  });
}

/**
 * Pending generation-keyed resolvers for the LONG-LIVED listener (round-3
 * P1a): a fresh BroadcastChannel opened by a waiter cannot replay a
 * message the winner already posted, so waitForMint arms a resolver HERE
 * and the page-lifetime listener resolves it — plus a recheck-after-
 * register for a mint that landed between the initial lastAdoption
 * check and resolver registration.
 */
const pendingMintResolvers = new Set<(message: AdoptionMessage) => void>();

function feedAdoptionMessage(data: Partial<AdoptionMessage> | null): void {
  if (
    data !== null &&
    typeof data.token === "string" &&
    data.token.length > 0 &&
    (data.contextId === null || typeof data.contextId === "string") &&
    typeof data.at === "number"
  ) {
    const message: AdoptionMessage = {
      token: data.token,
      contextId: data.contextId ?? null,
      at: data.at,
    };
    lastAdoption = message;
    for (const resolve of pendingMintResolvers) {
      resolve(message);
    }
    pendingMintResolvers.clear();
  }
}

/** Test seam: feed a mint as the long-lived listener would. */
export function deliverAdoptionForTests(message: AdoptionMessage): void {
  feedAdoptionMessage(message);
}

// Sibling broadcasts may land while this tab still waits on the lock. The
// listener also carries the CONTEXT-RESET fence. Browser-only (`window`
// guard): in Node, BroadcastChannel exists but holds the event loop open.
if (typeof window !== "undefined" && typeof BroadcastChannel !== "undefined") {
  const listener = new BroadcastChannel(ADOPTION_CHANNEL);
  listener.onmessage = (event: MessageEvent) => {
    feedAdoptionMessage(event.data as Partial<AdoptionMessage> | null);
  };
  // Review P0: a sibling's EXPLICIT login/logout is an origin-global
  // auth-context change. Fence locally: drop the stale bearer, bump the
  // epoch (in-flight 401 recoveries from the OLD context can never
  // replay under the new account), AND notify app-level subscribers —
  // the session cache must not keep serving the OLD account's /me to
  // mounted UI (a stale-A click would otherwise 401-refresh-retry as B).
  const resetListener = new BroadcastChannel(CONTEXT_RESET_CHANNEL);
  resetListener.onmessage = (event: MessageEvent) => {
    const data = event.data as { from?: unknown } | null;
    if (data !== null && data.from === TAB_ID) {
      return; // our OWN transition already fenced us locally
    }
    accessToken = null;
    authEpoch += 1;
    lastAdoption = null;
    notifyCrossTabReset();
  };
}

/**
 * Cross-tab auth-context reset subscriptions (round-3 P0): app-level
 * caches (the /me session cache first) subscribe so a SIBLING tab's
 * explicit login/logout is an application-level transition in THIS
 * tab too — mounted useSession consumers revalidate instead of
 * serving the previous account's cached UI.
 */
type ResetListener = () => void;
const resetListeners = new Set<ResetListener>();

/** Subscribe to cross-tab auth-context resets; returns an unsubscriber. */
export function onCrossTabAuthReset(listener: ResetListener): () => void {
  resetListeners.add(listener);
  return () => {
    resetListeners.delete(listener);
  };
}

function notifyCrossTabReset(): void {
  for (const listener of resetListeners) {
    listener();
  }
}

/**
 * Publish the mint: handoff slot first (the deterministic lock-to-lock
 * barrier), then the channel notification for already-open listeners.
 */
function publishMint(token: string, contextIdFromResponse: string | null): void {
  const message: AdoptionMessage = { token, contextId: contextIdFromResponse, at: Date.now() };
  lastAdoption = message;
  // The MARKER is the only persistent artifact — non-secret (re-review
  // P1): contextId is the double-submit csrf value already readable in
  // a JS cookie; `at` is a timestamp. The bearer never leaves memory.
  writeMarker({ contextId: contextIdFromResponse, at: message.at });
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(ADOPTION_CHANNEL);
    channel.postMessage(message);
    channel.close();
  }
}

/**
 * Review P0 fence sender: call at every EXPLICIT auth transition
 * (login/logout) after the transition request settles — tells sibling
 * tabs the origin's auth context changed.
 */
export function broadcastContextReset(): void {
  // The local fence applies EVERYWHERE (the sender is also a tab whose
  // stale handoff must die); the channel notification is browser-only.
  lastAdoption = null;
  clearMarker();
  if (typeof window === "undefined" || typeof BroadcastChannel === "undefined") {
    return;
  }
  const channel = new BroadcastChannel(CONTEXT_RESET_CHANNEL);
  channel.postMessage({ reset: true, from: TAB_ID });
  channel.close();
}

/**
 * Immediate adoption check: the freshest VALID candidate for OUR
 * context. Re-review P1: a stale `lastAdoption` must never shadow a
 * newer wave — validity (fresh + context match) is the filter, and the
 * under-lock path re-checks against the marker generation, so an old
 * in-memory message from a previous wave can neither win over a newer
 * mint nor trigger a second rotation.
 */
function tryAdopt(): string | null {
  if (
    lastAdoption !== null &&
    Date.now() - lastAdoption.at < ADOPTION_TTL_MS &&
    lastAdoption.contextId === readCsrfToken()
  ) {
    return lastAdoption.token;
  }
  return null;
}

/**
 * Rotate the session through the HttpOnly refresh cookie and remember
 * the new access token. Resolves `true` when a usable token came back.
 * Any failure (network, non-2xx, malformed body) forgets the stale
 * token and resolves `false` — the caller then surfaces its original
 * 401 (the session hook reads that as the anonymous state).
 *
 * SINGLE-FLIGHT: while one rotation is pending, every caller receives
 * the SAME promise; the slot frees only after it settles, so a later
 * 401 wave (requests that raced the rotation with the old token) may
 * legitimately start the next one.
 */
export function refreshAccessToken(): Promise<boolean> {
  // No NEW rotation may start while an explicit auth transition
  // (login/logout) is draining or executing: the transition's own
  // network request must be the LAST auth-cookie writer, and a refresh
  // started now could settle after it. Callers see `false` (their
  // original 401 surfaces) — correct, because their auth context is
  // being replaced anyway.
  if (transitionActive) {
    return Promise.resolve(false);
  }
  if (refreshInFlight === null) {
    refreshInFlight = rotate().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function rotate(): Promise<boolean> {
  // The auth context this rotation belongs to: a refresh that started
  // under one epoch and settles under another (its drain straddled a
  // beginAuthTransition) must not write the OLD context's token into
  // the NEW one's memory — and its HTTP response is still awaited by
  // the transition, so its Set-Cookie is applied BEFORE the login/
  // logout request goes out and loses the last-writer race by design.
  const epochAtStart = authEpoch;
  const locks = webLocks();
  if (locks === null) {
    return adoptOrRotate(epochAtStart);
  }
  // Serialize adopt-or-rotate across same-origin tabs. Rejection
  // handling is cause-aware:
  // - the wait TIMED OUT: firing unlocked would re-create the rotation
  //   race the lock exists to prevent — surface false; the next reload
  //   (or the winner's settled broadcast/cookie) recovers, which beats
  //   a guaranteed double-rotation under strict rotate-once;
  // - an explicit auth transition opened while we waited: its drain
  //   owns the cookie ordering — no new rotation — false;
  // - lock infrastructure refused BEFORE our callback ran: degrade to
  //   the unlocked adopt-or-rotate (performRotation is total).
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), refreshLockTimeoutMs);
  try {
    return await locks.request(
      REFRESH_LOCK_NAME,
      { signal: abort.signal },
      () => adoptOrRotate(epochAtStart),
    );
  } catch {
    if (abort.signal.aborted || transitionActive) {
      return false;
    }
    return adoptOrRotate(epochAtStart);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Inside the lock, in order:
 * 1. immediate adoption — the freshest VALID mint already in memory;
 * 2. marker wait — a fresh non-secret marker says THIS wave's mint is
 *    arriving on the channel; bounded-wait for it (the winner posted
 *    it while holding the lock, so it is queued). A stale
 *    lastAdoption from an earlier wave cannot shadow the marker: the
 *    wait validates `at >= marker.at - 50` for THIS generation;
 * 3. fail-safe — no candidate and no marker (or the wait times out /
 *    BroadcastChannel is absent): surface FALSE. Never a speculative
 *    second rotation into the strict rotate-once race (re-review P1);
 * 4. otherwise this tab IS the wave's winner: rotate once, publish the
 *    marker + channel mint, and hold the lock a short delivery grace
 *    so the message is queued before the next waiter acquires.
 */
async function adoptOrRotate(epochAtStart: number): Promise<boolean> {
  if (authEpoch !== epochAtStart || transitionActive) {
    return false;
  }
  const adopted = tryAdopt();
  if (adopted !== null) {
    accessToken = adopted;
    return true;
  }
  const marker = readMarker();
  const markerIsOurs =
    marker !== null &&
    Date.now() - marker.at < ADOPTION_TTL_MS &&
    marker.contextId === readCsrfToken();
  if (marker !== null && markerIsOurs) {
    // Round-3 P1b: the marker stays observable for the WHOLE generation
    // window (TTL) — every waiter in an N-tab wave may route through it;
    // the first waiter no longer removes it (that broke N > 2). Stale
    // waves are separated by AGE, not by consumption: a marker younger
    // than MINT_FRESH_MS announces a wave whose mint is imminent — a
    // timeout there FAILS CLOSED (no rotation; the winner's live
    // generation must not be retired by a missed message); an OLDER
    // marker belongs to a finished wave — this tab may rotate as the
    // new wave's winner (the v3 field-bug path, now age-gated).
    const outcome = await waitForMint(marker);
    if (outcome.kind === "no-channel") {
      return false;
    }
    if (outcome.kind === "mint") {
      if (authEpoch !== epochAtStart || transitionActive) {
        return false;
      }
      accessToken = outcome.message.token;
      return true;
    }
    // timeout on a FRESHLY announced generation: fail closed.
    if (Date.now() - marker.at < MINT_FRESH_MS) {
      return false;
    }
    // stale marker: fall through — this tab is the new wave's winner.
  }
  const ok = await performRotation(epochAtStart);
  if (ok && accessToken !== null && lastMintContext !== undefined) {
    // contextId comes from the REFRESH RESPONSE body (P1a): the token
    // and its context label left the server in the same message.
    publishMint(accessToken, lastMintContext);
    // Delivery grace: keep the lock across a few macrotasks so the
    // mint is QUEUED for every waiter's bounded wait before release.
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return ok;
}

/** The csrf_token riding the last successful refresh response (P1a). */
let lastMintContext: string | null | undefined = undefined;

/** The one network round-trip: POST /auth/refresh with the cookie pair. */
async function performRotation(epochAtStart: number): Promise<boolean> {
  try {
    const headers = new Headers({ Accept: "application/json" });
    const csrfToken = readCsrfToken();
    if (csrfToken !== null) {
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }
    const response = await fetch(AUTH_REFRESH_PATH, {
      method: "POST",
      headers,
      credentials: "include",
    });
    // Feed the shared clock estimate like every other API response.
    observeServerDateHeader(response.headers.get("Date"));
    if (!response.ok) {
      if (authEpoch === epochAtStart) {
        accessToken = null;
      }
      return false;
    }
    const body = (await response.json()) as {
      access_token?: unknown;
      csrf_token?: unknown;
    };
    if (typeof body.access_token !== "string" || body.access_token.length === 0) {
      if (authEpoch === epochAtStart) {
        accessToken = null;
      }
      return false;
    }
    if (authEpoch !== epochAtStart) {
      // The context this rotation served is gone (explicit logout or
      // login opened a new one while it was in flight): the drained
      // response's cookie side effect is ordered before the
      // transition's own request by construction; the MEMORY side
      // effect is simply dropped.
      return false;
    }
    accessToken = body.access_token;
    // P1a: the adoption label rides the SAME response — never a later
    // read of the origin-global cookie a sibling could have swapped.
    lastMintContext =
      typeof body.csrf_token === "string" && body.csrf_token.length > 0
        ? body.csrf_token
        : null;
    return true;
  } catch {
    // Network-level failure: no verdict on the session — forget the
    // stale token and let the caller's original error speak.
    if (authEpoch === epochAtStart) {
      accessToken = null;
    }
    return false;
  }
}

/**
 * Serialize an EXPLICIT auth transition (login/logout) against refresh
 * rotations (final re-review P0): mark the transition, bump the epoch
 * (requests from the closing context stop refreshing/replaying), and
 * DRAIN any in-flight refresh to completion BEFORE the caller sends
 * its own network request. Invariant: once the login/logout request
 * goes out, no older refresh response can still arrive in the future —
 * its Set-Cookie was already applied, so the transition's own cookies
  * are the last writers.
 */
export async function beginAuthTransition(): Promise<void> {
  transitionActive = true;
  authEpoch += 1;
  if (refreshInFlight !== null) {
    await refreshInFlight.catch(() => {
      // The drained rotation's own outcome is irrelevant here — what
      // mattered was ordering its response (and its Set-Cookie) ahead
      // of the transition request.
    });
  }
}

/** Close the transition window opened by beginAuthTransition. */
export function endAuthTransition(): void {
  transitionActive = false;
}

/** Test seam: reset the module singleton between test cases. */
export function resetAccessTokenManagerForTests(): void {
  accessToken = null;
  refreshInFlight = null;
  authEpoch = 0;
  transitionActive = false;
  lastAdoption = null;
  lastMintContext = undefined;
}

/**
 * Test seam: simulate a sibling tab's broadcast landing in the listener
 * (Node tests run without `window`, so the browser-only listener that
 * feeds `lastAdoption` never exists there).
 */
export function receiveAdoptionForTests(message: AdoptionMessage): void {
  lastAdoption = message;
}
