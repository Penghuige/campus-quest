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
 * PR #10 review): every cold start rotates the refresh cookie, and the
 * server's live-session row (`replaced_by IS NULL`) accepts exactly one
 * lineage tip — so N tabs rotating concurrently used to leave the
 * losers logged out. The contract is ONE rotation per cold-start wave:
 *
 * 1. the round-trip runs inside a Web Locks request (same-origin tabs
 *    serialize on it);
 * 2. the tab that first reaches the lock ROTATES once and PUBLISHES
 *    the minted access token to a SYNCHRONOUS same-origin handoff slot
 *    (localStorage — read UNDER the lock by the next waiter) plus a
 *    BroadcastChannel notification for open listeners;
 * 3. every sibling that reaches the lock afterwards ADOPTS that mint
 *    (bound to the REFRESH RESPONSE's own csrf_token — never a later
 *    cookie read) instead of rotating — one POST per wave, and the one
 *    minted token stays live for all tabs.
 *
 * Cross-tab auth-context fence (review P0): authEpoch is TAB-LOCAL but
 * cookies are origin-global, so a sibling tab's explicit login/logout
 * must fence THIS tab too. Every explicit transition broadcasts a
 * non-secret CONTEXT-RESET; receivers bump their local epoch, drop the
 * stale in-memory bearer, and invalidate caches BEFORE any 401
 * recovery can replay an old account's intent under the new one.
 *
 * Degraded-baseline contract (review P1c): the handoff slot is
 * localStorage, NOT BroadcastChannel — the wave invariant holds with
 * or without BC delivery. Without Web Locks the rotation degrades to
 * the documented legacy racy behavior (the supported browser baseline
 * is "Web Locks available" — every evergreen browser since 2021).
 * The server-side grace window (PR #10, default off) is the
 * non-browser/cross-origin backstop, not a substitute.
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
/** localStorage handoff slot: written UNDER the lock, read UNDER the lock —
 *  the deterministic barrier BroadcastChannel delivery cannot provide. */
const HANDOFF_SLOT_KEY = "cq:auth-handoff";
/** A published mint older than this is not adoptable (wave-scale freshness). */
const ADOPTION_TTL_MS = 10_000;

interface AdoptionMessage {
  token: string;
  /** The csrf_token from the SAME refresh response that minted the
   *  token (review P1a: never a post-response cookie read — the cookie
   *  is origin-global and a sibling could swap it mid-flight). */
  contextId: string | null;
  at: number;
}

let lastAdoption: AdoptionMessage | null = null;

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

/** The synchronous same-origin handoff slot (localStorage). */
function readHandoff(): AdoptionMessage | null {
  try {
    const raw = typeof localStorage === "undefined" ? null : localStorage.getItem(HANDOFF_SLOT_KEY);
    if (raw === null) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<AdoptionMessage> | null;
    if (
      parsed !== null &&
      typeof parsed.token === "string" &&
      parsed.token.length > 0 &&
      (parsed.contextId === null || typeof parsed.contextId === "string") &&
      typeof parsed.at === "number"
    ) {
      return {
        token: parsed.token,
        contextId: parsed.contextId ?? null,
        at: parsed.at,
      };
    }
    return null;
  } catch {
    return null;
  }
}

/** Write the mint to the handoff slot — called UNDER the lock, BEFORE
 *  its release, so the next waiter's read is deterministic. */
function writeHandoff(message: AdoptionMessage): void {
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(HANDOFF_SLOT_KEY, JSON.stringify(message));
    }
  } catch {
    // Quota/private-mode: the BroadcastChannel notification still goes
    // out; the invariant degrades to "best effort" for this wave only.
  }
}

// Sibling broadcasts may land while this tab still waits on the lock. The
// listener also carries the CONTEXT-RESET fence. Browser-only (`window`
// guard): in Node, BroadcastChannel exists but holds the event loop open.
if (typeof window !== "undefined" && typeof BroadcastChannel !== "undefined") {
  const listener = new BroadcastChannel(ADOPTION_CHANNEL);
  listener.onmessage = (event: MessageEvent) => {
    const data = event.data as Partial<AdoptionMessage> | null;
    if (
      data !== null &&
      typeof data.token === "string" &&
      data.token.length > 0 &&
      (data.contextId === null || typeof data.contextId === "string") &&
      typeof data.at === "number"
    ) {
      lastAdoption = {
        token: data.token,
        contextId: data.contextId ?? null,
        at: data.at,
      };
    }
  };
  // Review P0: a sibling's EXPLICIT login/logout is an origin-global
  // auth-context change. Fence locally: drop the stale bearer and bump
  // the epoch so in-flight 401 recoveries from the OLD context can
  // never replay under the new account. (Session/data caches invalidate
  // via their own subscriptions — see invalidateSessionCache callers.)
  const resetListener = new BroadcastChannel(CONTEXT_RESET_CHANNEL);
  resetListener.onmessage = () => {
    accessToken = null;
    authEpoch += 1;
    lastAdoption = null;
  };
}

/**
 * Publish the mint: handoff slot first (the deterministic lock-to-lock
 * barrier), then the channel notification for already-open listeners.
 */
function publishMint(token: string, contextIdFromResponse: string | null): void {
  const message: AdoptionMessage = { token, contextId: contextIdFromResponse, at: Date.now() };
  lastAdoption = message;
  writeHandoff(message);
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
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.removeItem(HANDOFF_SLOT_KEY);
    }
  } catch {
    // best effort
  }
  if (typeof window === "undefined" || typeof BroadcastChannel === "undefined") {
    return;
  }
  const channel = new BroadcastChannel(CONTEXT_RESET_CHANNEL);
  channel.postMessage({ reset: true });
  channel.close();
}

/** Adopt a sibling's fresh mint for OUR context, or null. */
function tryAdopt(): string | null {
  const candidate = lastAdoption ?? readHandoff();
  if (
    candidate !== null &&
    Date.now() - candidate.at < ADOPTION_TTL_MS &&
    candidate.contextId === readCsrfToken()
  ) {
    return candidate.token;
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
 * Inside the lock: adopt a sibling's fresh mint for our auth context, or
 * rotate ONCE and publish the mint. Either way at most one POST per
 * wave reaches the server from this tab.
 */
function adoptOrRotate(epochAtStart: number): Promise<boolean> {
  const adopted = tryAdopt();
  if (adopted !== null && authEpoch === epochAtStart && !transitionActive) {
    accessToken = adopted;
    return Promise.resolve(true);
  }
  return performRotation(epochAtStart).then((ok) => {
    if (ok && lastMintContext !== undefined) {
      // contextId comes from the REFRESH RESPONSE body (P1a): the token
      // and its context label left the server in the same message.
      publishMint(accessToken as string, lastMintContext);
    }
    return ok;
  });
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
