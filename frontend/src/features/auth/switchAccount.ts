/**
 * The switch-account handoff (popover batch, owner feedback
 * 2026-10-23): the shells' 切换账号 menu passes its pick to the login
 * page through sessionStorage — a URL query would put the identifier
 * into the browser history, the same reason the login prefill rides
 * client state and never the URL.
 *
 * The menu's rows exclude the CURRENT account without any extra
 * storage: the jar's HEAD is the current login by construction — every
 * success path funnels through the panel's recordLogin, which upserts
 * the just-used identifier to the head. The one login outside the
 * funnel (invite acceptance) records nothing, so at worst the menu
 * hides one stale option; it can never offer a broken one.
 */
import type { RecentLoginEntry } from "./recentLogins";

/** The login page reads and deletes this key on mount. */
export const SWITCH_PICK_KEY = "cq.switch-pick";

/** The menu's rows: the ledger minus its head, capped at four. Pure. */
export function switchMenuEntries(
  recent: readonly RecentLoginEntry[],
): RecentLoginEntry[] {
  return recent.slice(1, 5);
}

function sessionStore(): Storage | null {
  try {
    return (globalThis as { sessionStorage?: Storage }).sessionStorage ?? null;
  } catch {
    return null;
  }
}

/** Store the picked identifier right before the logout hop. */
export function setSwitchPick(identifier: string): void {
  const jar = sessionStore();
  if (jar === null) {
    return;
  }
  try {
    jar.setItem(SWITCH_PICK_KEY, identifier);
  } catch {
    // A blocked jar costs the auto-pick, never the switch itself.
  }
}

/** Read-and-delete: the pick is consumed exactly once, by the mount
 * that finds it. A missing key (or a blocked store) answers null. */
export function takeSwitchPick(): string | null {
  const jar = sessionStore();
  if (jar === null) {
    return null;
  }
  try {
    const value = jar.getItem(SWITCH_PICK_KEY);
    if (value !== null) {
      jar.removeItem(SWITCH_PICK_KEY);
    }
    return value;
  } catch {
    return null;
  }
}
