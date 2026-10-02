/**
 * QA #2 — shared data cache (lib/dataCache) contract tests.
 *
 * The behaviors the tester's complaint rides on: a remounting section
 * serves its cached snapshot SYNCHRONOUSLY (no refetch within the
 * fresh window), a stale entry revalidates in the background while
 * serving the old data, concurrent readers share one request, an auth
 * transition drops EVERYTHING, and errors never masquerade as success.
 */
import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

import {
  invalidateDataCache,
  peekDataCacheForTests,
  readCached,
  resetDataCacheForTests,
} from "../lib/dataCache";

beforeEach(() => {
  resetDataCacheForTests();
});

describe("readCached freshness", () => {
  test("fresh hit serves synchronously and does NOT call the loader", async () => {
    let loads = 0;
    const loader = () => {
      loads += 1;
      return Promise.resolve({ value: 1 });
    };
    const first = readCached("k", loader);
    if (first.kind !== "absent") throw new Error("expected absent");
    await first.promise;
    const outcome = readCached("k", loader);
    assert.equal(outcome.kind, "fresh");
    assert.deepEqual(outcome.data, { value: 1 });
    assert.equal(loads, 1);
  });

  test("force bypasses freshness (boundary refetch) but still dedupes", async () => {
    let loads = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const loader = () => {
      loads += 1;
      return gate.then(() => ({ value: loads }));
    };
    const seed = readCached("k", () => Promise.resolve({ value: 0 }));
    if (seed.kind !== "absent") throw new Error("expected absent");
    await seed.promise;
    const a = readCached("k", loader, { force: true });
    const b = readCached("k", loader, { force: true });
    assert.equal(a.kind, "stale");
    assert.equal(b.kind, "stale");
    release();
    const result = await (a as { revalidation: Promise<{ value: number }> }).revalidation;
    assert.equal(result.value, 1, "one load, both readers");
    assert.equal(loads, 1);
  });
});

describe("stale-while-revalidate", () => {
  test("a stale entry serves the old snapshot and revalidates", async () => {
    resetDataCacheForTests();
    // Age the entry past FRESH_MS by faking fetch time is not exposed —
    // exercise the stale path via force (same code path as aging).
    let loads = 0;
    const loader = () => {
      loads += 1;
      return Promise.resolve({ value: loads });
    };
    const seed = readCached("k", loader);
    if (seed.kind !== "absent") throw new Error("expected absent");
    await seed.promise;
    const outcome = readCached("k", loader, { force: true });
    if (outcome.kind !== "stale") throw new Error("expected stale");
    assert.deepEqual(outcome.data, { value: 1 }, "old snapshot served");
    const fresh = await outcome.revalidation;
    assert.deepEqual(fresh, { value: 2 });
    const after = readCached("k", loader);
    assert.equal(after.kind, "fresh");
    assert.deepEqual(after.data, { value: 2 });
  });

  test("a FAILED revalidation keeps the snapshot (stale-while-error) and frees the slot", async () => {
    let loads = 0;
    // Load 1 succeeds, load 2 fails (the outage), loads 3+ recover.
    const loader = () => {
      loads += 1;
      return loads === 2
        ? Promise.reject(new Error("network down"))
        : Promise.resolve({ value: "good" });
    };
    const seed = readCached("k", loader);
    if (seed.kind !== "absent") throw new Error("expected absent");
    await seed.promise;
    const outcome = readCached("k", loader, { force: true });
    if (outcome.kind !== "stale") throw new Error("expected stale");
    await assert.rejects(outcome.revalidation, /network down/);
    // The failed promise must not stay joinable: the next read starts a
    // NEW load (which now succeeds again).
    const retryOutcome = readCached("k", loader, { force: true });
    assert.equal(retryOutcome.kind, "stale");
    assert.deepEqual(retryOutcome.data, { value: "good" });
    await retryOutcome.revalidation;
    const settled = readCached<{ value: string }>("k", loader, { force: true });
    assert.equal(settled.kind, "stale");
    assert.deepEqual(await settled.revalidation, { value: "good" });
  });
});

describe("auth generation fence", () => {
  test("invalidateDataCache drops every key — user B never sees user A's data", async () => {
    const seed = readCached("mine", () => Promise.resolve({ secret: "A" }));
    if (seed.kind !== "absent") throw new Error("expected absent");
    await seed.promise;
    const before = peekDataCacheForTests();
    assert.equal(before.size, 1);
    invalidateDataCache();
    const after = peekDataCacheForTests();
    assert.equal(after.size, 0);
    assert.ok(after.generation > before.generation);
    const outcome = readCached<{ secret: string }>("mine", () => Promise.resolve({ secret: "B" }));
    if (outcome.kind !== "absent") throw new Error("old snapshot must not survive the fence");
    assert.deepEqual(await outcome.promise, { secret: "B" });
  });

  test("a fetch racing an invalidation writes NOTHING into the new generation", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const slow = readCached("k", () => gate.then(() => ({ who: "A" })));
    if (slow.kind !== "absent") throw new Error("expected absent");
    invalidateDataCache();
    release();
    await slow.promise.catch(() => {});
    const outcome = readCached<{ who: string }>("k", () => Promise.resolve({ who: "B" }));
    if (outcome.kind !== "absent") throw new Error("expected absent");
    assert.deepEqual(await outcome.promise, { who: "B" });
  });
});
