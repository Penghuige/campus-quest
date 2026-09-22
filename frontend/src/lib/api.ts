/**
 * Same-origin typed API client for the CampusQuest backend (spec §28
 * `/api/v1` surface).
 *
 * Auth model (spec §5.6 / backend `identity/routing_common.py`):
 * - the refresh token lives ONLY in an HttpOnly + Secure + SameSite=Lax
 *   cookie scoped to the auth paths — this client never reads, stores, or
 *   accepts tokens in localStorage or in JS variables;
 * - the SHORT-LIVED access token sits in the memory-only manager
 *   (`lib/accessToken.ts`) after login or a refresh rotation, and every
 *   request automatically carries it as `Authorization: Bearer …`. With
 *   no remembered token NO header is sent — the backend's 401 then
 *   triggers the bootstrap below (cold start after a reload);
 * - a 401 on an API request recovers exactly ONCE through the
 *   single-flight `POST /auth/refresh` (HttpOnly cookie + CSRF header)
 *   and retries the original request with the rotated bearer. Requests
 *   under `/api/v1/auth/` never enter that loop (their 401s are
 *   verdicts — wrong credentials, dead challenge — not session
 *   expiry), and neither do caller-supplied `Authorization` headers
 *   (the pending staff TOTP session owns its credential);
 * - every request sends cookies via `credentials: "include"` (same-origin);
 * - a CSRF double-submit token sits in the NON-HttpOnly `csrf_token`
 *   cookie; every mutating request echoes it in the `X-CSRF-Token` header.
 *
 * Request IDs: the backend resolves `X-Request-ID` on requests and echoes
 * the resolved id on every response (`app/core/observability.py`). Pass
 * `init.requestId` to forward a caller's id; error responses surface the
 * id on `ApiError.requestId`.
 *
 * Errors: any non-2xx response rejects with the typed `ApiError` from
 * `lib/errors` (envelope fields preserved, code-based branching). Network
 * failures reject with the browser's original `TypeError` — distinguish
 * with `isApiError`.
 *
 * Server clock: every response's `Date` header feeds the shared
 * server-clock offset store (`lib/serverClock.ts`), so countdowns driven
 * by `useNow` tick against server time (spec §9.3; patterns §14). The
 * `Date` header is a CORS-safelisted response header, so same-origin and
 * proxied responses expose it alike.
 *
 * Environment: browser first (relative same-origin `path`). Importing from
 * a Server Component is type-safe, but server-side callers must wrap their
 * own cookie-forwarding fetch; `document.cookie` CSRF pickup only exists
 * in the browser.
 */
import { observeServerDateHeader } from "./serverClock";
import { toApiError } from "./errors";
import { getAccessToken, getAuthEpoch, refreshAccessToken } from "./accessToken";
import { readCsrfToken, CSRF_HEADER_NAME } from "./csrf";

/** Header the backend accepts on requests and echoes on responses. */
export const REQUEST_ID_HEADER = "X-Request-ID";

/**
 * The identity surface's own endpoints. Their 401s are VERDICTS (wrong
 * credentials, dead OTP/invitation token), never an expired-session
 * signal, so the refresh-retry loop below never enters this prefix —
 * and `/auth/refresh` itself is structurally excluded from recursion.
 */
const AUTH_API_PREFIX = "/api/v1/auth/";

export interface ApiRequestInit extends Omit<RequestInit, "body"> {
  /** Request body; plain objects are JSON-encoded automatically. */
  body?: unknown;
  /** Forwarded as the `X-Request-ID` header (pass-through, optional). */
  requestId?: string;
}

function hasNativeBody(value: unknown): boolean {
  return (
    typeof value === "string" ||
    value instanceof FormData ||
    value instanceof URLSearchParams ||
    value instanceof Blob ||
    value instanceof ArrayBuffer
  );
}

/**
 * Fetch a CampusQuest API endpoint and return its decoded JSON body as `T`.
 *
 * - `path` is a same-origin API path such as `/api/v1/me`;
 * - plain-object bodies are JSON-encoded with `Content-Type: application/json`;
 * - 204/205 responses resolve to `undefined`;
 * - non-2xx responses throw `ApiError` built from the §29 envelope;
 * - the memory-only access token rides as `Authorization: Bearer …`
 *   when one is remembered and the caller did not supply their own.
 */
