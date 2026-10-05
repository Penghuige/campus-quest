# Plan 13 before-evidence — student secondary surfaces (T0)

Captured **before any plan-13 visual code** at branch
`campusquest-p13-surface-polish` (base `0ca3bd0`, which includes plan-12's
INK identity + visual harness). PNGs are **not committed** (owner rule for
this batch): they live next to this README locally and in the PR
description. Regenerable any time with the command below.

## Matrix (plan-11 brief §17.0)

| Axis | Value |
|---|---|
| Browser | Chromium (Playwright, headless) |
| Viewports | desktop `1440x900`, mobile `375x812` |
| Reduced motion | one extra mobile pass (`prefers-reduced-motion: reduce`) |
| World | fresh Plan 10 browser world per pass (fixed accounts/state; deadlines derive from claim time) |
| Tool | `frontend/e2e/visual-capture.spec.ts` — extended in T0 with `student-task-community`, a scroll-anchor shot framing `.community-section` on the open task (`student-notifications` was already covered; the seeded open task carries no comments, so the anchor is the section — composer + empty state — not a literal `.comment-thread`) |
| Manifest | `manifest.json` next to this README (uncommitted; name / route / account / viewport / motion / world run per shot) |

World run ids: desktop `47fe5c13c900`, mobile `fc81434bc94c`,
mobile-reduced-motion `50b0611f0c51`.

Regenerate (from `frontend/`, ports clear of the owner stacks):

```bash
CQ_E2E=1 CQ_E2E_CAPTURE_DIR=../docs/evidence/plan13-before CQ_E2E_VIEWPORT=desktop \
CQ_E2E_BASE_URL=http://localhost:3200 CQ_E2E_API_URL=http://localhost:8200/api/v1 \
npx playwright test visual-capture.spec.ts
# repeat with CQ_E2E_VIEWPORT=mobile, then + CQ_E2E_REDUCED_MOTION=1
```

Surface shots for this workstream: `student-rewards`, `student-rankings`,
`student-task-community` (+ `student-task-detail` full page),
`student-notifications` — each in `desktop/`, `mobile/`,
`mobile-reduced-motion/`.

## Weakest-element list (input to T1–T4; max 8, each tied to a plan-11 P2 ruling)

1. **Rewards — equal-metric wallet.** 可用/累计/可花费 render as three
   identical numbers (100/100/100) in one wide shallow panel; nothing
   answers "还差多少能兑换什么". Violates P2: *one dominant spendable
   balance + next-reward progress; earned/frozen/debt quiet; no
   equal-metric grammar*. → T1.
2. **Rewards — cost is not the tile's first read.** "50 积分" sits in the
   same quiet metadata row as stock; a 可兑换 badge duplicates what the
   primary CTA already states; the tile has no object identity (no icon).
   Violates P2: *reward tiles with icon/cost/availability/CTA*. → T1.
3. **Rewards — orphan column.** One ~20rem card floats on a 1440px
   desktop; the grid never grows into the viewport. Violates the P2
   cross-page rule: *the content must use the viewport; no wall of equal
   cards — tiles are discrete objects sized by content*. → T1.
4. **Rankings — zero top-3 distinction.** The #1 row is grammatically
   identical to every other row (same weight, same keyline). Violates P2:
   *restrained top-3 distinction (no podium)*. → T2.
5. **Rankings — weak 我 anchor.** The current-user cue is a small 我 badge
   only, and 排行榜 / 我的附近 are two visually identical plain strips —
   the "where am I" story does not read. Violates P2: *strong 我 anchor;
   around-me reads as one continuous story*. → T2.
6. **Community — composer outweighs the thread.** The composer panel
   (identity radiogroup + textarea + primary button) is the section's
   dominant object while the thread below is a flat strip; on populated
   threads author/timestamp/reactions share one undifferentiated line.
   Violates the P2 cross-page rule: *what should the user notice first —
   the thread and its authors, not the input furniture*. → T3.
7. **Notifications — no unread hierarchy.** Rows are uniform; unread is
   not weighted by typography or surface (the thin e2e world seeds zero
   notifications, so this is evidenced from `NotificationsView` code, not
   a PNG — recorded honestly). Violates P2: *unread weight via
   typography + subtle surface tint; read = quiet*. → T4.
8. **Notifications — no event-type anchor.** Review result / deadline /
   redemption / system items carry no per-category glyph or keyline, so
   the list cannot be scanned by kind. Violates P2: *event-type visual
   anchor, restrained, text always present*. → T4.
