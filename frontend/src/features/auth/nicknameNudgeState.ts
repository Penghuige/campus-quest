/**
 * First-login rename nudge (V1.1 spec §6; plan Task 10) — the pure
 * decision core shared by every shell's NicknameNudge island.
 *
 * - the trigger recognises exactly the AUTO-GENERATED nickname shapes:
 *   all digits (student numbers, digit email local parts) or equal to
 *   the username's local part (staff emails). A renamed account flips
 *   the predicate false, so "never re-shown after rename" needs no
 *   extra bookkeeping;
 * - dismissal persists per-account under `cq-nudge-rename:{user_id}`
 *   in localStorage. Storage reaches this module as an injected
 *   StorageLike (tests inject a Map; the component goes through the
 *   safeLocalStorage access guard — a denied storage just reads as
 *   "not dismissed", never an error);
 * - non-secret UI state only, like the sidebar preference: a key names
 *   the account but carries nothing sensitive.
 */

/** The subset of Storage the nudge touches (get/set only). */
export interface NudgeStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const DISMISSED_VALUE = "1";

/** True when the nickname still looks machine-generated (V1.1 §6). */
export function isAutoNickname(nickname: string, username: string): boolean {
  if (nickname.length === 0) {
    // Broken data, not a known auto shape — no nudge.
    return false;
  }
  if (/^\d+$/.test(nickname)) {
    return true;
  }
  const localPart = username.split("@", 1)[0];
  return nickname === localPart;
}

/** The per-account dismissal key. */
export function nudgeStorageKey(userId: string): string {
  return `cq-nudge-rename:${userId}`;
}

/** Whether THIS account already dismissed the nudge. */
export function readNudgeDismissed(storage: NudgeStorage, userId: string): boolean {
  return storage.getItem(nudgeStorageKey(userId)) === DISMISSED_VALUE;
}

/** Persist the dismissal for this account. */
export function writeNudgeDismissed(storage: NudgeStorage, userId: string): void {
  try {
    storage.setItem(nudgeStorageKey(userId), DISMISSED_VALUE);
  } catch {
    // No persistence is a degraded-but-usable session, not an error.
  }
}
