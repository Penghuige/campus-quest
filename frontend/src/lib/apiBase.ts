/**
 * The same-origin API mount prefix, shared by every API fetcher.
 *
 * Development and CI serve the app at the root (prefix ""); production
 * serves it under a path prefix — NEXT_PUBLIC_API_BASE=/campus with the
 * API proxied at /campus/api/v1. `apiRequest` has always carried the
 * prefix; any OTHER direct fetch of an API path must resolve through
 * the same helper or it escapes the mount; the refresh POST 404ing on
 * the 207 deploy was exactly that (production QA #1 re-test, PR #14
 * follow-up).
 *
 * The environment is read at CALL time so tests can inject the prefix;
 * Next inlines NEXT_PUBLIC_* references in client bundles regardless of
 * where they appear.
 */

/** The configured mount prefix ("/campus" in production, "" otherwise). */
export function apiBasePath(): string {
  return process.env.NEXT_PUBLIC_API_BASE ?? "";
}

/** Resolve an unprefixed same-origin "/api/v1/..." path against the mount. */
export function resolveApiPath(path: string): string {
  return `${apiBasePath()}${path}`;
}
