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

/** The chrome for a session state, given whether the document request
 * carried session evidence. The role refinement (a TEACHER/ADMIN
 * session gets the guidance page, still minimal chrome) happens in
 * StudentShell AFTER useSession resolves — this table only owns the
 * chrome verdict. */
export function shellChromeFor(
  status: SessionState["status"],
  sessionCookiePresent: boolean,
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
      return sessionCookiePresent ? "workspace" : "minimal";
  }
}
