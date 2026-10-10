/**
 * Pixel visual regression — plan-12 task 9: screenshot baselines for the
 * §17.0 fixed matrix plus the task-7 component gallery.
 *
 * Runs ONLY with CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1 (the
 * Makefile's `visual-regression` target sets all three):
 * - CQ_E2E: the suite-wide guard (the config orchestrates both dev
 *   servers and seeds the browser world in globalSetup);
 * - CQ_VISUAL: this suite's own opt-in — ordinary `npm run test:e2e`
 *   runs (including the release gate) collect these tests as skipped
 *   and never depend on pixel stability;
 * - CQ_E2E_FIXED_LABELS: the task-8 world-label freeze. It must reach
 *   the SEED (globalSetup spreads process.env into the browser_world
 *   call), so the guard here fails loudly instead of diffing a
 *   run-id-labelled world against the fixed-label baselines.
 *
 * Baselines (e2e/visual-regression.spec.ts-snapshots/, committed) are
 * LINUX-AUTHORITATIVE: they were generated on Linux Chromium and are
 * reviewed like code — every PNG eyeballed before commit. macOS/local
 * font rasterization deltas are expected; regenerate deliberately with
 * `CQ_VISUAL_UPDATE=1 npm run test:e2e:visual` (the npm script wires the
 * flag to --update-snapshots) and re-review every changed PNG before
 * committing. (Wired into both the CI visual job and `make release-gate`.)
 *
 * Determinism discipline (per shot): real login through the app's own
 * forms (fixtures' ensureStudentLogin/staffLogin), wait for the page's
 * own header block plus the desktop sidebar landmark, 800ms settle —
 * the visual-capture harness's discipline. Screenshot options pin
 * fullPage + maxDiffPixelRatio 0.01; Playwright's toHaveScreenshot
 * defaults already disable animations (finite fast-forwarded, infinite
 * frozen at their initial state — the gallery skeleton's shimmer) and
 * hide the caret.
 *
 * Masks — how they actually work here:
 * - a mask paints an opaque box over the matched element's box AT
 *   CAPTURE TIME — the produced PNG (baseline write or actual) carries
 *   the box, which is why the stored baselines show pink strips;
 * - `--update-snapshots` rewrites a baseline only when the comparison
 *   mismatches, so adding a mask later does NOT regenerate an existing
 *   baseline — the stale file must be deleted and regenerated (that is
 *   how the hero-deadline box initially went missing from
 *   student-dashboard-linux.png while comparisons still passed);
 * - a mask whose locator matches NOTHING is a silent no-op, so every
 *   declared mask carries an engagement assertion (count > 0, hard
 *   fail) — an inert mask can never pass unnoticed again.
 *
 * Masks are scoped PER SHOT: a shot lists only the volatile regions its
 * own page renders (deterministic content stays visible and asserted —
 * e.g. the dashboard's reward-progress note is fixed under fixed labels
 * and must NOT be masked). The per-shot truth table below was measured
 * by a live probe (fix-round-1, probe counts at capture state):
 *   .deadline-line / .progress-note / .claim-deadline / .hero-line~截止 /
 *   time / span.mono[title] match ONLY on:
 *   student-dashboard (.progress-note=1 deterministic, .claim-deadline=1,
 *   .hero-line~截止=1), teacher-reviews (time=1: submitted_at),
 *   admin-users (time=10: created_at; span.mono[title]=10: user UUIDs).
 * Follow-up probe (claim + task-detail shots, same discipline):
 *   student-claim — .deadline-line=1 (volatile countdown), .progress-note=1
 *   (volatile grace timestamp), .assignment-item~领取时间 .assignment-value=1
 *   (claimed_at); student-task-detail — .deadline-line=1 but its text is
 *   "领取后 4320 分钟内提交" (derived from the seeded task's FIXED RELATIVE
 *   duration → deterministic, deliberately unmasked); all other candidates 0.
 * Plan-13 T4 probe (student-notifications): the thin world seeds ZERO
 *   notifications (backend tests/e2e/browser_world.py grows no
 *   notification rows), so /notifications renders the deterministic
 *   empty state — .notif-item/.notif-time count = 0 at capture state,
 *   and a timestamp mask would be an inert no-op (forbidden by the
 *   engagement contract below). The shot therefore declares NO masks;
 *   populated rows with timestamps are pixel-pinned through the
 *   dev-gallery fixture block, whose fixture timestamps are FIXED
 *   (deterministic, mask-free). If world seeding ever grows a
 *   notification, this shot MUST gain the .notif-time mask.
 */


