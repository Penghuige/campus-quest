# Plan 11 shell-after evidence — Task 3 (§17.0 matrix)

Captured after the Task 3 navigation shell landed (commit chain on
`design/visual-refresh-v1`), with the same matrix as the baseline
(`docs/evidence/plan11-baseline/`): Chromium; desktop `1440x900`,
mobile `375x812`, mobile + reduced-motion; fresh seeded world per
pass; per-shot manifest in the capture dir.

Committed set: the five redesign-target screens per pass. The full
19-screen set regenerates with the documented command.

## What changed versus baseline (pixel-verified)

- desktop: the quiet sidebar renders as grid column 1 (232px +
  border-right at x=231; sidebar surface `color-mix(background 55%,
  surface-1)`), the top bar degrades to the context/actions strip
  (no horizontal nav pills spanning the bar), content lives in
  column 2;
- mobile: fixed 5-slot bottom nav (`display: grid`, `position:
  fixed`), content reserves `4rem + safe-area + 16px` bottom padding;
- staff shells share the sidebar geometry; narrow staff gets the
  hamburger menu sheet (native dialog focus trap + Escape).

DOM ground truth (probe run): `.app-shell` children =
`[aside.app-sidebar, div.app-body, nav.app-bottomnav]`;
`getComputedStyle(.app-shell).display === "grid"` with
`232px 1208px` columns at 1440px; `.app-sidebar` flex; bottom nav
grid/fixed at 375px.

## Evidence-integrity incident (and the hardening it bought)

The FIRST after-shell capture produced screenshots whose top strip a
vision-model read as the old layout. Pixel forensics settled it: the
232px border, the sidebar surface color, and the left-cluster
positions (sidebar brand at x≈21-129; bell + nickname at the content
column start) all match the NEW shell — the vision read of a 70px
crop was wrong, but the scare exposed a real gap: a stale/leaked dev
server could have produced wrong-artifact evidence silently.

Hardening (committed): `visual-capture.spec.ts` now asserts, before
every authenticated shot, that THIS pass's navigation landmark is
visible (`.app-sidebar` on desktop; `.app-bottomnav`/`.app-menubtn`
on narrow) — a wrong build fails loudly instead of becoming
evidence. Lesson recorded for Task 12's fold-back: trust pixels and
DOM probes over vision-model reads for layout verdicts.

Run ids: desktop `6797284b89dc`/`fb2128bcc85a`, mobile
`882e75ca5510`/`af665bc2ad76`, rm `3c4b7e46ae8f`/`fbece7b8513e`
(two worlds per pass: the auth screens were re-captured after a
guard-parameter fix re-ran the spec).

## Behavior gates already green at this batch

- full Playwright at default ports (frontend 3000, MinIO-CORS
  legal): **52 passed / 0 failed**, teacher/admin zero-skip asserted;
- typecheck / lint / unit 468-0.

Remaining for the milestone: fresh `make release-gate` (runs with
`CQ_E2E_API_URL` pointed at a free backend port — the frontend stays
3000 for CORS; see the port rule in the baseline README).
