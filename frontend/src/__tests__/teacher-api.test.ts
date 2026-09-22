/**
 * Task 9 (Plan 09): teacher workbench API wrappers against a stubbed
 * fetch transport — wire-shape pins for every endpoint the teacher
 * workspace calls, transcribed from the backend routers
 * (`tasks/router.py`, `submissions/router.py`, the S2
 * `community/router.py`).
 *
 * Pins (the merge-time drift guards for the hand-written S2 part):
 * - paths + methods + exact field names for list/detail/create/lifecycle/
 *   import preview+confirm/collaborators/statistics/review-queue/approve/
 *   revision/invalidate/download-mint;
 * - the import preview's RAW-BODY transport: the CSV rides the request
 *   body as a Blob with `Content-Type: text/csv` (no multipart);
 * - cookie credentials on every call (the shared apiRequest contract).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  addCollaborator,
  approveSubmission,
  updateTeacherTask,
  confirmAssignmentImport,
  createTeacherTask,
  getTaskStatistics,
  getTeacherTask,
  invalidateRewardLock,
  listModerationComments,
  listReviewQueue,
  listTaskReports,
  listTeacherTasks,
  mintSubmissionDownload,
  moderateDeleteComment,
  previewAssignmentImport,
  removeCollaborator,
  requireRevision,
  runTaskLifecycle,
} from "../features/admin/teacherApi";

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

describe("teacher task endpoints", () => {
  test("list sends offset pagination on GET /teacher/tasks", async () => {
    stubFetch(emptyPage());
    await listTeacherTasks({ limit: 10, offset: 20 });
    assert.equal(recorded?.url, "/api/v1/teacher/tasks?limit=10&offset=20");
    assert.equal(recorded?.method, "GET");
    assert.equal(recorded?.credentials, "include");
  });

  test("detail URL-encodes the task id", async () => {
    stubFetch(JSON.stringify({ id: "t", status: "DRAFT" }));
    await getTeacherTask("task/uuid-1");
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/tasks/task%2Fuuid-1",
    );
  });

  test("create posts the full TaskCreateRequest body", async () => {
    stubFetch(JSON.stringify({ id: "t" }), 201);
    await createTeacherTask({
      title: "采集",
      description: "描述",
      base_reward_points: 120,
      deadline_mode: "FIXED",
      allowed_file_types: ["CSV"],
      max_file_size_bytes: 52_428_800,
      task_type: "DATA_CRAWL",
      rarity: "NORMAL",
      fixed_deadline_at: "2030-09-30T10:00:00.000Z",
      duration_minutes: null,
      claim_cutoff_minutes: 240,
      submission_schema: { columns: ["platform"] },
      submission_schema_version: 1,
      notify_24h: true,
      notify_4h: true,
      notification_channels: ["SMS", "EMAIL", "IN_APP"],
    });
    assert.equal(recorded?.url, "/api/v1/teacher/tasks");
    assert.equal(recorded?.method, "POST");
    const body = JSON.parse(String(recorded?.body)) as Record<string, unknown>;
    assert.equal(body.deadline_mode, "FIXED");
    assert.equal(body.task_type, "DATA_CRAWL");
    assert.deepEqual(body.notification_channels, ["SMS", "EMAIL", "IN_APP"]);
    assert.deepEqual(body.allowed_file_types, ["CSV"]);
  });

  test("lifecycle verbs POST /teacher/tasks/{id}/{verb}", async () => {
    const verbs = ["publish", "pause", "resume", "close", "archive"] as const;
    for (const verb of verbs) {
      stubFetch(
        JSON.stringify({
          task_id: "t1",
          status: "PUBLISHED",
          claimable: true,
          published_at: null,
          closed_at: null,
        }),
      );
      await runTaskLifecycle("t1", verb);
      assert.equal(recorded?.url, `/api/v1/teacher/tasks/t1/${verb}`);
      assert.equal(recorded?.method, "POST");
    }
  });

  test("collaborators: PUT carries {permissions}; DELETE is bare", async () => {
    stubFetch(
      JSON.stringify({
        task_id: "t1",
        teacher_id: "t2",
        permissions: ["VIEW_TASK"],
      }),
    );
    await addCollaborator("t1", "t2", ["VIEW_TASK"]);
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/tasks/t1/collaborators/t2",
    );
    assert.equal(recorded?.method, "PUT");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      permissions: ["VIEW_TASK"],
    });

    stubFetch("", 204);
    await removeCollaborator("t1", "t2");
    assert.equal(recorded?.method, "DELETE");
  });

  test("update PATCHes a DIFF-ONLY partial body verbatim", async () => {
    stubFetch(JSON.stringify({ id: "t1", status: "DRAFT" }));
    await updateTeacherTask("t1", {
      title: "新标题",
      submission_schema: { columns: ["platform"] },
      submission_schema_version: 1,
    });
    assert.equal(recorded?.url, "/api/v1/teacher/tasks/t1");
    assert.equal(recorded?.method, "PATCH");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      title: "新标题",
      submission_schema: { columns: ["platform"] },
      submission_schema_version: 1,
    });
    assert.equal(recorded?.credentials, "include");
  });

  test("statistics rides GET /teacher/tasks/{id}/statistics", async () => {
    stubFetch(JSON.stringify({ task_id: "t1", submission_counts: {} }));
    await getTaskStatistics("t1");
    assert.equal(recorded?.url, "/api/v1/teacher/tasks/t1/statistics");
  });
});

describe("assignment import endpoints (spec §7.1)", () => {
  test("preview sends the file as the RAW body with text/csv", async () => {
    stubFetch(
      JSON.stringify({
        task_id: "t1",
        total_rows: 0,
        valid_count: 0,
        error_count: 0,
        errors: [],
        preview_token: "tok",
        expires_at: null,
      }),
    );
    const csv = new Blob(["platform,keyword\n"], { type: "text/csv" });
    await previewAssignmentImport("t1", csv);
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/tasks/t1/assignments/import/preview",
    );
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.headers.get("Content-Type"), "text/csv");
    assert.ok(recorded?.body instanceof Blob, "body must be the raw file");
    assert.equal(recorded?.credentials, "include");
  });

  test("confirm posts the single-use preview_token", async () => {
    stubFetch(JSON.stringify({ task_id: "t1", inserted: 3 }));
    const result = await confirmAssignmentImport("t1", "tok-1");
    assert.equal(result.inserted, 3);
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/tasks/t1/assignments/import/confirm",
    );
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      preview_token: "tok-1",
    });
  });
});

describe("review endpoints (spec §11.3/§14/§41)", () => {
  test("queue paginates over GET /teacher/submissions/review-queue", async () => {
    stubFetch(emptyPage());
    await listReviewQueue({ limit: 5, offset: 5 });
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/submissions/review-queue?limit=5&offset=5",
    );
  });

  test("approve is a bare POST", async () => {
    stubFetch(
      JSON.stringify({
        claim_id: "c1",
        claim_status: "COMPLETED",
        reward_lock_status: "CONFIRMED",
        points_granted: 120,
        already_reviewed: false,
      }),
    );
    await approveSubmission("s1");
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/submissions/s1/approve",
    );
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.body, undefined);
  });

  test("revision-required carries the mandatory note", async () => {
    stubFetch(
      JSON.stringify({
        claim_id: "c1",
        claim_status: "REVISION_REQUIRED",
        reward_lock_status: "PROVISIONAL",
        revision_deadline_at: "2030-01-01T00:00:00Z",
      }),
    );
    await requireRevision("s1", "请补充 keyword 列");
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      note: "请补充 keyword 列",
    });
  });

  test("invalidate-reward-lock carries the mandatory reason", async () => {
    stubFetch(
      JSON.stringify({
        claim_id: "c1",
        claim_status: "REVISION_REQUIRED",
        reward_lock_status: "INVALIDATED",
        revision_deadline_at: "2030-01-01T00:00:00Z",
      }),
    );
    await invalidateRewardLock("s1", "内容与分配关键词不符");
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/submissions/s1/invalidate-reward-lock",
    );
    assert.deepEqual(JSON.parse(String(recorded?.body)), {
      reason: "内容与分配关键词不符",
    });
  });

  test("download mint rides the shared submissions download route", async () => {
    stubFetch(
      JSON.stringify({ url: "https://storage/presigned", expires_at: "2030-01-01T00:00:00Z" }),
    );
    await mintSubmissionDownload("s1");
    assert.equal(recorded?.url, "/api/v1/submissions/s1/download");
    assert.equal(recorded?.method, "GET");
  });
});

describe("S2 community moderation endpoints (hand-written contract)", () => {
  test("moderation comments list the Teacher-safe page", async () => {
    stubFetch(emptyPage());
    await listModerationComments("t1", { limit: 20 });
    assert.equal(
      recorded?.url,
      "/api/v1/teacher/tasks/t1/comments/moderation?limit=20",
    );
  });

  test("report queue rides GET /teacher/tasks/{id}/reports", async () => {
    stubFetch(emptyPage());
    await listTaskReports("t1");
    assert.equal(recorded?.url, "/api/v1/teacher/tasks/t1/reports");
  });

  test("moderation delete carries the mandatory reason in the body", async () => {
    stubFetch("", 204);
    await moderateDeleteComment("c1", "垃圾信息");
    assert.equal(recorded?.url, "/api/v1/teacher/comments/c1");
    assert.equal(recorded?.method, "DELETE");
    // DELETE with a body is the backend's documented shape (reason is
    // mandatory at the transport, spec §21.4).
    assert.deepEqual(JSON.parse(String(recorded?.body)), { reason: "垃圾信息" });
  });
});
