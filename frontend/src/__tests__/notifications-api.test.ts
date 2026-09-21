/**
 * Task 7 (Plan 09): notifications API wrappers against the fetch stub —
 * wire-shape pins for the S3 notifications surface (hand-written contract
 * until the merged snapshot regenerates `lib/api/schema`): the unread
 * filter's presence semantics, the bell's count poll (unread first page
 * -> total), and the bodyless per-item mark-read POST the router defines
 * (backend `app/modules/notifications/router.py`).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  fetchUnreadCount,
  listNotifications,
  markNotificationRead,
} from "../features/notifications/api";

type RecordedRequest = { url: string; method: string; body?: unknown };

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

const ITEM = {
  id: "44444444-4444-4444-8444-444444444444",
  event_type: "REVISION_REQUIRED",
  title: "老师已退回修改",
  body: "奖励档位已保留，请在截止时间前提交新版本。",
  read_at: null as string | null,
  created_at: "2026-09-21T09:30:00Z",
};

const PAGE = {
  items: [ITEM],
  total: 41,
  limit: 20,
  offset: 0,
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

describe("GET /api/v1/notifications", () => {
  test("no options -> bare path (server defaults: no filter, limit 20)", async () => {
    stubFetch(JSON.stringify({ ...PAGE, items: [] }));
    await listNotifications();
    assert.equal(recorded?.url, "/api/v1/notifications");
    assert.equal(recorded?.method, "GET");
  });

  test("unread rides ONLY as ?unread=true; false stays absent (backend default)", async () => {
    stubFetch(JSON.stringify(PAGE));
    await listNotifications({ unread: true, limit: 20, offset: 20 });
    assert.equal(
      recorded?.url,
      "/api/v1/notifications?unread=true&limit=20&offset=20",
    );
    await listNotifications({ unread: false });
    assert.equal(recorded?.url, "/api/v1/notifications");
  });

  test("decodes one inbox page (the DTO's own fields only)", async () => {
    stubFetch(JSON.stringify(PAGE));
    const page = await listNotifications({ unread: true });
    assert.equal(page.total, 41);
    assert.equal(page.items[0].event_type, "REVISION_REQUIRED");
    assert.equal(page.items[0].read_at, null);
    assert.deepEqual(Object.keys(page.items[0]).sort(), [
      "body",
      "created_at",
      "event_type",
      "id",
      "read_at",
      "title",
    ]);
  });
});

describe("the bell's unread count (unread first page -> total)", () => {
  test("polls limit=1 unread and returns the server's total, never a client count", async () => {
    stubFetch(
      JSON.stringify({ items: [ITEM], total: 5, limit: 1, offset: 0 }),
    );
    const count = await fetchUnreadCount();
    assert.equal(
      recorded?.url,
      "/api/v1/notifications?unread=true&limit=1",
    );
    assert.equal(count, 5);
  });
});

describe("POST /api/v1/notifications/{id}/read", () => {
  test("bodyless owner mark-read; the echo carries the authoritative read_at", async () => {
    stubFetch(JSON.stringify({ ...ITEM, read_at: "2026-09-21T10:00:00Z" }));
    const echo = await markNotificationRead(ITEM.id);
    assert.equal(
      recorded?.url,
      `/api/v1/notifications/${ITEM.id}/read`,
    );
    assert.equal(recorded?.method, "POST");
    // No body: the endpoint takes none (router signature).
    assert.equal(recorded?.body, undefined);
    assert.equal(echo.read_at, "2026-09-21T10:00:00Z");
  });
});
