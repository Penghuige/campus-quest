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

import { shellChromeFor } from "../features/auth/shellChrome";

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
