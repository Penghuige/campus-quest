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
 * committing. Not wired into release-gate/ci.yml yet: pixel stability
 * across runner environments is proven first (Phase C promotes it).
 *
 * Determinism discipline (per shot): real login through the app's own
 * forms (fixtures' ensureStudentLogin/staffLogin), wait for the page's
 * own header block plus the desktop sidebar landmark, 800ms settle —
 * the visual-capture harness's discipline. Screenshot options pin
 * fullPage + maxDiffPixelRatio 0.01; Playwright's toHaveScreenshot
 * defaults already disable animations (finite fast-forwarded, infinite
 * frozen at their initial state — the gallery skeleton's shimmer) and
 * hide the caret. The mask list covers the remaining clock/id-volatile
 * regions until the world grows a fixed clock:
 * - .deadline-line / .progress-note (the briefed relative-clock text);
 * - .claim-deadline (the dashboard claim-row "截止 …· 还剩 …" line —
 *   the same relative-clock text under a different existing class,
 *   DashboardView.tsx);
 * - .hero-line bearing 截止 (the dashboard HERO's deadline copy;
 *   .hero-line also carries the deterministic step strip, so the mask
 *   keys on the frozen "截止" copy, not the shared class);
 * - <time> elements (absolute timestamps: admin created_at, review
 *   submitted_at — same clock-volatile rationale);
 * - span.mono[title] (the admin user-id cells: run-unique UUIDs).
 */
import { type Page } from "@playwright/test";

import {
  BASE_URL,
  ensureStudentLogin,
  expect,
  staffLogin,
  test,
} from "./fixtures";

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

type ShotAuth = "anon" | "student" | "teacher" | "admin";

const SHOTS: Array<{ name: string; path: string; auth: ShotAuth }> = [
  { name: "auth-login", path: "/login", auth: "anon" },
  { name: "dev-gallery", path: "/dev/gallery", auth: "anon" },
  { name: "student-dashboard", path: "/", auth: "student" },
  { name: "student-tasks", path: "/tasks", auth: "student" },
  { name: "student-rankings", path: "/rankings", auth: "student" },
  { name: "student-rewards", path: "/rewards", auth: "student" },
  { name: "teacher-reviews", path: "/teacher/reviews", auth: "teacher" },
  { name: "admin-users", path: "/admin/users", auth: "admin" },
];

async function authenticate(page: Page, auth: ShotAuth): Promise<void> {
  switch (auth) {
    case "anon":
      return;
    case "student":
      return ensureStudentLogin(page);
    case "teacher": {
      test.skip(
        !(process.env.CQ_E2E_STAFF && process.env.CQ_E2E_STAFF_TOTP_SECRET),
        "world did not export the staff contract",
      );
      return staffLogin(
        page,
        process.env.CQ_E2E_STAFF!,
        process.env.CQ_E2E_STAFF_TOTP_SECRET!,
      );
    }
    case "admin": {
      test.skip(
        !(process.env.CQ_E2E_ADMIN && process.env.CQ_E2E_ADMIN_TOTP_SECRET),
        "world did not export the admin contract",
      );
      return staffLogin(
        page,
        process.env.CQ_E2E_ADMIN!,
        process.env.CQ_E2E_ADMIN_TOTP_SECRET!,
      );
    }
  }
}

for (const shot of SHOTS) {
  test(shot.name, async ({ page }) => {
    await authenticate(page, shot.auth);
    await page.goto(`${BASE_URL}${shot.path}`);
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
    await expect(page).toHaveScreenshot(`${shot.name}.png`, {
      fullPage: true,
      maxDiffPixelRatio: 0.01,
      mask: [
        page.locator(".deadline-line"),
        page.locator(".progress-note"),
        page.locator(".claim-deadline"),
        page.locator(".hero-line", { hasText: "截止" }),
        page.locator("time"),
        page.locator("span.mono[title]"),
      ],
    });
  });
}
