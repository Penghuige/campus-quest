/**
 * The student shell's chrome verdict (backlog UX: the refresh shell
 * jump, owner QA 2026-10-03).
 *
 * Design system §8: ALL authenticated workspaces share ONE navigation
 * geometry across the three viewport bands. The refresh jump existed
 * because the pre-resolve frames (loading, error) rendered a minimal
 * topbar shell instead, so every reload repainted a different chrome
 * for the ~0.6s session-resolve window.
 *
 * The verdict: when the document request carried session EVIDENCE —
 * the readable `csrf_token` cookie, the only session artifact a page
 * request ever sees (the refresh cookie is HttpOnly AND path-scoped
 * to the auth endpoints, so it never rides a document request) — the
 * pre-resolve frames render the workspace geometry optimistically,
 * with the user-data slots (rail account, topbar actions) as
 * placeholders. Without evidence the minimal topbar shell stays, so
 * the 401→anonymous exit keeps the minimal→card transition it has
 * always had and never introduces a sidebar→card jump direction.
 *
 * The cookie is evidence, not truth: a revoked-but-unexpired session
 * still renders the optimistic frame and then corrects to the auth
 * card once /me answers 401 — an honest correction on a rare path.
 */
import type { SessionState } from "./session";

export type ShellChrome = "workspace" | "minimal";

/**
 * The role-hint cookie's name (the staff wrong-shell flash fix, plan
 * A; owner report 2026-10-15). The session mint writes it alongside
 * the csrf cookie — HttpOnly + SameSite=Lax is the sanctioned shape
 * (the consumer is the (student) layout's SERVER-side cookie probe,
 * not client JS). The value domain is COARSE and frozen:
 * "student" | "staff" — deliberately NOT ADMIN/TEACHER granular.
 *
 * Lifecycle (three write points, all server-side — keep them in sync
 * when touching the session mint):
 * 1. LOGIN and every refresh ROTATION (re)write it with the minted
 *    session's role family;
 * 2. LOGOUT clears it;
 * 3. ROLE CHANGE rewrites it (the admin role-change power lands with
 *    Wave 2a — that endpoint MUST join this contract).
 *
 * Absence (a session minted before this cookie existed, or a dropped
 * cookie) reads as "no hint" and keeps the pre-fix behavior — the
 * self-healing legacy path.
 */
export const SHELL_HINT_COOKIE_NAME = "cq_shell";

/** The coarse hint values the contract admits. */
export type ShellHint = "student" | "staff";

/**
 * Parse a cookie value into the hint domain; anything else (including
 * absent) is "no hint" — an unknown value must never change chrome.
 */
export function shellHintFromCookieValue(value: string | undefined): ShellHint | undefined {
  if (value === "staff" || value === "student") {
    return value;
  }
  return undefined;
}

/** The chrome for a session state, given whether the document request
 * carried session evidence, and the role hint when it did. The role
 * refinement (a TEACHER/ADMIN session gets the guidance page, still
 * minimal chrome) happens in StudentShell AFTER useSession resolves —
 * this table only owns the chrome verdict. */
export function shellChromeFor(
  status: SessionState["status"],
  sessionCookiePresent: boolean,
  shellHint: ShellHint | undefined = undefined,
): ShellChrome {
  switch (status) {
    case "authenticated":
      // /me answered: the verdict follows the ANSWER, never the probe.
      return "workspace";
    case "anonymous":
      return "minimal";
    // The pre-resolve frames: optimistic only with session evidence.
    case "loading":
    case "error":
      if (!sessionCookiePresent) {
        return "minimal";
      }
      // A STAFF hint routes the pre-resolve frames to the minimal
      // shell — which IS the staff terminal geometry on student
      // routes (the guidance card renders under minimal chrome), so a
      // staff refresh never paints the student workspace first.
      return shellHint === "staff" ? "minimal" : "workspace";
  }
}
