/**
 * Task 6 (Plan 09): community API wrappers against the fetch stub —
 * wire-shape pins for the S2 community surface (hand-written contract
 * until the merged snapshot regenerates `lib/api/schema`):
 * paths, methods, bodies (extra-free by the backend's extra=forbid),
 * the server sort param, and the toggle echoes the UI reconciles from.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  castCommentVote,
  createTaskComment,
  DEFAULT_EMOJI_WHITELIST,
  deleteOwnComment,
  editOwnComment,
  listTaskComments,
  putTaskRating,
  REPORT_CATEGORY_OPTIONS,
  reportComment,
  toggleCommentReaction,
} from "../features/community/api";

type RecordedRequest = { url: string; method: string; body?: unknown };

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

const COMMENT = {
  id: "11111111-1111-4111-8111-111111111111",
  task_id: "22222222-2222-4222-8222-222222222222",
  parent_id: null,
  content: "第一",
  is_anonymous: false,
  author_display: "同学甲",
  created_at: "2026-09-20T10:00:00Z",
  updated_at: "2026-09-20T10:00:00Z",
  edited: false,
  deleted: false,
};

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const rawBody = init?.body;
    let body: unknown;
    if (typeof rawBody === "string") {
      body = JSON.parse(rawBody);
    }
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      body,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("GET /api/v1/tasks/{id}/comments", () => {
  test("sends the server sort + offset page and decodes the page", async () => {
    stubFetch(
      JSON.stringify({ items: [COMMENT], total: 41, limit: 20, offset: 40 }),
    );
    const page = await listTaskComments("t-1", {
      sort: "hot",
      limit: 20,
      offset: 40,
    });
    assert.equal(
      recorded?.url,
      "/api/v1/tasks/t-1/comments?sort=hot&limit=20&offset=40",
    );
    assert.equal(recorded?.method, "GET");
    assert.equal(page.total, 41);
    assert.equal(page.items[0].author_display, "同学甲");
  });

  test("no options -> bare path (server defaults: latest, limit 20, offset 0)", async () => {
    stubFetch(JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }));
    await listTaskComments("t-1");
    assert.equal(recorded?.url, "/api/v1/tasks/t-1/comments");
  });
});

describe("POST /api/v1/tasks/{id}/comments (201)", () => {
  test("root comment: content + identity only — no author override exists", async () => {
    stubFetch(JSON.stringify(COMMENT), 201);
    const created = await createTaskComment("t-1", {
      content: "第一",
      is_anonymous: true,
    });
    assert.equal(recorded?.url, "/api/v1/tasks/t-1/comments");
    assert.equal(recorded?.method, "POST");
    // Exactly the three spec §21.1/§21.4 inputs the backend accepts.
    assert.deepEqual(recorded?.body, { content: "第一", is_anonymous: true });
    assert.equal(created.id, COMMENT.id);
  });

  test("reply carries the parent id; tombstone parents are the server's refusal", async () => {
    stubFetch(JSON.stringify(COMMENT), 201);
    await createTaskComment("t-1", {
      content: "回复",
      parent_id: "11111111-1111-4111-8111-111111111111",
    });
    assert.deepEqual(recorded?.body, {
      content: "回复",
      parent_id: "11111111-1111-4111-8111-111111111111",
    });
  });
});

describe("comment vote toggle (POST /comments/{id}/vote)", () => {
  test("value is exactly -1 | 0 | 1; the echo is authoritative state", async () => {
    stubFetch(
      JSON.stringify({ current_value: 0, likes: 3, dislikes: 1 }),
    );
    const echo = await castCommentVote("c-9", 0); // pressing 赞 again removes
    assert.equal(recorded?.url, "/api/v1/comments/c-9/vote");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(recorded?.body, { value: 0 });
    assert.equal(echo.current_value, 0);
    assert.equal(echo.likes, 3);
    assert.equal(echo.dislikes, 1);
  });
});

describe("emoji reaction toggle (POST /comments/{id}/reactions)", () => {
  test("body is exactly the emoji; counts echo per emoji (aggregated)", async () => {
    stubFetch(
      JSON.stringify({ emoji: "🔥", added: true, counts: { "🔥": 2, "👍": 5 } }),
    );
    const echo = await toggleCommentReaction("c-9", "🔥");
    assert.equal(recorded?.url, "/api/v1/comments/c-9/reactions");
    assert.deepEqual(recorded?.body, { emoji: "🔥" });
    assert.equal(echo.added, true);
    assert.deepEqual(echo.counts, { "🔥": 2, "👍": 5 });
  });

  test("the whitelist mirror is exactly the spec §22 eight defaults", () => {
    assert.deepEqual([...DEFAULT_EMOJI_WHITELIST], [
      "👍",
      "❤️",
      "😂",
      "🎉",
      "😭",
      "👀",
      "🤔",
      "🔥",
    ]);
  });
});

describe("report (POST /comments/{id}/reports, 201)", () => {
  test("category + optional note; blank note rides as null", async () => {
    stubFetch(
      JSON.stringify({
        id: "33333333-3333-4333-8333-333333333333",
        comment_id: "c-9",
        category: "HARASSMENT",
        note: null,
        status: "OPEN",
        created_at: "2026-09-20T11:00:00Z",
      }),
      201,
    );
    const report = await reportComment("c-9", {
      category: "HARASSMENT",
      note: null,
    });
    assert.equal(recorded?.url, "/api/v1/comments/c-9/reports");
    assert.deepEqual(recorded?.body, { category: "HARASSMENT", note: null });
    assert.equal(report.status, "OPEN");
  });

  test("the category set is the closed §23 enum (exact strings)", () => {
    assert.deepEqual(
      REPORT_CATEGORY_OPTIONS.map((option) => option.value),
      ["SPAM", "HARASSMENT", "PRIVACY", "OTHER"],
    );
  });
});

describe("rating upsert (PUT /tasks/{id}/rating)", () => {
  test("body is the 1-5 integer; the echo carries the rater's own value", async () => {
    stubFetch(
      JSON.stringify({
        task_id: "t-1",
        rating: 4,
        created_at: "2026-09-20T12:00:00Z",
        updated_at: "2026-09-20T12:00:00Z",
      }),
    );
    const echo = await putTaskRating("t-1", 4);
    assert.equal(recorded?.url, "/api/v1/tasks/t-1/rating");
    assert.equal(recorded?.method, "PUT");
    assert.deepEqual(recorded?.body, { rating: 4 });
    assert.equal(echo.rating, 4);
  });
});

describe("owner edit/delete", () => {
  test("edit sends content only (parent_id is unrepresentable, §21.3)", async () => {
    stubFetch(JSON.stringify({ ...COMMENT, edited: true }));
    await editOwnComment("c-9", "改后");
    assert.equal(recorded?.url, "/api/v1/comments/c-9");
    assert.equal(recorded?.method, "PATCH");
    assert.deepEqual(recorded?.body, { content: "改后" });
  });

  test("delete is bodyless and resolves undefined on 204", async () => {
    stubFetch("", 204);
    const result = await deleteOwnComment("c-9");
    assert.equal(recorded?.url, "/api/v1/comments/c-9");
    assert.equal(recorded?.method, "DELETE");
    assert.equal(result, undefined);
  });
});
