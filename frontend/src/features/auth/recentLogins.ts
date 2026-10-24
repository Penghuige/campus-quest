/**
 * The recent-logins ledger (batch 2, owner brief 2026-10-20): the
 * login page's account-quick-switch cards. Pure client state —
 *
 * ZERO credentials: the jar holds {identifier (for the prefill — the
 * browser-remembered-username convention), masked display string,
 * role, nickname, lastUsedAt}. Tokens live only in cookies; nothing
 * here ever authenticates anything.
 *
 * G11 on the DOM side: the CARD renders the masked form; the full
 * identifier only ever returns to the input the user is about to use
 * (the click prefill — the same field they typed it into).
 *
 * Dirty-data tolerance: a corrupted jar (or none at all — private
 * mode, SSR, node) never throws; malformed rows filter out silently.
 */
const STORAGE_KEY = "cq.recent-logins";
const MAX_ENTRIES = 5;

export type RecentLoginRole = "STUDENT" | "TEACHER" | "ADMIN";

export interface RecentLoginEntry {
  /** The prefill source — plaintext, never rendered on a card. */
  identifier: string;
  /** The G11 display form the card shows. */
  masked: string;
  role: RecentLoginRole;
  nickname: string;
  /** Epoch ms — the recency sort key. */
  lastUsedAt: number;
  /** The account's remembered-slot handle (the slot-matching
   * contract, 2026-10-24): written when a login MINTS a credential,
   * RETAINED when a later login mints nothing (the older slot is
   * still live until revoke_all). Absent on legacy cards — they
   * degrade to the code flow. */
  device_slot?: string;
}

/** The G11 mask: a CN mobile keeps its last four; an email its first
 * local char + domain; a student number its bookends. E.164 forms
 * (the probe's own shape) mask like the bare mobile — an unmasked
 * passthrough would leak the whole number AND never match the probe. */
export function maskIdentifier(identifier: string): string {
  const trimmed = identifier.trim();
  if (/^1[3-9]\d{9}$/.test(trimmed)) {
    return `+86****${trimmed.slice(-4)}`;
  }
  const e164 = /^\+?(86)?[\s-]*(1[3-9]\d{9})$/.exec(trimmed);
  if (e164 !== null) {
    return `+86****${e164[2].slice(-4)}`;
  }
  const at = trimmed.indexOf("@");
  if (at > 0) {
    return `${trimmed[0]}***${trimmed.slice(at)}`;
  }
  if (/^\d{6,20}$/.test(trimmed)) {
    return `${trimmed.slice(0, 2)}****${trimmed.slice(-2)}`;
  }
  if (/^\d+$/.test(trimmed)) {
    // The backend's short-value rule (mask_student_number): a value
    // shorter than the student-number minimum is masked FULLY — four
    // kept digits would give a five-digit value away.
    return "****";
  }
  return trimmed;
}

function storage(): Storage | null {
  try {
    return (globalThis as { localStorage?: Storage }).localStorage ?? null;
  } catch {
    return null;
  }
}

function isValidEntry(value: unknown): value is RecentLoginEntry {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const entry = value as Record<string, unknown>;
  return (
    typeof entry.identifier === "string" &&
    entry.identifier.length > 0 &&
    typeof entry.masked === "string" &&
    (entry.role === "STUDENT" || entry.role === "TEACHER" || entry.role === "ADMIN") &&
    typeof entry.nickname === "string" &&
    typeof entry.lastUsedAt === "number" &&
    // The slot rides only as a string when present (mint-only).
    (entry.device_slot === undefined || typeof entry.device_slot === "string")
  );
}

/** Upsert by identifier (bump to the head), cap at five, newest-first.
 * The mint-only retention rule: an entry arriving WITHOUT a slot keeps
 * the previous row's — an unminted re-login (the checkbox unticked)
 * never breaks the older, still-live credential. */
export function recordRecentLogin(entry: RecentLoginEntry): void {
  const jar = storage();
  if (jar === null) {
    return;
  }
  let rows: RecentLoginEntry[];
  try {
    rows = listRecentLogins();
  } catch {
    return;
  }
  const previous = rows.find((row) => row.identifier === entry.identifier);
  const landed =
    entry.device_slot === undefined && previous?.device_slot !== undefined
      ? { ...entry, device_slot: previous.device_slot }
      : entry;
  rows = [landed, ...rows.filter((row) => row.identifier !== entry.identifier)].slice(
    0,
    MAX_ENTRIES,
  );
  try {
    jar.setItem(STORAGE_KEY, JSON.stringify(rows));
  } catch {
    // A full/blocked jar is a convenience loss, never an error.
  }
}

/** The listing — valid rows only, newest first, already capped. */
export function listRecentLogins(): RecentLoginEntry[] {
  const jar = storage();
  if (jar === null) {
    return [];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(jar.getItem(STORAGE_KEY) ?? "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) {
    return [];
  }
  return parsed
    .filter(isValidEntry)
    .sort((a, b) => b.lastUsedAt - a.lastUsedAt)
    .slice(0, MAX_ENTRIES);
}

/** The card's 从本机移除 verb — a pure jar delete, no backend call. */
export function removeRecentLogin(identifier: string): void {
  const jar = storage();
  if (jar === null) {
    return;
  }
  try {
    const rows = listRecentLogins().filter((row) => row.identifier !== identifier);
    jar.setItem(STORAGE_KEY, JSON.stringify(rows));
  } catch {
    // Same tolerance as the record path.
  }
}
