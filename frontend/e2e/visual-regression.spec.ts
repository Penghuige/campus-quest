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
 * hide the caret.
 *
 * Masks — how they actually work here (fix-round-1 correction; the
 * earlier "masks apply at comparison only" note was wrong):
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
import { type Locator, type Page } from "@playwright/test";

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

/** One masked volatile region. Every declared mask MUST engage on its
 * shot — the test asserts count > 0 before capturing, so a mask that a
 * UI change silently detaches (rename, removal) fails loudly instead of
 * rotting into a no-op. */
interface ShotMask {
  /** Stable id used in assertion messages. */
  id: string;
  /** Why this region is volatile (what clock/id fact it hides). */
  why: string;
  locate: (page: Page) => Locator;
}

interface Shot {
  name: string;
  auth: ShotAuth;
  masks: ShotMask[];
  /** Static path (matrix routes)… */
  path?: string;
  /** …or a world-export env carrying a seeded deep link (claim/task
   * detail): the shot skips when the export is absent, mirroring
   * visual-capture.spec.ts's conditional-capture discipline. These two
   * shots are deliberately NOT in scripts/assert-e2e-no-skips.mjs's
   * watched list — the skip is legal. */
  envPath?: string;
  prepare?: (page: Page) => Promise<void>;
}

const SHOTS: Shot[] = [
  { name: "admin-innovation-operations", path: "/admin/innovation-operations", auth: "admin", masks: [] },
  { name: "auth-login", path: "/login", auth: "anon", masks: [] },
  { name: "dev-gallery", path: "/dev/gallery", auth: "anon", masks: [] },
  {
    name: "student-dashboard",
    path: "/",
    auth: "student",
    masks: [
      {
        id: ".claim-deadline",
        why: "claim-row 截止…· 还剩… — relative-clock text (DashboardView.tsx)",
        locate: (page) => page.locator(".claim-deadline"),
      },
      {
        id: ".hero-line~截止",
        why: "hero deadline — the same relative-clock copy; .hero-line is shared with the deterministic step rail, so the mask keys on the frozen 截止 copy (DashboardView.tsx)",
        locate: (page) => page.locator(".hero-line", { hasText: "截止" }),
      },
      // Deliberately NOT masked: the reward-progress .progress-note
      // ("（50 积分）现在就可以兑换") is deterministic under fixed labels.
    ],
  },
  { name: "student-tasks", path: "/tasks", auth: "student", masks: [] },
  {
    name: "student-task-detail",
    envPath: "CQ_E2E_TASK_OPEN_PATH",
    auth: "student",
    // Probe proof (follow-up run): the page's only candidate, its
    // .deadline-line, renders "领取后 4320 分钟内提交" — derived from the
    // seeded task's fixed RELATIVE duration, deterministic across runs.
    masks: [],
  },
  {
    name: "student-claim",
    envPath: "CQ_E2E_CLAIM_PATH",
    auth: "student",
    masks: [
      {
        id: ".assignment-item~领取时间 .assignment-value",
        why: "领取时间 = claimed_at — absolute clock (ClaimDetailView.tsx)",
        locate: (page) =>
          page
            .locator(".assignment-item", { hasText: "领取时间" })
            .locator(".assignment-value"),
      },
      {
        id: ".deadline-line",
        why: "截止…· 还剩… — relative-clock countdown (ClaimDetailView.tsx)",
        locate: (page) => page.locator(".deadline-line"),
      },
      {
        id: ".progress-note",
        why: "grace note 超过截止时间后至 <grace> 仍可提交 — absolute clock (ClaimDetailView.tsx)",
        locate: (page) => page.locator(".progress-note"),
      },
    ],
  },
  { name: "student-rankings", path: "/rankings", auth: "student", masks: [] },
  { name: "student-rewards", path: "/rewards", auth: "student", masks: [] },
  {
    name: "student-owner-profile-form",
    path: "/profile/owner-profile",
    auth: "student",
    masks: [],
    prepare: async (page) => {
      await expect(page.getByRole("region", { name: "负责人资料编辑器" })).toBeVisible();
    },
  },
  {
    name: "student-project-draft-form",
    path: "/profile/project-drafts",
    auth: "student",
    masks: [],
    prepare: async (page) => {
      await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
      await expect(page.getByRole("region", { name: "项目草稿编辑器" })).toBeVisible();
    },
  },
  {
    name: "student-achievement-draft-form",
    path: "/profile/project-drafts",
    auth: "student",
    masks: [],
    prepare: async (page) => {
      await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
      await page.getByLabel("项目名称", { exact: true }).fill("成果草稿示例项目");
      await page.getByRole("button", { name: "保存草稿", exact: true }).click();
      await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
      await page.getByRole("link", { name: "管理成果草稿", exact: true }).click();
      await page.getByRole("button", { name: "新建成果草稿", exact: true }).click();
      await expect(page.getByRole("region", { name: "成果草稿编辑器" })).toBeVisible();
    },
  },
  {
    name: "student-notifications",
    path: "/notifications",
    auth: "student",
    // Plan-13 T4: masks deliberately EMPTY — the thin world seeds zero
    // notifications, so the deterministic empty state is the captured
    // truth and no clock-volatile element exists to mask (see the header
    // probe note; the engagement contract makes an inert .notif-time
    // mask fail loud, so none is declared).
    masks: [],
  },
  {
    name: "teacher-reviews",
    path: "/teacher/reviews",
    auth: "teacher",
    masks: [
      {
        id: "time",
        why: "queue-row submitted_at — absolute clock (SubmissionReview.tsx)",
        locate: (page) => page.locator("time"),
      },
    ],
  },
  {
    name: "admin-users",
    path: "/admin/users",
    auth: "admin",
    masks: [
      {
        id: "time",
        why: "row created_at — absolute clock (AdminUserAccounts.tsx)",
        locate: (page) => page.locator("time"),
      },
      {
        id: "span.mono[title]",
        why: "run-unique user UUIDs (the username .mono cell carries no title attr, so this selects only the id cells)",
        locate: (page) => page.locator("span.mono[title]"),
      },
    ],
  },
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
    const path =
      shot.envPath === undefined ? shot.path : process.env[shot.envPath];
    test.skip(
      path === undefined || path === "",
      `world did not export ${shot.envPath ?? "path"}`,
    );
    await authenticate(page, shot.auth);
    await page.goto(`${BASE_URL}${path}`);
    await shot.prepare?.(page);
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
