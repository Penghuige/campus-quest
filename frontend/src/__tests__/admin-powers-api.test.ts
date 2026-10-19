/**
 * Wave 2a admin power-pack wire pins (V1.1 spec §3/§4; the 2026-10-18
 * frozen contract with the backend session): the eight endpoints'
 * paths, methods, and body shapes, pinned BEFORE the backend lands
 * (contract-first, the auth-II discipline — the wrappers live in
 * `features/admin/adminPowersApi.ts` with handwritten types that flip
 * to generated aliases in the backend's PR).
 *
 * The frozen answers this file holds: batch rows carry STABLE error
 * codes in `reason` (same domain as BusinessError.code, ok=true has
 * none); overview's by_role/by_status/tasks.by_status answer FULL
 * fixed key sets (zero values included — no data-dependent key
 * drift); whitelist_size is GONE (the whitelist retired with #95,
 * spec §4.3 voided by 547099b); the invitation family rides the
 * /admin/staff/invitations prefix with the four-state lifecycle.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  batchAccountAction,
  changeAdminUserRole,
  fetchAdminOverview,
  fetchAdminUserDetail,
  issueAdminPasswordReset,
  listAdminInvitations,
  resendAdminInvitation,
  revokeAdminInvitation,
  revokeAdminUserSessions,
} from "../features/admin/adminPowersApi";

type Recorded = {
  url: string;
  method: string;
  body: string | null;
  credentials: RequestCredentials | undefined;
};

const originalFetch = globalThis.fetch;
let recorded: Recorded | undefined;
let responseFor: () => Response;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      body: typeof init?.body === "string" ? init.body : null,
      credentials: init?.credentials,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("the overview aggregate (spec §4.1)", () => {
  test("GETs /admin/overview; the fixed key sets decode verbatim", async () => {
    stubFetch(
      JSON.stringify({
        users: {
          total: 207,
          by_role: { STUDENT: 200, TEACHER: 5, ADMIN: 2 },
          by_status: { ACTIVE: 205, PENDING_APPROVAL: 1, SUSPENDED: 1, BANNED: 0 },
        },
        tasks: {
          by_status: { DRAFT: 1, PUBLISHED: 8, PAUSED: 0, CLOSED: 2, ARCHIVED: 0 },
        },
        pending_submissions: 3,
        pending_redemptions: 1,
        today: { new_users: 4, active_users: 32 },
      }),
    );
    const overview = await fetchAdminOverview();
    assert.equal(overview.users.total, 207);
    assert.equal(overview.tasks.by_status.ARCHIVED, 0);
    assert.equal(recorded?.url, "/api/v1/admin/overview");
    assert.equal(recorded?.method, "GET");
    assert.equal(recorded?.credentials, "include");
  });
});

describe("the user detail panorama (spec §3.1)", () => {
  test("GETs the aggregate with cookie credentials", async () => {
    stubFetch(
      JSON.stringify({
        user: { id: "u1", username: "20240001", nickname: "A", role: "STUDENT", status: "ACTIVE" },
        wallet_balance: 120,
        recent_submissions: [
          { id: "s1", task_title: "采集任务", status: "UNDER_REVIEW", submitted_at: "2026-10-18T08:00:00Z" },
        ],
        active_session_count: 2,
        remembered_device_count: 1,
      }),
    );
    const detail = await fetchAdminUserDetail("u1");
    assert.equal(detail.user.id, "u1");
    assert.equal(detail.recent_submissions[0].task_title, "采集任务");
    assert.equal(recorded?.url, "/api/v1/admin/users/u1");
    assert.equal(recorded?.method, "GET");
  });
});

describe("the admin password reset (spec §3.2)", () => {
  test("POSTs and answers the one-time TOKEN (the URL composes client-side)", async () => {
    stubFetch(JSON.stringify({ reset_token: "urlsafe32-abc" }), 200);
    const { reset_token } = await issueAdminPasswordReset("u1");
    assert.equal(reset_token, "urlsafe32-abc");
    assert.equal(recorded?.url, "/api/v1/admin/users/u1/password-reset");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.body, null);
  });
});

describe("session management (spec §3.3)", () => {
  test("the kick DELETE answers BOTH revoked counts (the GET discloses counts only — no listing)", async () => {
    stubFetch(JSON.stringify({ revoked_sessions: 2, revoked_devices: 1 }), 200);
    const counts = await revokeAdminUserSessions("u1");
    assert.deepEqual(counts, { revoked_sessions: 2, revoked_devices: 1 });
    assert.equal(recorded?.method, "DELETE");
    assert.equal(recorded?.url, "/api/v1/admin/users/u1/sessions");
  });
});

describe("role change (spec §3.4)", () => {
  test("PATCHes {role} to the dedicated endpoint", async () => {
    stubFetch(JSON.stringify({ id: "u1", username: "t@school.edu", nickname: "T", role: "ADMIN", status: "ACTIVE" }));
    const user = await changeAdminUserRole("u1", "ADMIN");
    assert.equal(user.role, "ADMIN");
    assert.equal(recorded?.url, "/api/v1/admin/users/u1/role");
    assert.equal(recorded?.method, "PATCH");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), { role: "ADMIN" });
  });
});

describe("batch account actions (spec §3.5)", () => {
  test("POSTs {action, user_ids}; rows carry stable codes in reason", async () => {
    stubFetch(
      JSON.stringify({
        results: [
          { user_id: "u1", ok: true },
          { user_id: "u2", ok: false, reason: "ACCOUNT_TRANSITION_INVALID" },
        ],
      }),
    );
    const { results } = await batchAccountAction("suspend", ["u1", "u2"]);
    assert.equal(results[1].reason, "ACCOUNT_TRANSITION_INVALID");
    assert.equal(recorded?.url, "/api/v1/admin/users/batch");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      action: "suspend",
      user_ids: ["u1", "u2"],
    });
  });
});

describe("the invitation lifecycle (spec §4.2)", () => {
  test("lists the four-state rows under the admin prefix", async () => {
    stubFetch(
      JSON.stringify({
        items: [
          {
            id: "inv1",
            email: "t@school.edu",
            role: "TEACHER",
            status: "PENDING",
            created_by: "admin@school.edu",
            created_at: "2026-10-18T08:00:00Z",
            expires_at: "2026-10-20T08:00:00Z",
          },
        ],
      }),
    );
    const { items } = await listAdminInvitations();
    assert.equal(items[0].status, "PENDING");
    assert.equal(recorded?.url, "/api/v1/admin/staff/invitations");
    assert.equal(recorded?.method, "GET");
  });

  test("revoking answers 204; resending answers the one-time {link}", async () => {
    stubFetch("", 204);
    await revokeAdminInvitation("inv1");
    assert.equal(recorded?.method, "DELETE");
    assert.equal(recorded?.url, "/api/v1/admin/staff/invitations/inv1");

    // The wire carries the fresh one-time TOKEN (the create contract's
    // own shape); the URL composes through buildInvitationLink.
    stubFetch(
      JSON.stringify({
        id: "inv1",
        role: "TEACHER",
        token: "fresh-urlsafe-token",
        expires_at: "2026-10-20T12:00:00Z",
        created_at: "2026-10-19T12:00:00Z",
      }),
    );
    const { token } = await resendAdminInvitation("inv1");
    assert.equal(token, "fresh-urlsafe-token");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.url, "/api/v1/admin/staff/invitations/inv1/resend");
  });
});
