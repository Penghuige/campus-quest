"use client";
/**
 * Load/retry state for one independently fetched section (patterns §4
 * server state, §5 no waterfalls; design §10 loading/empty/error).
 *
 * Deliberately dependency-free (the session hook's documented stance): the
 * hook is the seam a future server-state library replaces. Lint rules
 * shape the design: the loader lives in a ref updated between effects
 * (never during render), the effect body only STARTS the async load (all
 * setState happens in async callbacks), and `retry()` — an event handler —
 * is what re-enters the loading state. A cancelled flag keeps out-of-order
 * retries from clobbering newer data.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export type SectionState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: unknown };

export interface UseSectionResult<T> {
  state: SectionState<T>;
  /** Re-enter loading and re-run the current loader (retry / boundary). */
  retry: () => void;
}

export function useSection<T>(loader: () => Promise<T>): UseSectionResult<T> {
  const [state, setState] = useState<SectionState<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const loaderRef = useRef(loader);

  // Keep the ref current between effects; inline arrow loaders passed by
  // rendering islands must not retrigger the fetch effect.
  useEffect(() => {
    loaderRef.current = loader;
  });

  useEffect(() => {
    let cancelled = false;
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
