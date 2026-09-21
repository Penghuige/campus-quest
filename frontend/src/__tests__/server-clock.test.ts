/**
 * Task 4 FOLD: the clock-offset estimator wired into `apiRequest` — every
 * response `Date` header feeds the shared store `useNow` consumes
 * (spec §9.3; patterns §14), plus the clamp/absent-header guards.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { apiRequest } from "../lib/api";
import {
  currentServerClockOffset,
  observeServerDateHeader,
  resetServerClockForTests,
} from "../lib/serverClock";

const originalFetch = globalThis.fetch;

beforeEach(() => {
  resetServerClockForTests();
});

afterEach(() => {
  resetServerClockForTests();
  globalThis.fetch = originalFetch;
});

function stubResponse(headers: Record<string, string>, status = 200): () => number {
  let receivedAt = 0;
  globalThis.fetch = (async () => {
    receivedAt = Date.now();
    // Null-body statuses reject a body (undici mirrors the spec).
    const body = status === 204 || status === 205 || status === 304 ? null : "{}";
    return new Response(body, { status, headers });
  }) as typeof fetch;
  return () => receivedAt;
}

describe("apiRequest feeds the server-clock store from the Date header", () => {
  test("a valid Date header becomes the offset (header - receivedAt)", async () => {
    const serverMs = Date.parse("2026-09-21T10:00:00Z");
    const receivedAt = stubResponse({ Date: "Mon, 21 Sep 2026 10:00:00 GMT" });
    await apiRequest("/api/v1/tasks");
    const expected = serverMs - receivedAt();
    // The stub records receive time synchronously; allow scheduling slack.
    assert.ok(
      Math.abs(currentServerClockOffset() - expected) < 100,
      `offset ${currentServerClockOffset()} should be ~${expected}`,
    );
  });

  test("204 responses observe the header too", async () => {
    stubResponse({ Date: "Mon, 21 Sep 2026 10:00:00 GMT" }, 204);
    await apiRequest("/api/v1/void");
    assert.notEqual(currentServerClockOffset(), 0);
  });

  test("error responses still feed the estimate", async () => {
    stubResponse({ Date: "Mon, 21 Sep 2026 10:00:00 GMT" }, 500);
    await assert.rejects(apiRequest("/api/v1/tasks"));
    assert.notEqual(currentServerClockOffset(), 0);
  });

  test("missing Date header leaves offset 0", async () => {
    stubResponse({});
    await apiRequest("/api/v1/tasks");
    assert.equal(currentServerClockOffset(), 0);
  });

  test("an absurd sample (>24h off) is discarded as garbage", () => {
    const twoDaysAgo = new Date(Date.now() - 48 * 60 * 60 * 1000);
    observeServerDateHeader(twoDaysAgo.toUTCString());
    assert.equal(currentServerClockOffset(), 0);
  });

  test("an unparseable header is ignored", () => {
    observeServerDateHeader("not a date");
    assert.equal(currentServerClockOffset(), 0);
  });

  test("latest valid sample wins", () => {
    // The HTTP Date header carries whole seconds only (the documented
    // ±1s granularity), so expectations compare against the PARSED
    // header, not the raw offset that was fed in.
    const firstHeaderMs = Date.parse(new Date(Date.now() + 5_000).toUTCString());
    observeServerDateHeader(new Date(firstHeaderMs).toUTCString());
    const first = currentServerClockOffset();

    const secondHeaderMs = Date.parse(new Date(Date.now() + 60_000).toUTCString());
    observeServerDateHeader(new Date(secondHeaderMs).toUTCString());
    assert.ok(currentServerClockOffset() > first, "second sample must move the estimate");
    assert.ok(
      Math.abs(currentServerClockOffset() - (secondHeaderMs - Date.now())) < 50,
      "offset tracks the latest parsed header",
    );
  });
});
