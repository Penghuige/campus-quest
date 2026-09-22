/**
 * CSRF double-submit material (spec §5.6/§33.1; backend
 * `identity/routing_common.py`): the NON-HttpOnly `csrf_token` cookie is
 * the readable half of the pair, the `X-CSRF-Token` header the echoed
 * half. Shared by the API client (`lib/api.ts`) and the access-token
 * manager (`lib/accessToken.ts`, whose rotation POST must satisfy the
 * same guard) so the pair is read in exactly one place.
 */
export const CSRF_COOKIE_NAME = "csrf_token";
export const CSRF_HEADER_NAME = "X-CSRF-Token";

/** Read the double-submit CSRF token from the non-HttpOnly cookie. */
export function readCsrfToken(): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${CSRF_COOKIE_NAME}=`));
  if (!match) {
    return null;
  }
  try {
    return decodeURIComponent(match.slice(CSRF_COOKIE_NAME.length + 1));
  } catch {
    // A malformed escape sequence must never break the request path; a
    // cookie we cannot decode simply provides no CSRF token.
    return null;
  }
}
