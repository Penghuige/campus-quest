/**
 * Same-origin typed API client for the CampusQuest backend (spec §28
 * `/api/v1` surface).
 *
 * Auth model (spec §5.6 / backend `identity/routing_common.py`):
 * - the refresh token lives ONLY in an HttpOnly + Secure + SameSite=Lax
 *   cookie scoped to the auth paths — this client never reads, stores, or
 *   accepts tokens in localStorage or in JS variables;
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
 * Environment: browser first (relative same-origin `path`). Importing from
 * a Server Component is type-safe, but server-side callers must wrap their
 * own cookie-forwarding fetch; `document.cookie` CSRF pickup only exists
 * in the browser.
 */
import { toApiError } from "./errors";

/** Header the backend accepts on requests and echoes on responses. */
export const REQUEST_ID_HEADER = "X-Request-ID";

const CSRF_COOKIE_NAME = "csrf_token";
const CSRF_HEADER_NAME = "X-CSRF-Token";

export interface ApiRequestInit extends Omit<RequestInit, "body"> {
  /** Request body; plain objects are JSON-encoded automatically. */
  body?: unknown;
  /** Forwarded as the `X-Request-ID` header (pass-through, optional). */
  requestId?: string;
}

/** Read the double-submit CSRF token from the non-HttpOnly cookie. */
export function readCsrfToken(): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${CSRF_COOKIE_NAME}=`));
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE_NAME.length + 1)) : null;
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
 * - non-2xx responses throw `ApiError` built from the §29 envelope.
 */
export async function apiRequest<T>(
  path: string,
  init: ApiRequestInit = {},
): Promise<T> {
  const { body: initBody, headers: initHeaders, method: initMethod, requestId, ...rest } = init;
  const method = (initMethod ?? "GET").toUpperCase();

  const headers = new Headers(initHeaders);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
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
