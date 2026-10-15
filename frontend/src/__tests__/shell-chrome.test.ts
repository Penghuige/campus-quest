/**
 * Backlog UX (owner QA 2026-10-03, promoted 2026-10-10): the refresh
 * shell jump — the chrome decision table.
 *
 * Design system §8: ALL authenticated workspaces share ONE navigation
 * geometry across the three viewport bands. This table pins when the
 * student shell may render that workspace geometry BEFORE the session
 * resolves (optimistically, holding user-data slots as placeholders)
 * and when it must keep the minimal topbar shell:
 *
 * - session evidence present (the document request carried the
 *   readable csrf cookie — the only session artifact a page request
 *   ever sees; the refresh cookie is HttpOnly AND path-scoped to the
 *   auth endpoints, so it never rides a document request) → the
 *   loading and error frames render the workspace geometry, and a
 *   refresh never jumps between shells;
 * - no session evidence (first visit, logged out) → minimal chrome,
 *   so the 401→anonymous exit stays minimal→card and never
 *   introduces a sidebar→card jump direction;
 * - the terminal states: authenticated keeps the workspace;
 *   anonymous is minimal chrome whoever asks.
 *
 * The role refinement (a TEACHER/ADMIN session gets the guidance
 * page, still minimal chrome) happens in StudentShell AFTER
 * useSession resolves — this table only owns the chrome verdict.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  shellChromeFor,
  shellHintFromCookieValue,
  SHELL_HINT_COOKIE_NAME,
} from "../features/auth/shellChrome";

test("session evidence gates the optimistic workspace chrome", () => {
  // A returning session: loading AND error hold the workspace geometry.
  assert.equal(shellChromeFor("loading", true), "workspace");
  assert.equal(shellChromeFor("error", true), "workspace");
  // No session evidence: minimal chrome through the whole resolve.
  assert.equal(shellChromeFor("loading", false), "minimal");
  assert.equal(shellChromeFor("error", false), "minimal");
});

test("terminal states ignore the cookie probe", () => {
  // /me answered: the verdict follows the ANSWER, never the probe.
  assert.equal(shellChromeFor("authenticated", true), "workspace");
  assert.equal(shellChromeFor("authenticated", false), "workspace");
  assert.equal(shellChromeFor("anonymous", true), "minimal");
  assert.equal(shellChromeFor("anonymous", false), "minimal");
});

// The staff wrong-shell flash (owner report 2026-10-15, fix plan A):
// csrf evidence is ROLE-BLIND — a staff session refreshing a student
// route SSR'd the STUDENT optimistic shell and repainted to the
// guidance card once /me answered. The session mint now also sets a
// role-HINT cookie (cq_shell, coarse student|staff — deliberately NOT
// ADMIN/TEACHER granular); a staff hint routes the pre-resolve frames
// to MINIMAL chrome, which IS the staff terminal geometry here (the
// guidance card renders under minimal chrome) — no wrong-role flash,
// no jump for students.
test("a STAFF shell hint routes the pre-resolve frames to minimal chrome", () => {
  assert.equal(shellChromeFor("loading", true, "staff"), "minimal");
  assert.equal(shellChromeFor("error", true, "staff"), "minimal");
});

test("a STUDENT hint (and the pre-cookie legacy default) keeps today's behavior", () => {
  assert.equal(shellChromeFor("loading", true, "student"), "workspace");
  // The compat default: sessions minted before the hint cookie existed
  // (or a dropped cookie) must not regress the student refresh fix.
  assert.equal(shellChromeFor("loading", true, undefined), "workspace");
  assert.equal(shellChromeFor("error", true, undefined), "workspace");
});

test("the hint never overrides evidence or the terminal verdicts", () => {
  // No csrf evidence: minimal regardless of any hint value.
  assert.equal(shellChromeFor("loading", false, "staff"), "minimal");
  // Terminal states follow /me, never the hint.
  assert.equal(shellChromeFor("authenticated", true, "staff"), "workspace");
  assert.equal(shellChromeFor("anonymous", true, "staff"), "minimal");
});

test("the hint cookie contract: coarse two-value domain, HttpOnly-class shape", () => {
  // The frozen contract with the backend mint (reviewer ruling
  // 2026-10-15): name cq_shell, values student|staff ONLY — coarse by
  // design; unknown values read as absent (the legacy default).
  assert.equal(SHELL_HINT_COOKIE_NAME, "cq_shell");
  assert.equal(shellHintFromCookieValue("staff"), "staff");
  assert.equal(shellHintFromCookieValue("student"), "student");
  assert.equal(shellHintFromCookieValue("ADMIN"), undefined);
  assert.equal(shellHintFromCookieValue(""), undefined);
  assert.equal(shellHintFromCookieValue(undefined), undefined);
});
