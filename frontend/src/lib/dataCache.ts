/**
 * Shared server-state cache (QA #2: module switches refetched EVERYTHING).
 *
 * The pattern session.ts proved for /me, generalized to the data surface:
 * - one module-level store keyed by request identity (the caller's
 *   stable cache key — typically the request path + serialized query);
 * - STALE-WHILE-REVALIDATE within a short window: a remounting section
 *   renders its cached snapshot SYNCHRONOUSLY (no skeleton flash on
 *   module switches / back-navigation) while a background revalidation
 *   refreshes when the entry has aged;
 * - concurrent consumers of one key SHARE the in-flight request;
 * - an AUTH GENERATION fence drops the whole store on login/logout —
 *   one user's data can never serve the next (the session cache's
 *   generation rule, applied to every key at once);
 * - errors are never cached as success: a failed revalidation leaves
 *   the previous snapshot in place (stale-while-error) and surfaces
 *   to the caller through the returned promise; a failed FIRST load
 *   simply rejects.
 *
 * Scope rules (patterns §3/§4): GET-shaped section loads only —
 * mutations never read this cache, and explicit boundary refetches
 * (post-claim, post-redeem…) pass `force` to bypass freshness. This
 * layer serves PRESENTATION freshness; server verdicts stay
 * authoritative, and every section still owns its loading/empty/error
 * triad (design §10).
 */

/** How long a cached entry serves without revalidation. */
const FRESH_MS = 15_000;

interface Entry {
  generation: number;
  fetchedAt: number;
  /** Last successful snapshot; undefined until the first load lands. */
  data: unknown;
  inflight: Promise<unknown> | null;
}

const store = new Map<string, Entry>();
let generation = 0;

/** Drop every cached datum — call on every auth transition. */
export function invalidateDataCache(): void {
  generation += 1;
  store.clear();
}

/** Test seam: what a freshly mounted consumer would see. */
export function peekDataCacheForTests(): { size: number; generation: number } {
  return { size: store.size, generation };
}

/** Test seam: reset the store between cases. */
export function resetDataCacheForTests(): void {
  store.clear();
  generation = 0;
}

function isFresh(entry: Entry): boolean {
  return Date.now() - entry.fetchedAt < FRESH_MS;
}

export type ReadOutcome<T> =
  /** Fresh hit: serve synchronously, nothing is running. */
  | { kind: "fresh"; data: T }
  /** Stale hit: serve the snapshot now; the promise settles with newer
   *  data (or rejects — keep the snapshot, surface the error). */
  | { kind: "stale"; data: T; revalidation: Promise<T> }
  /** No usable snapshot: first load (or post-invalidation). */
  | { kind: "absent"; promise: Promise<T> };

/**
 * Read one key. `force` bypasses freshness (boundary refetches /
 * user retries) while still deduping onto any live in-flight request.
 */
export function readCached<T>(
  key: string,
  loader: () => Promise<T>,
  options: { force?: boolean } = {},
): ReadOutcome<T> {
  const cached = store.get(key);
  const valid = cached !== undefined && cached.generation === generation;

  // Fresh and not forced: serve with no network at all.
  if (valid && cached !== undefined && isFresh(cached) && !options.force) {
    return { kind: "fresh", data: cached.data as T };
  }

  // Start (or join) the in-flight request.
  let inflight = valid && cached !== undefined ? cached.inflight : null;
  if (inflight === null) {
    const myGeneration = generation;
    inflight = loader().then(
      (data) => {
        if (generation === myGeneration) {
          store.set(key, {
            generation: myGeneration,
            fetchedAt: Date.now(),
            data,
            inflight: null,
          });
        }
        return data;
      },
      (error: unknown) => {
        // Failure: keep any previous snapshot; free the in-flight slot
        // so the next reader retries instead of joining a dead promise.
        if (generation === myGeneration) {
          const current = store.get(key);
          if (current !== undefined && current.inflight === inflight) {
            current.inflight = null;
          }
        }
        throw error;
      },
    );
    if (generation === myGeneration) {
      store.set(key, {
        generation: myGeneration,
        fetchedAt: cached !== undefined && valid ? cached.fetchedAt : 0,
        data: cached !== undefined && valid ? cached.data : undefined,
        inflight,
      });
    }
  }

  if (valid && cached !== undefined && cached.data !== undefined) {
    return { kind: "stale", data: cached.data as T, revalidation: inflight as Promise<T> };
  }
  return { kind: "absent", promise: inflight as Promise<T> };
}
