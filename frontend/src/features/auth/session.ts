"use client";
/**
 * Session bootstrap: a minimal fetch-based session query hook for
 * `GET /api/v1/me`.
 *
 * Library choice: `docs/quality/frontend-patterns.md` classifies session as
 * server state and does not prescribe a data-fetching library, so this is
 * a dependency-free hook with SWR-style staleness semantics (documented
 * below) rather than pulling in SWR/TanStack Query for one endpoint. When
 * a library is adopted for the wider data surface, this hook is the seam
 * to replace.
 *
 * SWR-style semantics implemented here:
 * - one module-level cache; within `STALE_AFTER_MS` (30s) consumers get
 *   the cached result synchronously on mount — no flash of "loading";
 * - beyond the fresh window the previous value stays displayed while a
 *   background revalidation runs (stale-while-revalidate);
 * - concurrent mounts share one in-flight request (deduplication);
 * - the tab refetches on `focus`/`visibilitychange` only when stale;
 * - `refresh()` forces a refetch for post-login/post-logout transitions.
 *
 * Anonymous vs. error: `GET /me` answers 401 with the §29 envelope code
 * `AUTHENTICATION_REQUIRED` when no live session exists — that is the
 * anonymous state, not an error. Everything else (including a 401 without
 * a proper envelope) is surfaced as `status: "error"`.
 *
 * CSRF note: this hook only reads session state. Mutations elsewhere go
 * through `apiRequest`, which reads the non-HttpOnly `csrf_token` cookie
 * and echoes it in the `X-CSRF-Token` header on every mutating request
 * (backend `identity/routing_common.py` double-submit contract).
 */
import { useCallback, useEffect, useState } from "react";

import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";
import { isApiError } from "@/lib/errors";

/** Owner's account view (backend `MePublic`, spec §40). */
export type MeProfile = components["schemas"]["MePublic"];

export type SessionState =
  | { status: "loading" }
  | { status: "authenticated"; me: MeProfile }
  | { status: "anonymous" }
  | { status: "error"; error: unknown };

type SessionResult =
  | { kind: "authenticated"; me: MeProfile }
  | { kind: "anonymous" };

const STALE_AFTER_MS = 30_000;

let cache: { result: SessionResult; fetchedAt: number } | null = null;
let inflight: Promise<SessionResult> | null = null;

function toState(result: SessionResult): SessionState {
  return result.kind === "authenticated"
    ? { status: "authenticated", me: result.me }
    : { status: "anonymous" };
}

async function fetchSession(): Promise<SessionResult> {
  try {
    const me = await apiRequest<MeProfile>("/api/v1/me");
    return { kind: "authenticated", me };
  } catch (error) {
    if (isApiError(error) && error.code === "AUTHENTICATION_REQUIRED") {
      return { kind: "anonymous" };
    }
    throw error;
  }
}

function load(): Promise<SessionResult> {
  if (inflight !== null) {
    return inflight;
  }
  const request: Promise<SessionResult> = fetchSession().then((result) => {
    cache = { result, fetchedAt: Date.now() };
    return result;
  });
  inflight = request;
  // Clear the dedupe slot when settled; the handled copy prevents this
  // settlement from surfacing as an unhandled rejection — real callers
  // already receive the outcome through `request` itself.
  request.then(
    () => {
      inflight = null;
    },
    () => {
      inflight = null;
    },
  );
  return request;
}

function isFresh(): boolean {
  return cache !== null && Date.now() - cache.fetchedAt < STALE_AFTER_MS;
}

export interface UseSessionResult {
  state: SessionState;
  /** Force a refetch (call after login/logout mutations). */
  refresh: () => void;
}

export function useSession(): UseSessionResult {
  // Synchronous cache hit keeps SSR and fresh navigations flash-free.
  const [state, setState] = useState<SessionState>(() =>
    isFresh() && cache !== null ? toState(cache.result) : { status: "loading" },
  );
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const apply = (result: SessionResult) => {
      if (!cancelled) {
        setState(toState(result));
      }
    };
    const fail = (error: unknown) => {
      if (!cancelled) {
        setState({ status: "error", error });
      }
    };

    // First run honors the fresh window; a `refresh()` revision always
    // bypasses it so login/logout transitions refetch unconditionally.
    if (revision === 0 && isFresh() && cache !== null) {
      apply(cache.result);
    } else {
      load().then(apply, fail);
    }

    const revalidateIfStale = () => {
      if (!isFresh()) {
        load().then(apply, () => {});
      }
    };
    document.addEventListener("visibilitychange", revalidateIfStale);
    window.addEventListener("focus", revalidateIfStale);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", revalidateIfStale);
      window.removeEventListener("focus", revalidateIfStale);
    };
  }, [revision]);

  const refresh = useCallback(() => {
    setRevision((value) => value + 1);
  }, []);

  return { state, refresh };
}
