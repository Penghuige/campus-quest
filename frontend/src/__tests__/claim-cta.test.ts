/**
 * Defect (owner, 2026-10-10 production): a depleted task's DETAIL page
 * kept the claim button pressable — the student pressed and only then
 * met the NO_ASSIGNMENT_AVAILABLE rejection. The page must say
 * "cannot claim" up front: the CTA's shape derives from the same
 * server availability number the square card already carries.
 *
 * The concurrent-race window (availability was >0 at render, the last
 * slot left before the press) keeps the typed 409 copy as the
 * authoritative fallback — claimErrors' mapping stays unit-covered;
 * this table only owns the PRE-PRESS verdict.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { claimCtaView } from "../features/tasks/display";

test("an open task offers the claim CTA", () => {
  assert.equal(claimCtaView(3, false).kind, "claim");
  assert.equal(claimCtaView(1, false).kind, "claim");
});

test("a depleted task disables the CTA up front — no pressable lie", () => {
  const view = claimCtaView(0, false);
  assert.equal(view.kind, "depleted");
  if (view.kind === "depleted") {
    assert.equal(view.ctaLabel, "名额已满");
    assert.equal(view.hint, "该任务的所有名额都已被领取，看看其他任务吧");
  }
});

test("an existing claim wins over availability — the panel owns the page", () => {
  // my_claim present means the viewer already holds a unit; the
  // availability number is irrelevant to their CTA.
  assert.equal(claimCtaView(0, true).kind, "claim");
  assert.equal(claimCtaView(2, true).kind, "claim");
});
