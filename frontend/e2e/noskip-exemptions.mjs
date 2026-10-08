/**
 * Explicit no-skip exemptions (P1 automation): tests that legitimately
 * skip in the STANDARD battery run (the report this gate reads) because
 * their fixture contracts ride opt-in env flags. Every entry names the
 * gating variable and why the dedicated run — not the standard battery —
 * owns that coverage.
 *
 * Adding an entry: the skipped title substring must be tight enough to
 * match ONLY the intended test(s) (the gate logs every match it accepts;
 * an over-broad fragment hides future skips — keep them specific).
 */
export const NOSKIP_EXEMPTIONS = [
  {
    file: "auth-cross-tab-fence.spec.ts",
    title: "sibling explicit login fences this tab",
    gate: "CQ_E2E_FENCE=1",
    reason: "Fence suite runs as its own dedicated battery stage.",
  },
  {
    file: "auth-cross-tab-fence.spec.ts",
    title: "out-of-order /me: a straggler answering OLD identity",
    gate: "CQ_E2E_FENCE=1",
    reason: "Same dedicated fence stage (round-5 P0 scenario).",
  },
  {
    file: "auth-wave-invariant.spec.ts",
    title: "one rotation per cold-start wave",
    gate: "CQ_E2E_PROBE=1",
    reason: "Wave-probe instrumentation runs as its own battery stage.",
  },
  {
    file: "task-claim.spec.ts",
    title: "conflict shows typed copy",
    gate: "CQ_E2E_EMPTY_TASK_URL",
    reason:
      "Needs a task whose assignments are all taken; the orchestrated world exports no such deep link.",
  },
  {
    file: "notifications.spec.ts",
    title: "owner mark-read (the §28 owner-only surface)",
    gate: "runtime seed state",
    reason:
      "In-test conditional skip: needs a seeded UNREAD notification for the student at run time (the fixture seeds one, but an earlier same-battery consumer can drain it); the read-path coverage also rides the same spec's other green tests.",
  },
  {
    file: "rewards-ranking.spec.ts",
    title: "insufficient spendable disables the tile CTA",
    gate: "CQ_E2E_POOR_STUDENT",
    reason:
      "Needs a student whose spendable points sit below the cheapest reward; not part of the standard world export.",
  },
  {
    file: "submission.spec.ts",
    title: "mocked storage PUT: pinned wire shape",
    gate: "CQ_E2E_MOCK_STORAGE=1",
    reason:
      "Mock-mode test (inverse gate): runs only when real object storage is deliberately unavailable.",
  },
  // The four former "https-stack mixed content" exemptions are REMOVED:
  // the dedicated e2e MinIO https instance (compose profile "e2e",
  // :9002, presigned flow proven live during the run — objects observed
  // on the instance mid-battery, then wiped by the world clean) makes
  // the real-upload chain green on the https stack.
  ...["auth screens", "student core routes", "teacher routes", "admin routes"].map(
    (title) => ({
      file: "visual-capture.spec.ts",
      title,
      gate: "CQ_E2E_CAPTURE=1",
      reason:
        "Evidence-capture harness (screenshots for review), gated off the standard battery.",
    }),
  ),
  ...[
    "admin-users",
    "auth-login",
    "dev-gallery",
    "student-claim",
    "student-dashboard",
    "student-notifications",
    "student-rankings",
    "student-rewards",
    "student-task-detail",
    "student-tasks",
    "teacher-reviews",
  ].map((title) => ({
    file: "visual-regression.spec.ts",
    title,
    gate: "CQ_VISUAL=1",
    reason:
      "The pixel suite is its own battery/CI stage (`make visual-regression` + the CI visual job), skipped in the functional battery on purpose.",
  })),
];
