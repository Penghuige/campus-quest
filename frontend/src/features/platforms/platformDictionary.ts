"use client";
/**
 * The platform dictionary (platform-registry spec §3): the
 * enabled-only, read-after-login code → display_name table every
 * claim/assignment surface consumes.
 *
 * One cache key, two access shapes:
 * - `usePlatformDictionary` — the raw loading/ready/error section
 *   (the admin page's own fetch);
 * - `usePlatformNames` — the synchronous name MAP for already-ready
 *   islands (claim panels, review rows). It rides the SAME cached
 *   section: a remount serves the cached snapshot synchronously (no
 *   flash, no codes-ever), the FIRST load shows the honest fallback
 *   (codes render as themselves) for the dictionary's own loading
 *   window, and the landed dictionary re-renders the rows with names —
 *   the claim data it decorates is never blocked or refetched.
 */
import { useMemo } from "react";

import { useSection } from "@/components/ui/useSection";

import { listPlatforms } from "./platformApi";
import { platformDisplayMap } from "./platformView";
import type { PlatformDictionary } from "./platformView";

const CACHE_KEY = "platforms:dictionary";
const EMPTY: PlatformDictionary = [];

/** Stable loader identity (the cache dedupes requests onto the key). */
function loadDictionary(): Promise<PlatformDictionary> {
  return listPlatforms();
}

/** The dictionary as a loading/ready/error section. */
export function usePlatformDictionary() {
  return useSection(loadDictionary, CACHE_KEY);
}

/** The synchronous code → display_name map (empty = codes render raw). */
export function usePlatformNames(): ReadonlyMap<string, string> {
  const { state } = useSection(loadDictionary, CACHE_KEY);
  const dictionary = state.status === "ready" ? state.data : EMPTY;
  return useMemo(() => platformDisplayMap(dictionary), [dictionary]);
}
