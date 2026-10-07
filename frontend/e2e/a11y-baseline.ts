/**
 * P3-A: the accessibility baseline, per shot — EXISTING violations the
 * gate tolerates, each with a reason. Policy: fix one, DELETE its entry
 * (the gate fails on stale entries — a rotting baseline hides nothing).
 * NOTHING may be added without the owner ruling the violation
 * acceptable; new findings fail the gate outright.
 *
 * Keyed by shot name (same names as the pixel suite); fingerprint =
 * `${rule}(${impact})@${selector}` — exactly what a failure prints.
 * Discovery run 2026-10-08: 7/11 surfaces fully clean; the entries
 * below are the whole existing debt (color-contrast on deliberately
 * muted states + one card heading-order nit).
 */
export const A11Y_BASELINE: Record<string, { fingerprint: string; reason: string }[]> = {
  "dev-gallery": [
    {
      fingerprint: "color-contrast(serious)@.reward-unavailable",
      reason: "Unavailable-reward state renders in --subtle-foreground (the design system's quietest tone) — below AA by design intent; re-triage when the unavailable state gets its own token.",
    },
    {
      fingerprint: "color-contrast(serious)@.reward-unavailable", // both unavailable cards share the own-segment fingerprint
      reason: "Same unavailable-reward subtle tone as the entry above.",
    },
    {
      fingerprint: "color-contrast(serious)@.comment-edited",
      reason: "The '(已编辑)' annotation is a muted footnote tone; decorative metadata rather than content.",
    },
    {
      fingerprint: "color-contrast(serious)@.comment-tombstone-text",
      reason: "Deleted-comment tombstone is deliberately de-emphasized (the row is an audit trail, not content to read).",
    },
  ],
  "student-dashboard": [
    {
      fingerprint: "color-contrast(serious)@.rank-mini-n",
      reason: "The dashboard hero's mini-rank numeral rides the muted hero metadata line; the full rankings surface (clean here) carries the same number at AA.",
    },
  ],
  "student-tasks": [
    {
      fingerprint: "heading-order(moderate)@h3",
      reason: "Task cards title as h3 (the frozen card contract) directly under the page's h1 — /tasks has no h2 level. Fixing means an sr-only h2 or changing the card contract; owner call, tracked here.",
    },
  ],
  "student-claim": [
    {
      fingerprint: "color-contrast(serious)@.claim-step-label",
      reason: "Claim-strip future steps are SPEC-intentionally subtle ('future subtle', design §9 state machine) — contrast is the de-emphasis mechanism.",
    },
    // All three future-step nodes share the own-segment fingerprint
    // (dedup on the found side), so ONE entry covers them.
  ],
};
