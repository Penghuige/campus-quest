/**
 * Defect #3 (QA 2026-09-30) — sidebar collapse/resize preference.
 *
 * The rail's expanded width is a design token (`--rail-width`, rem) with
 * the drag operating in screen pixels; the constants here are the TS
 * mirror of that contract (pinned by `__tests__/sidebar-preference.test.ts`
 * against the globals.css text, the pwa-manifest pin pattern). The
 * preference itself is NON-SECRET UI state persisted in localStorage
 * under one stable key, read through the same SSR/private-mode guards
 * as the auth handoff marker: a missing, corrupt, or hostile storage
 * degrades to the CSS defaults — never crashes the mount.
 */

/** One stored preference: the collapsed flag plus the last EXPANDED width. */
export interface SidebarPreference {
  collapsed: boolean;
  widthPx: number;
}

/** 14.5rem — mirrors the `--rail-width` token default (16px root). */
export const RAIL_WIDTH_DEFAULT_PX = 232;
/** 13rem — narrowest expanded rail that still reads 4-char zh labels. */
export const RAIL_WIDTH_MIN_PX = 208;
/** 25rem — widest drag target before the content column suffers. */
export const RAIL_WIDTH_MAX_PX = 400;
/** 4.5rem — mirrors `--rail-width-collapsed` (icon rail). */
export const RAIL_WIDTH_COLLAPSED_PX = 72;

/** Stable, non-secret; e2e and any future migration rely on the name. */
export const SIDEBAR_PREFERENCE_KEY = "cq:sidebar-preference";

/** Clamp a drag/keyboard width into the allowed pixel range. */
export function clampRailWidth(px: number): number {
  if (Number.isNaN(px)) {
    // NaN carries no direction — fall back rather than guess a bound.
    return RAIL_WIDTH_DEFAULT_PX;
  }
  return Math.min(Math.max(Math.round(px), RAIL_WIDTH_MIN_PX), RAIL_WIDTH_MAX_PX);
}

/**
 * Parse a stored payload. Structurally invalid input returns null (the
 * caller keeps the CSS defaults); a valid shape with an out-of-range
 * width is NORMALIZED by clamping so legacy or hand-edited values
 * cannot wedge the rail outside the drag range.
 */
export function parseSidebarPreference(raw: string | null): SidebarPreference | null {
  if (raw === null || raw.length === 0) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const { collapsed, widthPx } = parsed as { collapsed?: unknown; widthPx?: unknown };
  if (typeof collapsed !== "boolean" || typeof widthPx !== "number" || !Number.isFinite(widthPx)) {
    return null;
  }
  return { collapsed, widthPx: clampRailWidth(widthPx) };
}

export function serializeSidebarPreference(pref: SidebarPreference): string {
  return JSON.stringify({ collapsed: pref.collapsed, widthPx: clampRailWidth(pref.widthPx) });
}

/** The read surface `readSidebarPreference` needs — `localStorage` shaped. */
export interface PreferenceReader {
  getItem(key: string): string | null;
}

/** The write surface `writeSidebarPreference` needs. */
export interface PreferenceWriter {
  setItem(key: string, value: string): void;
}

/** The full localStorage shape. */
export interface PreferenceStorage extends PreferenceReader, PreferenceWriter {}

/**
 * Read the preference; `storage === undefined` is the SSR case. Every
 * storage call is guarded — a SecurityError (sandboxed iframe) or a
 * corrupt payload reads as "no preference".
 */
export function readSidebarPreference(storage?: PreferenceReader): SidebarPreference | null {
  if (storage === undefined) {
    return null;
  }
  try {
    return parseSidebarPreference(storage.getItem(SIDEBAR_PREFERENCE_KEY));
  } catch {
    return null;
  }
}

/** Persist the preference; quota/private-mode failures are swallowed. */
export function writeSidebarPreference(storage: PreferenceWriter, pref: SidebarPreference): void {
  try {
    storage.setItem(SIDEBAR_PREFERENCE_KEY, serializeSidebarPreference(pref));
  } catch {
    // No persistence is a degraded-but-usable session, not an error.
  }
}

/**
 * Reach the localStorage OBJECT safely. The getter itself throws on
 * opaque origins (sandboxed iframe without allow-same-origin, or a
 * browser policy denying storage) — before any helper's internal
 * try/catch can run — so callers must go through THIS access guard,
 * never touch `window.localStorage` directly (Codex P2, PR #19).
 */
export function safeLocalStorage(): PreferenceStorage | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
