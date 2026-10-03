/**
 * Defect #4 (QA 2026-09-30) — the "我" page's subpage tabs.
 *
 * URL-state pattern (patterns §4; the rankings ?period= and
 * notifications ?filter= precedent): the tab is a search param so a
 * 个人信息 deep link is shareable; the SERVER parses it (garbage
 * degrades to the default) and the page renders the matching island.
 */

export type ProfileTab = "growth" | "info";

export interface ProfileTabItem {
  key: ProfileTab;
  label: string;
}

/** Display order = landing order: 我的档案 stays the landing tab. */
export const PROFILE_TABS: readonly ProfileTabItem[] = [
  { key: "growth", label: "我的档案" },
  { key: "info", label: "个人信息" },
] as const;

export const DEFAULT_PROFILE_TAB: ProfileTab = "growth";

const TAB_KEYS: ReadonlySet<string> = new Set(PROFILE_TABS.map((tab) => tab.key));

/**
 * Parse the ?tab= param; repeated params keep the FIRST value (the
 * searchParams convention), unknown/empty values degrade to the
 * default tab — never an error page for a typo in the URL.
 */
export function parseProfileTab(raw: string | string[] | undefined): ProfileTab {
  const value = Array.isArray(raw) ? raw[0] : raw;
  return value !== undefined && TAB_KEYS.has(value) ? (value as ProfileTab) : DEFAULT_PROFILE_TAB;
}
