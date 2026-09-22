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
    // A FIXED skew from NOW, not a hardcoded calendar date: the store
    // discards samples beyond ±24h as proxy garbage, so a hardcoded
    // "Sep 21" header is a time bomb — the suite starts failing exactly
    // one day after the stamp (observed 2026-09-22). A -90s skew from
    // the current clock is always inside the window.
    const skewMs = 90_000;
    const serverMs = Date.now() - skewMs;
    const receivedAt = stubResponse({ Date: new Date(serverMs).toUTCString() });
    await apiRequest("/api/v1/tasks");
    const expected = serverMs - receivedAt();
    // The stub records its timestamp SYNCHRONOUSLY at the fetch call;
    // the estimator's clock read happens one microtask later, so the
    // stub-derived expectation is the UPPER bound of the sample and the
    // scheduling gap is one-sided: expected - offset ∈ [0, slack]. The
    // slack is generous (a loaded dev box can stall the gap for
    // seconds); what the assertion must catch is a WRONG SIGN or a
    // garbage-sized sample, both of which land far outside it.
    const offset = currentServerClockOffset();
    assert.ok(
      expected - offset >= 0 && expected - offset < 5_000,
      `offset ${offset} should be within [expected-5000, expected] of ${expected}`,
    );
  });

  test("204 responses observe the header too", async () => {
    stubResponse({ Date: new Date(Date.now() - 90_000).toUTCString() }, 204);
    await apiRequest("/api/v1/void");
    assert.notEqual(currentServerClockOffset(), 0);
  });

  test("error responses still feed the estimate", async () => {
    stubResponse({ Date: new Date(Date.now() - 90_000).toUTCString() }, 500);
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
