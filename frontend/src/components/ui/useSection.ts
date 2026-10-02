"use client";
/**
 * Load/retry state for one independently fetched section (patterns §4
 * server state, §5 no waterfalls; design §10 loading/empty/error).
 *
 * QA #2: with a `cacheKey`, the hook rides the shared data cache
 * (lib/dataCache) — a REMOUNTING section (module switch, back
 * navigation) renders its cached snapshot synchronously instead of a
 * skeleton flash, and a background revalidation refreshes when the
 * entry has aged. Callers WITHOUT a key keep the original
 * fetch-on-mount semantics. `retry` (user action / boundary refetch)
 * forces a reload either way.
 *
 * Lint rules still shape the design: the loader lives in a ref updated
 * between effects (never during render), the effect only STARTS the
 * async load (all setState happens in async callbacks), and a
 * cancelled flag keeps out-of-order retries from clobbering newer data.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { readCached } from "@/lib/dataCache";

export type SectionState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: unknown };

export interface UseSectionResult<T> {
  state: SectionState<T>;
  /** Re-enter loading and force a reload (retry / boundary). */
  retry: () => void;
}

export function useSection<T>(
  loader: () => Promise<T>,
  cacheKey?: string,
): UseSectionResult<T> {
  const [state, setState] = useState<SectionState<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const loaderRef = useRef(loader);
  const keyRef = useRef(cacheKey);

  // Keep the refs current between effects; inline arrow loaders passed by
  // rendering islands must not retrigger the fetch effect.
  useEffect(() => {
    loaderRef.current = loader;
    keyRef.current = cacheKey;
  });

  useEffect(() => {
    let cancelled = false;
    const key = keyRef.current;
    if (key !== undefined) {
      const outcome = readCached<T>(key, loaderRef.current, {
        force: attempt > 0,
      });
      if (outcome.kind === "fresh") {
        setState({ status: "ready", data: outcome.data });
        return;
      }
      if (outcome.kind === "stale") {
        // Serve the snapshot now; the revalidation updates (or errors)
        // when it lands — stale-while-error keeps the old data visible.
        setState({ status: "ready", data: outcome.data });
        outcome.revalidation.then(
          (data) => {
            if (!cancelled) {
              setState({ status: "ready", data });
            }
          },
          (error: unknown) => {
            if (!cancelled) {
              setState({ status: "error", error });
            }
          },
        );
        return;
      }
      // absent: fall through to loading + the shared promise.
      setState({ status: "loading" });
      outcome.promise.then(
        (data) => {
          if (!cancelled) {
            setState({ status: "ready", data });
          }
        },
        (error: unknown) => {
          if (!cancelled) {
            setState({ status: "error", error });
          }
        },
      );
      return;
    }
    setState({ status: "loading" });
    loaderRef.current().then(
      (data) => {
        if (!cancelled) {
          setState({ status: "ready", data });
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setState({ status: "error", error });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((value) => value + 1);
  }, []);

  return { state, retry };
}