import { BASE_URL, expect, test } from "./fixtures";
import { authenticate, SHOTS } from "./shot-surfaces";

const ENABLED =
  process.env.CQ_E2E === "1" &&
  process.env.CQ_VISUAL === "1" &&
  process.env.CQ_E2E_FIXED_LABELS === "1";

test.skip(
  !ENABLED,
  "pixel regression is opt-in: make visual-regression " +
    "(CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1)",
);

// One desktop viewport for the whole matrix (the §17.0 desktop pass,
// 1440x900); mobile evidence stays with the capture harness.
test.use({ viewport: { width: 1440, height: 900 } });

export type ShotAuth = "anon" | "student" | "teacher" | "admin";

/** One masked volatile region. Every declared mask MUST engage on its
 * shot — the test asserts count > 0 before capturing, so a mask that a
 * UI change silently detaches (rename, removal) fails loudly instead of
 * rotting into a no-op. */

for (const shot of SHOTS) {
  test(shot.name, async ({ page }) => {
    const path =
      shot.envPath === undefined ? shot.path : process.env[shot.envPath];
    test.skip(
      path === undefined || path === "",
      `world did not export ${shot.envPath ?? "path"}`,
    );
    await authenticate(page, shot.auth);
    await page.goto(`${BASE_URL}${path}`);
    // Font determinism (C3 ⑧, CI lessons 1+2): (a) the local dev box
    // resolves zh sans to Microsoft YaHei (msyh.ttf) while CI falls to
    // the pinned fonts-noto-cjk — different metrics, 1-2px page-height
    // drift. (b) the `.mono` stack opens with ui-monospace, which
    // resolves to Noto Sans Mono locally but DejaVu Sans Mono on the
    // runner — deterministic per-glyph deltas on digit-dense surfaces
    // (the admin table's username column). Pin BOTH stacks to families
    // both environments carry (Liberation Mono ships with every Ubuntu
    // image) so baselines compare identical rendering everywhere.
    await page.addStyleTag({
      content: `:root { --font-sans: "Noto Sans CJK SC", sans-serif; }
        .mono { font-family: "Liberation Mono", monospace !important; }`,
    });
    // The capture harness's wrong-artifact guard: the shot is only valid
    // if THIS pass rendered the page's own header and (for authenticated
    // surfaces) the desktop sidebar landmark.
    await expect(page.locator(".page-head, main").first()).toBeVisible({
      timeout: 15_000,
    });
    if (shot.auth !== "anon") {
      await expect(page.locator(".app-sidebar").first()).toBeVisible({
        timeout: 5_000,
      });
    }
    await page.waitForTimeout(800);
    // Engagement contract: every declared mask must match at least one
    // element on THIS shot, at capture state. A zero-count mask is a
    // silent no-op (nothing painted, nothing protected) — fail hard.
    const masks: Locator[] = [];
    for (const mask of shot.masks) {
      const locator = mask.locate(page);
      const count = await locator.count();
      expect(
        count,
        `mask "${mask.id}" on ${shot.name} must engage (${mask.why}); got 0 elements`,
      ).toBeGreaterThan(0);
      masks.push(locator);
    }
    await expect(page).toHaveScreenshot(`${shot.name}.png`, {
      fullPage: true,
      maxDiffPixelRatio: 0.01,
      mask: masks,
    });
  });
}
