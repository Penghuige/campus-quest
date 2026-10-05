/**
 * Plan-12 Task 7: the dev-only component gallery's contract pins.
 *
 * The page itself is the deliverable (it renders the shared primitive ×
 * variant matrix for visual review and the Task 9 pixel baseline); this
 * test pins the two properties that keep it safe and useful:
 *
 * - production gate: the route must 404 outside development, through the
 *   `notFound()` mechanism (bundled Next 16 docs: api-reference/functions/
 *   not-found) keyed on NODE_ENV — the build runs with
 *   NODE_ENV=production, so the gate also covers `next build`'s
 *   prerender pass;
 * - matrix coverage: the primitive families the gallery exists to make
 *   inspectable must actually appear in the source (spot-pinned here:
 *   the Phase A `.btn-danger` fix, the rarity accent class, and the two
 *   section-state primitives).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("dev gallery is production-gated and covers the primitive matrix", () => {
  const src = readFileSync("src/app/dev/gallery/page.tsx", "utf8");
  assert.match(src, /process\.env\.NODE_ENV === "production"/);
  assert.match(src, /notFound\(\)/);
  for (const cls of [
    "btn-danger",
    "task-card-rarity",
    "SectionSkeleton",
    "EmptyState",
  ])
    assert.ok(src.includes(cls), `gallery covers ${cls}`);
});
