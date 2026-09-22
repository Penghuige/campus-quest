/**
 * Task 10 (Plan 09): admin workspace API wrappers against a stubbed
 * fetch transport — wire-shape pins for every Plan 08 admin endpoint the
 * six pages call, transcribed from the generated snapshot
 * (`lib/api/schema`) and the backend routers
 * (`identity/admin_router.py`, `identity/whitelist_admin.py`,
 * `system/router.py`, `audit/router.py`, `notifications/router.py`,
 * `points/admin_router.py`, the points review-queue routes).
 *
 * Pins:
 * - paths + methods + exact body field names for every wrapper;
 * - the query-parameter spellings (role/status filters, the audit
 *   equality filters, revoke's reason-as-query on DELETE);
 * - the settings PUT typed bodies ({value} per key, plus the OPTIONAL
 *   trimmed reason — T10's audit gap-fill — omitted when absent/blank);
 * - 204 grant/revoke resolving to undefined through the shared client;
 * - cookie credentials on every call (the shared apiRequest contract).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  approveRedemption,
  banAdminUser,
  confirmWhitelistImport,
  createNotificationTemplate,
  createRewardItem,
  disableNotificationTemplate,
  disableRewardItem,
  enableNotificationTemplate,
  forceFailDelivery,
  fulfillRedemption,
  getCurrentAcademicTerm,
  grantRewardReview,
  listAdminUsers,
  listAuditLogs,
  listNotificationFailures,
  listRedemptionQueue,
  listRewardCatalogue,
  listSystemSettings,
  listWhitelist,
  previewWhitelistImport,
  putAbandonDailyLimit,
  putCurrentAcademicTerm,
  putEmojiWhitelist,
  putManagementNetworkCidrs,
  putManagementNetworkEnabled,
  reactivateAdminUser,
  rejectRedemption,
  releaseOccupiedAssignment,
  revealCommentIdentity,
  revokeRewardReview,
  suspendAdminUser,
  toggleWhitelistEntry,
  updateNotificationTemplate,
  updateRewardItem,
} from "../features/admin/adminApi";

type RecordedRequest = {
  url: string;
  method: string;
  headers: Headers;
  credentials: RequestCredentials | undefined;
  body: unknown;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  // 204/205 are null-body statuses: undici rejects a Response constructed
  // with an explicit (even empty) body for them.
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      headers: new Headers(init?.headers),
      credentials: init?.credentials,
      body: init?.body,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function emptyPage(): string {
  return JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 });
}

describe("account directory (spec §5.7)", () => {
  test("list carries role/status filters on GET /admin/users", async () => {
    stubFetch(emptyPage());
    await listAdminUsers({ limit: 10, offset: 20, role: "TEACHER", status: "ACTIVE" });
    assert.equal(
      recorded?.url,
      "/api/v1/admin/users?limit=10&offset=20&role=TEACHER&status=ACTIVE",
    );
    assert.equal(recorded?.method, "GET");
    assert.equal(recorded?.credentials, "include");
  });

  test("suspend/ban/reactivate each POST the mandatory reason", async () => {
    for (const [call, path, expectedStatus] of [
      [() => suspendAdminUser("u1", "违规核查"), "/suspend", "SUSPENDED"],
      [() => banAdminUser("u1", "多次违规"), "/ban", "BANNED"],
      [() => reactivateAdminUser("u1", "申诉通过"), "/reactivate", "ACTIVE"],
    ] as const) {
      stubFetch(
        JSON.stringify({
          id: "u1",
          username: "20260001",
          role: "STUDENT",
          status: expectedStatus,
        }),
      );
      await call();
      assert.equal(recorded?.url, `/api/v1/admin/users/u1${path}`);
      assert.equal(recorded?.method, "POST");
      const body = JSON.parse(String(recorded?.body)) as { reason: string };
      assert.equal(typeof body.reason, "string");
      assert.notEqual(body.reason.trim(), "");
    }
  });
});

describe("whitelist (spec §5.1)", () => {
  test("listing paginates over GET /admin/whitelist", async () => {
    stubFetch(emptyPage());
    await listWhitelist({ limit: 10, offset: 5 });
    assert.equal(recorded?.url, "/api/v1/admin/whitelist?limit=10&offset=5");
  });

  test("preview POSTs the raw text as {content}", async () => {
    stubFetch(
      JSON.stringify({
        total_rows: 1,
        decisions: [],
        counts: {
          total_rows: 1,
          importable: 1,
          duplicate_in_file: 0,
          duplicate_in_db: 0,
          full_width_digits: 0,
          invalid_characters: 0,
          invalid_length: 0,
        },
        importable: ["20260001"],
        confirm_token: "digest",
      }),
    );
    await previewWhitelistImport("20260001\n");
    assert.equal(recorded?.url, "/api/v1/admin/whitelist/preview");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(String(recorded?.body)), { content: "20260001\n" });
  });

  test("confirm POSTs the digest-bound set verbatim (all-or-nothing)", async () => {
    stubFetch(JSON.stringify({ created: 2, enable: true }));
    const result = await confirmWhitelistImport({
      confirm_token: "digest",
      enable: true,
      student_numbers: ["20260001", "20260002"],
    });
    assert.equal(result.created, 2);
    assert.equal(recorded?.url, "/api/v1/admin/whitelist/confirm");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      confirm_token: "digest",
      enable: true,
      student_numbers: ["20260001", "20260002"],
    });
  });

  test("toggle PATCHes {enabled, reason} on the student-number path", async () => {
    stubFetch(JSON.stringify({ student_number: "20260001", toggled: true }));
    await toggleWhitelistEntry("20260001", { enabled: false, reason: "毕业离校" });
    assert.equal(recorded?.url, "/api/v1/admin/whitelist/20260001");
    assert.equal(recorded?.method, "PATCH");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      enabled: false,
      reason: "毕业离校",
    });
  });
});

describe("reward catalogue + grants (spec §16)", () => {
  test("catalogue listing rides the STUDENT GET /rewards (enabled-only read surface)", async () => {
    stubFetch(JSON.stringify({ items: [] }));
    await listRewardCatalogue();
    assert.equal(recorded?.url, "/api/v1/rewards");
    assert.equal(recorded?.method, "GET");
  });

  test("create POSTs the full row with the audit reason", async () => {
    stubFetch(JSON.stringify({ id: "r1", name: "文创礼包", point_cost: 100 }));
    await createRewardItem({
      name: "文创礼包",
      point_cost: 100,
      description: null,
      stock: 10,
      per_user_term_limit: 1,
      available_from: null,
      available_until: null,
      requires_manual_review: false,
      fulfillment_instructions: null,
      reason: "新学期上架",
    });
    assert.equal(recorded?.url, "/api/v1/admin/rewards");
    assert.equal(recorded?.method, "POST");
    const body = JSON.parse(String(recorded?.body)) as Record<string, unknown>;
    assert.equal(body.name, "文创礼包");
    assert.equal(body.point_cost, 100);
    assert.equal(body.reason, "新学期上架");
  });

  test("update PATCHes a presence-based partial body verbatim", async () => {
    stubFetch(JSON.stringify({ id: "r1", point_cost: 120 }));
    await updateRewardItem("r1", { point_cost: 120, reason: "调价" });
    assert.equal(recorded?.url, "/api/v1/admin/rewards/r1");
    assert.equal(recorded?.method, "PATCH");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      point_cost: 120,
      reason: "调价",
    });
  });

  test("disable POSTs the mandatory reason", async () => {
    stubFetch(JSON.stringify({ id: "r1", enabled: false }));
    await disableRewardItem("r1", "库存清空");
    assert.equal(recorded?.url, "/api/v1/admin/rewards/r1/disable");
    assert.deepEqual(JSON.parse(String(recorded?.body)), { reason: "库存清空" });
  });

  test("grant POSTs {teacher_id, reason} and resolves 204 to undefined", async () => {
    stubFetch("", 204);
    const result = await grantRewardReview("t1", "负责兑换发放");
    assert.equal(result, undefined);
    assert.equal(recorded?.url, "/api/v1/admin/reward-review-grants");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      teacher_id: "t1",
      reason: "负责兑换发放",
    });
  });

  test("revoke DELETEs with the reason as a QUERY parameter (no body)", async () => {
    stubFetch("", 204);
    await revokeRewardReview("t1", "岗位调整");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/reward-review-grants/t1?reason=" + encodeURIComponent("岗位调整"),
    );
    assert.equal(recorded?.method, "DELETE");
    assert.equal(recorded?.body, undefined);
  });
});

describe("redemption review queue (spec §16.2)", () => {
  test("queue paginates over GET /teacher/rewards/redemptions", async () => {
    stubFetch(emptyPage());
    await listRedemptionQueue({ limit: 5, offset: 5 });
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/rewards/redemptions?limit=5&offset=5",
    );
  });

  test("approve is a bare POST; reject carries the reason; fulfill carries the note", async () => {
    const row = JSON.stringify({
      id: "d1",
      reward_item_id: "r1",
      status: "APPROVED",
      points: 100,
      term_key: "2026-2027-1",
      created_at: "2026-09-01T00:00:00Z",
      requester_nickname: null,
      item_name: "礼包",
    });
    stubFetch(row);
    await approveRedemption("d1");
    assert.equal(recorded?.url, "/api/v1/teacher/rewards/redemptions/d1/approve");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.body, undefined);

    stubFetch(row);
    await rejectRedemption("d1", "不符合发放条件");
    assert.equal(recorded?.url, "/api/v1/teacher/rewards/redemptions/d1/reject");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      reason: "不符合发放条件",
    });

    stubFetch(row);
    await fulfillRedemption("d1", "已线下发放");
    assert.equal(recorded?.url, "/api/v1/teacher/rewards/redemptions/d1/fulfill");
    assert.deepEqual(JSON.parse(String(recorded?.body)), { note: "已线下发放" });
  });
});

describe("audit search (spec §30)", () => {
  test("equality filters map to their wire spellings; empty filters are omitted", async () => {
    stubFetch(emptyPage());
    await listAuditLogs({ action: "USER_SUSPENDED", actorUserId: "u1", limit: 20 });
    assert.equal(
      recorded?.url,
      "/api/v1/admin/audit-logs?limit=20&action=USER_SUSPENDED&actor_user_id=u1",
    );

    stubFetch(emptyPage());
    await listAuditLogs({ targetType: "" });
    assert.equal(recorded?.url, "/api/v1/admin/audit-logs");
  });
});

describe("system settings (typed keys)", () => {
  test("list rides GET /admin/settings; the term GET resolves the effective value", async () => {
    stubFetch(JSON.stringify({ items: [] }));
    await listSystemSettings();
    assert.equal(recorded?.url, "/api/v1/admin/settings");

    stubFetch(JSON.stringify({ value: "2026-2027-1" }));
    const term = await getCurrentAcademicTerm();
    assert.equal(term.value, "2026-2027-1");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/settings/current-academic-term",
    );
  });

  test("each key PUTs its typed {value} body; the optional reason rides trimmed or is omitted", async () => {
    const valueResponse = JSON.stringify({ key: "K", value: "v", version: 1 });
    stubFetch(JSON.stringify({ value: "2026-2027-2" }));
    await putCurrentAcademicTerm("2026-2027-2", "新学期开始");
    assert.equal(recorded?.method, "PUT");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      value: "2026-2027-2",
      reason: "新学期开始",
    });

    stubFetch(JSON.stringify({ value: "2026-2027-3" }));
    await putCurrentAcademicTerm("2026-2027-3");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      value: "2026-2027-3",
    });

    stubFetch(valueResponse);
    await putEmojiWhitelist(["🎉", "👏"], "  收紧表情列表  ");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/settings/emoji-whitelist",
    );
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      value: ["🎉", "👏"],
      reason: "收紧表情列表", // trimmed client-side
    });

    stubFetch(valueResponse);
    await putAbandonDailyLimit(3, "   ");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/settings/abandon-daily-limit",
    );
    // A whitespace-only reason is OMITTED (None is legal; a blank string
    // would be the backend's typed 422).
    assert.deepEqual(JSON.parse(String(recorded?.body)), { value: 3 });

    stubFetch(valueResponse);
    await putManagementNetworkEnabled(true, "机房网络收敛");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/settings/management-network-enabled",
    );
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      value: true,
      reason: "机房网络收敛",
    });

    stubFetch(valueResponse);
    await putManagementNetworkCidrs(["10.0.0.0/8"]);
    assert.equal(
      recorded?.url,
      "/api/v1/admin/settings/management-network-cidrs",
    );
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      value: ["10.0.0.0/8"],
    });
  });
});

describe("notification failures + templates (spec §25.4/§25.5)", () => {
  test("failures paginate over GET /admin/notification-failures", async () => {
    stubFetch(emptyPage());
    await listNotificationFailures({ limit: 10 });
    assert.equal(
      recorded?.url,
      "/api/v1/admin/notification-failures?limit=10",
    );
  });

  test("template create/update/enable/disable hit their verbs with exact bodies", async () => {
    const row = JSON.stringify({
      id: "tpl1",
      event_type: "ACCOUNT_SECURITY",
      channel: "SMS",
      title: "t",
      template_body: "b",
      enabled: true,
      version: 1,
    });
    stubFetch(row);
    await createNotificationTemplate({
      event_type: "ACCOUNT_SECURITY",
      channel: "SMS",
      title: "安全提醒",
      template_body: "您的账号 {nickname}",
    });
    assert.equal(recorded?.url, "/api/v1/admin/notification-templates");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      event_type: "ACCOUNT_SECURITY",
      channel: "SMS",
      title: "安全提醒",
      template_body: "您的账号 {nickname}",
    });

    stubFetch(row);
    await updateNotificationTemplate("tpl1", {
      title: "新标题",
      template_body: "新正文",
    });
    assert.equal(
      recorded?.url,
      "/api/v1/admin/notification-templates/tpl1",
    );
    assert.equal(recorded?.method, "PATCH");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      title: "新标题",
      template_body: "新正文",
    });

    stubFetch(row);
    await enableNotificationTemplate("tpl1");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/notification-templates/tpl1/enable",
    );
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.body, undefined);

    stubFetch(row);
    await disableNotificationTemplate("tpl1");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/notification-templates/tpl1/disable",
    );
  });
});

describe("named repairs + anonymous reveal", () => {
  test("release-occupied-assignment POSTs {assignment_id, reason}", async () => {
    stubFetch(
      JSON.stringify({
        id: "a1",
        task_id: "t1",
        availability_status: "AVAILABLE",
      }),
    );
    await releaseOccupiedAssignment("a1", "悬空占用修复");
    assert.equal(
      recorded?.url,
      "/api/v1/admin/repairs/release-occupied-assignment",
    );
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      assignment_id: "a1",
      reason: "悬空占用修复",
    });
  });

  test("force-fail-delivery POSTs {delivery_id, reason}", async () => {
    stubFetch(
      JSON.stringify({
        id: "d1",
        status: "FAILED",
        last_error: null,
        attempts: 3,
        updated_at: "2026-09-01T00:00:00Z",
      }),
    );
    await forceFailDelivery("d1", "投递卡死修复");
    assert.equal(recorded?.url, "/api/v1/admin/repairs/force-fail-delivery");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      delivery_id: "d1",
      reason: "投递卡死修复",
    });
  });

  test("reveal POSTs {reason} on the comment path (the sole identity surface)", async () => {
    stubFetch(
      JSON.stringify({
        user_id: "u1",
        nickname: "同学",
        username: "20260001",
      }),
    );
    const identity = await revealCommentIdentity("c1", "举报核查");
    assert.equal(identity.username, "20260001");
    assert.equal(recorded?.url, "/api/v1/admin/comments/c1/reveal-identity");
    assert.deepEqual(JSON.parse(String(recorded?.body)), { reason: "举报核查" });
  });
});
