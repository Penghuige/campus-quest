/**
 * Owner ruling 2026-10-10 (task-square batch ②): EVERY task card takes
 * the orchestrated entrance, and the grid stays FAST at any size — the
 * per-card stagger interval is list-length-aware so the stagger TAIL
 * (last card's delay) never exceeds 80ms, the budget the old
 * three-child cap spent (2 × --stagger 40ms). Total entrance =
 * --motion-in 220ms + tail ≤ 80ms = the §11 ~300ms ceiling, unchanged.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { entranceStaggerMs } from "../features/tasks/display";

test("small lists keep the classic 40ms interval", () => {
  assert.equal(entranceStaggerMs(0), 0);
  assert.equal(entranceStaggerMs(1), 0);
  assert.equal(entranceStaggerMs(2), 40);
  assert.equal(entranceStaggerMs(3), 40);
});

test("the stagger tail is bounded at ANY list size", () => {
  for (const count of [4, 12, 24, 50, 200]) {
    const interval = entranceStaggerMs(count);
    const tail = (count - 1) * interval;
    assert.ok(tail <= 80.0001, `count ${count}: tail ${tail}ms exceeds the 80ms budget`);
    assert.ok(interval > 0, `count ${count}: interval must stay positive`);
  }
});

test("the interval only ever SHRINKS as the list grows", () => {
  // load-more appends cards: a growing interval would push already
  // completed animations back before their delay (a visible rewind).
  // The regime that matters starts at TWO cards (a one-card grid has
  // no stagger at all, and its only card's delay is 0 by
  // construction — index 0 — so growing out of count=1 rewinds
  // nothing).
  let previous = entranceStaggerMs(2);
  for (let count = 3; count <= 60; count += 1) {
    const interval = entranceStaggerMs(count);
    assert.ok(interval <= previous, `interval grew at count ${count}`);
    previous = interval;
  }
});
