/**
 * Task 3 (Plan 09): tasks/claims API wrappers against the fetch stub —
 * wire-shape pins for the discovery list (offset pagination), detail, and
 * own-claims endpoints the dashboard and discovery surfaces call.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { getTask, listMyClaims, listTasks } from "../features/tasks/api";

type RecordedRequest = { url: string; method: string };

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

function taskCard(id: string, overrides: Record<string, unknown> = {}) {
  return JSON.stringify({
    id,
    title: "图书馆书影采集",
    rarity: "RARE",
    base_reward_points: 160,
    deadline_mode: "FIXED",
    fixed_deadline_at: "2026-09-30T18:00:00Z",
    duration_minutes: null,
    assignments_available: 5,
    rating: { average: 4.5, count: 8 },
    ...overrides,
  });
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("GET /api/v1/tasks", () => {
  test("sends offset pagination and decodes the §42 card page", async () => {
    stubFetch(
      JSON.stringify({
        items: [
          JSON.parse(taskCard("11111111-1111-4111-8111-111111111111")),
          JSON.parse(taskCard("22222222-2222-4222-8222-222222222222", { rating: null })),
        ],
        total: 27,
        limit: 12,
        offset: 12,
      }),
    );
    const page = await listTasks({ limit: 12, offset: 12 });
    assert.equal(recorded?.url, "/api/v1/tasks?limit=12&offset=12");
    assert.equal(recorded?.method, "GET");
    assert.equal(page.total, 27);
    assert.equal(page.items.length, 2);
    const [first, second] = page.items;
    // Card fields exactly as the backend names them (§42).
    assert.equal(first.title, "图书馆书影采集");
    assert.equal(first.rarity, "RARE");
    assert.equal(first.base_reward_points, 160);
    assert.equal(first.deadline_mode, "FIXED");
    assert.equal(first.assignments_available, 5);
    assert.deepEqual(first.rating, { average: 4.5, count: 8 });
    assert.equal(second.rating, null);
  });

  test("no query options -> bare path (server defaults apply)", async () => {
    stubFetch(JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }));
    await listTasks();
    assert.equal(recorded?.url, "/api/v1/tasks");
  });
});

describe("GET /api/v1/tasks/{task_id}", () => {
  test("encodes the id and decodes detail + own claim", async () => {
    stubFetch(
      JSON.stringify({
        id: "11111111-1111-4111-8111-111111111111",
        title: "图书馆书影采集",
        description: "拍摄指定书架的书影",
        task_type: "DATA_CRAWL",
        rarity: "EPIC",
        base_reward_points: 200,
        status: "PUBLISHED",
        deadline_mode: "RELATIVE",
        fixed_deadline_at: null,
        duration_minutes: 240,
        published_at: "2026-09-19T10:00:00Z",
        assignments_available: 3,
        rating: null,
        my_claim: null,
      }),
    );
    const detail = await getTask("11111111-1111-4111-8111-111111111111");
    assert.equal(recorded?.url, "/api/v1/tasks/11111111-1111-4111-8111-111111111111");
    assert.equal(detail.duration_minutes, 240);
    assert.equal(detail.my_claim, null);
    assert.equal(detail.description, "拍摄指定书架的书影");
  });
});

describe("GET /api/v1/me/claims", () => {
  test("pagination rides the query; rows decode with task context", async () => {
    stubFetch(
      JSON.stringify({
        items: [
          {
            claim_id: "33333333-3333-4333-8333-333333333333",
            task_id: "11111111-1111-4111-8111-111111111111",
            task_title: "图书馆书影采集",
            status: "REVISION_REQUIRED",
            platform: "小红书",
            keyword: "图书馆",
            claimed_at: "2026-09-20T08:00:00Z",
            deadline_at: "2026-09-20T20:00:00Z",
            grace_deadline_at: "2026-09-21T20:00:00Z",
            base_reward_points_snapshot: 200,
          },
        ],
        total: 1,
        limit: 10,
        offset: 0,
      }),
    );
    const page = await listMyClaims({ limit: 10 });
    assert.equal(recorded?.url, "/api/v1/me/claims?limit=10");
    assert.equal(page.items[0].task_title, "图书馆书影采集");
    assert.equal(page.items[0].status, "REVISION_REQUIRED");
    assert.equal(page.items[0].platform, "小红书");
  });
});
