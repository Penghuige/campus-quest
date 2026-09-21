/**
 * Task 9 (Plan 09): offset-page accumulation pins — the T8-review fold
 * (the loadMore merge semantics get a unit pin). These helpers ARE the
 * production path: NotificationInbox, CommentThread, and the three
 * teacher workbench lists all accumulate offset pages through
 * `mergeOffsetPage`, so the dedup/order/total semantics live exactly
 * once and are pinned here.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { hasMorePages, mergeOffsetPage } from "../lib/offsetPages";

interface Row {
  id: string;
  label: string;
}

describe("mergeOffsetPage", () => {
  test("keys on the caller's identity field (e.g. submission_id)", () => {
    const queue = [
      { submission_id: "s1", version: 1 },
      { submission_id: "s2", version: 1 },
    ];
    const overlap = [
      { submission_id: "s2", version: 1 },
      { submission_id: "s3", version: 2 },
    ];
    const merged = mergeOffsetPage(queue, overlap, (row) => row.submission_id);
    assert.deepEqual(merged.map((row) => row.submission_id), ["s1", "s2", "s3"]);
  });

  test("appends unseen rows in server order, keeping loaded rows first", () => {
    const loaded: Row[] = [
      { id: "a", label: "1" },
      { id: "b", label: "2" },
    ];
    const incoming: Row[] = [
      { id: "c", label: "3" },
      { id: "d", label: "4" },
    ];
    const byId = (row: Row) => row.id;
    assert.deepEqual(mergeOffsetPage(loaded, incoming, byId), [...loaded, ...incoming]);
  });

  test("an overlapping page (rows shifted by a concurrent insert) dedups by id", () => {
    const loaded: Row[] = [
      { id: "a", label: "1" },
      { id: "b", label: "2" },
    ];
    // Offset window moved: the second page re-delivers b before c.
    const incoming: Row[] = [
      { id: "b", label: "2" },
      { id: "c", label: "3" },
    ];
    assert.deepEqual(mergeOffsetPage(loaded, incoming, (row) => row.id), [
      { id: "a", label: "1" },
      { id: "b", label: "2" },
      { id: "c", label: "3" },
    ]);
  });

  test("a fully overlapping page is a no-op (no duplicate ids, ever)", () => {
    const loaded: Row[] = [{ id: "a", label: "1" }];
    assert.deepEqual(mergeOffsetPage(loaded, loaded, (row) => row.id), loaded);
  });

  test("empty inputs stay empty; empty incoming is safe", () => {
    const empty: Row[] = [];
    assert.deepEqual(mergeOffsetPage(empty, [], (row) => row.id), []);
    assert.deepEqual(
      mergeOffsetPage([{ id: "a", label: "1" }], [], (row) => row.id),
      [{ id: "a", label: "1" }],
    );
  });

  test("does not mutate its inputs", () => {
    const loaded: Row[] = [{ id: "a", label: "1" }];
    const incoming: Row[] = [{ id: "a", label: "1" }, { id: "b", label: "2" }];
    const snapshot = [...loaded];
    mergeOffsetPage(loaded, incoming, (row) => row.id);
    assert.deepEqual(loaded, snapshot);
  });
});

describe("hasMorePages", () => {
  test("true exactly while loaded count is below the server total", () => {
    assert.equal(hasMorePages(0, 20), true);
    assert.equal(hasMorePages(19, 20), true);
    assert.equal(hasMorePages(20, 20), false);
    assert.equal(hasMorePages(24, 20), false);
  });

  test("an empty list has no more pages", () => {
    assert.equal(hasMorePages(0, 0), false);
  });
});
