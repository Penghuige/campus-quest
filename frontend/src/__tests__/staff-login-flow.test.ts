/**
 * Staff post-login landing (owner report 2026-10-15; the PR #84 ratchet
 * restoration) — the orchestration seam the staff login form rides:
 * the landing target comes from the POST-LOGIN /me answer, and each
 * staff role lands on its OWN cockpit. Pinned with injected
 * login/read/router spies (the repo's harness has no component
 * renderer; the e2e landing verdict covers the wired form end-to-end).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { landAfterStaffLogin } from "../features/auth/staffLoginFlow";

function harness(role: string) {
  const calls: string[] = [];
  return {
    calls,
    deps: {
      login: async () => {
        calls.push("login");
      },
      refreshSession: () => {
        calls.push("refreshSession");
      },
      readMe: async () => {
        calls.push("readMe");
        return { role };
      },
      router: {
        replace: (path: string) => {
          calls.push(`replace:${path}`);
        },
        refresh: () => {
          calls.push("routerRefresh");
        },
      },
    },
  };
}

describe("landAfterStaffLogin (the owner report's landing rule)", () => {
  test("an ADMIN session lands on the admin workspace", async () => {
    const h = harness("ADMIN");
    await landAfterStaffLogin(h.deps);
    assert.ok(h.calls.includes("replace:/admin/users"));
    assert.ok(!h.calls.includes("replace:/teacher/reviews"));
  });

  test("a TEACHER session lands on the review queue", async () => {
    const h = harness("TEACHER");
    await landAfterStaffLogin(h.deps);
    assert.ok(h.calls.includes("replace:/teacher/reviews"));
  });

  test("a STUDENT answer (defensive) lands on the student home", async () => {
    const h = harness("STUDENT");
    await landAfterStaffLogin(h.deps);
    assert.ok(h.calls.includes("replace:/"));
  });

  test("ordering: refresh the session cache BEFORE reading /me; router refresh last", async () => {
    const h = harness("ADMIN");
    await landAfterStaffLogin(h.deps);
    assert.deepEqual(h.calls, [
      "login",
      "refreshSession",
      "readMe",
      "replace:/admin/users",
      "routerRefresh",
    ]);
  });

  test("a failed login never routes (the error path is the form's catch)", async () => {
    const replaced: string[] = [];
    await assert.rejects(
      landAfterStaffLogin({
        login: async () => {
          throw new Error("401");
        },
        refreshSession: () => {},
        readMe: async () => {
          throw new Error("must not be reached");
        },
        router: {
          replace: (path) => replaced.push(path),
          refresh: () => {},
        },
      }),
    );
    assert.deepEqual(replaced, []);
  });
});
