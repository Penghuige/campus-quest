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

/**
 * Cross-tab rotation lock (Web Locks API). The backend's refresh cookie
 * is ROTATE-ONCE: each POST /auth/refresh consumes the cookie it was
 * presented. Two tabs cold-starting simultaneously (or two localhost
 * ports sharing the cookie jar) therefore used to race — the winner's
 * rotation consumed the cookie and the loser's refresh answered 401,
 * parking that tab on the login screen until a manual reload. Holding
 * this lock for the network round-trip serializes rotations ACROSS
 * tabs: the second tab waits for the first tab's Set-Cookie to land in
 * the shared jar, then rotates with the FRESH cookie — every tab ends
 * with its own valid credential. (In-tab callers are already deduped by
 * the single-flight slot; browsers without navigator.locks keep the old
 * behavior — every evergreen browser ships it.)
 */
const REFRESH_LOCK_NAME = "cq:auth-refresh";

/** How long a tab may WAIT for another tab's rotation before giving up
 * on serialization and firing unlocked (degrades to the old racy path
 * rather than hanging a login). */
const REFRESH_LOCK_TIMEOUT_MS = 10_000;

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
    return performRotation(epochAtStart);
  }
  // Serialize the round-trip across tabs (see REFRESH_LOCK_NAME); on a
  // lock timeout or rejection, degrade to the unlocked rotation rather
  // than surfacing a false anonymous state.
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), REFRESH_LOCK_TIMEOUT_MS);
  try {
    return await locks.request(
      REFRESH_LOCK_NAME,
      { signal: abort.signal },
      () => performRotation(epochAtStart),
    );
  } catch {
    return performRotation(epochAtStart);
  } finally {
    clearTimeout(timer);
  }
}

async function performRotation(epochAtStart: number): Promise<boolean> {
  const headers = new Headers({ Accept: "application/json" });
  const csrfToken = readCsrfToken();
  if (csrfToken !== null) {
    headers.set(CSRF_HEADER_NAME, csrfToken);
  }
  try {
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
    const body = (await response.json()) as { access_token?: unknown };
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

/** Test-only: reset the module singleton between test cases. */
export function resetAccessTokenManagerForTests(): void {
  accessToken = null;
  refreshInFlight = null;
  authEpoch = 0;
  transitionActive = false;
}