export async function apiRequest<T>(
  path: string,
  init: ApiRequestInit = {},
): Promise<T> {
  return performApiRequest<T>(path, init, true);
}

/**
 * One request attempt. `allowRefreshRetry` is false on the retry itself:
 * a request recovers through at most ONE rotation, so a second 401 (the
 * rotation did not help, or the endpoint's own verdict) surfaces as-is.
 */
async function performApiRequest<T>(
  path: string,
  init: ApiRequestInit,
  allowRefreshRetry: boolean,
): Promise<T> {
  const { body: initBody, headers: initHeaders, method: initMethod, requestId, ...rest } = init;
  const method = (initMethod ?? "GET").toUpperCase();

  const headers = new Headers(initHeaders);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  // Caller-owned Authorization wins (the pending staff TOTP session
  // passes its confined bearer explicitly); otherwise the memory-only
  // manager's token rides, and its 401-recovery applies. tokenUsed /
  // authEpochUsed remember WHAT this request carried and UNDER WHICH
  // auth context — the recovery block below distinguishes a same-
  // context refresh rotation from a logout/login context switch.
  const callerOwnsAuthorization = headers.has("Authorization");
  let tokenUsed: string | null = null;
  let authEpochUsed = 0;
  if (!callerOwnsAuthorization) {
    tokenUsed = getAccessToken();
    authEpochUsed = getAuthEpoch();
    if (tokenUsed !== null) {
      headers.set("Authorization", `Bearer ${tokenUsed}`);
    }
  }

  let body: BodyInit | undefined;
  if (initBody !== undefined) {
    if (hasNativeBody(initBody)) {
      body = initBody as BodyInit;
    } else {
      body = JSON.stringify(initBody);
      if (!headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
    }
  }

  // Cookie-authenticated mutations must carry the double-submit token.
  if (method !== "GET" && method !== "HEAD") {
    const csrfToken = readCsrfToken();
    if (csrfToken !== null) {
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }
  }

  if (requestId !== undefined) {
    headers.set(REQUEST_ID_HEADER, requestId);
  }

  const response = await fetch(path, {
    ...rest,
    method,
    headers,
    body,
    credentials: "include",
  });

  // Feed the shared clock estimate from every response (success or
  // error alike — the header rides both). Missing headers are a no-op.
  observeServerDateHeader(response.headers.get("Date"));

  // Session-expiry recovery (cold-start bootstrap included): rotate the
  // HttpOnly refresh cookie once, then retry the original request with
  // the fresh bearer. The failed attempt's body is left unread.
  if (
    response.status === 401 &&
    allowRefreshRetry &&
    !callerOwnsAuthorization &&
    !path.startsWith(AUTH_API_PREFIX)
  ) {
    if (getAuthEpoch() !== authEpochUsed) {
      // Auth-context switch (final re-review P0): the human behind the
      // tab changed (logout A -> login B) since this request was sent.
      // Replaying it with the NEW account's token would execute the OLD
      // account's intent against the new one — never do that; surface
      // the original 401 unchanged (and never rotate for a dead
      // context either).
    } else {
      // Late-stale-401 guard (targeted re-review P0): within the SAME
      // auth context, this 401 may arrive after another request's
      // rotation already replaced the token it used. Rotating again
      // would revoke the session the first retry is riding (the
      // backend's rotate-once semantics revoke the predecessor), so
      // when the manager already holds a DIFFERENT token, retry
      // directly with it — no second rotation.
      const current = getAccessToken();
      if (current !== null && current !== tokenUsed) {
        return performApiRequest<T>(path, init, false);
      }
      const rotated = await refreshAccessToken();
      if (rotated) {
        return performApiRequest<T>(path, init, false);
      }
    }
  }

  if (response.status === 204 || response.status === 205) {
    return undefined as T;
  }

  const requestIdHeader = response.headers.get(REQUEST_ID_HEADER);
  const text = await response.text();
  let parsed: unknown = null;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }

  if (!response.ok) {
    throw toApiError(response.status, parsed, requestIdHeader);
  }
  return parsed as T;
}
