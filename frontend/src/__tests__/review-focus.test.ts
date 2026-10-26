import assert from "node:assert/strict";
import { test } from "node:test";
import { restoreReviewFocus } from "../features/innovation/reviewFocus";

function target({ connected = true, disabled = false } = {}) {
  let focused = false;
  return {
    element: { isConnected: connected, matches: () => disabled, focus: () => { focused = true; } } as unknown as HTMLElement,
    focused: () => focused,
  };
}

test("review close restores the live opener and falls back for disabled or removed openers", () => {
  for (const [connected, disabled] of [[true, false], [true, true], [false, false]]) {
    const opener = target({ connected, disabled });
    const fallback = target();
    restoreReviewFocus({ element: opener.element, epoch: 3 }, fallback.element, 3);
    assert.equal(opener.focused(), connected && !disabled);
    assert.equal(fallback.focused(), !connected || disabled);
  }
});

test("review close never focuses an old account's opener or fallback after epoch changes", () => {
  const opener = target();
  const fallback = target();
  restoreReviewFocus({ element: opener.element, epoch: 3 }, fallback.element, 4);
  assert.equal(opener.focused(), false);
  assert.equal(fallback.focused(), false);
});
