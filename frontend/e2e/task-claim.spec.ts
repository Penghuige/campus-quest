/**
 * CampusQuest task-claim e2e — Plan 09 Task 3 (task discovery + claim).
 *
 * STATUS: SPEC ONLY — NOT WIRED TO A RUNNER YET.
 *
 * Same guard pattern as `e2e/auth.spec.ts`: Playwright itself is installed
 * by Plan 10 (no `@playwright/test` dependency and no `test:e2e` script
 * yet). Until then this file stays invisible to the gates:
 * - `tsconfig.json` includes only `src/**` + `.next/**`, so `tsc` skips it;
 * - `eslint.config.mjs` lists `e2e/**` in globalIgnores for the same reason;
 * - `next build` never touches files outside `src/app`.
 * Once Plan 10 installs Playwright, remove the eslint ignore, add the
 * `test:e2e` script, and run with `CQ_E2E=1` — every test below is skipped
 * unless that flag is set, so importing the file can never depend on a
 * live backend during ordinary development.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1               enable the suite (required);
 * - CQ_E2E_BASE_URL        frontend origin (default http://localhost:3000);
 * - CQ_E2E_LOGIN_URL       login page (default $CQ_E2E_BASE_URL/login);
 * - CQ_E2E_TASK_URL        task DETAIL deep link to a published task with
 *                          at least one AVAILABLE assignment (required for
 *                          the claim-flow tests; Plan 10's fixture prepares
 *                          it — until then those tests skip);
 * - CQ_E2E_EMPTY_TASK_URL  task detail deep link whose assignments are all
 *                          taken (optional; drives the conflict-copy test);
 * - CQ_E2E_STUDENT         pre-seeded student credentials
 *                          "student-number:password" (required with
 *                          CQ_E2E_TASK_URL; the fixture seeds the account).
 *
 * Privacy pins under test (spec §42): the claim surface never renders a
 * selectable Assignment list or an assignment_id input — the ONLY
 * platform/keyword the student ever sees is the one the server allocated
 * to their own claim, and it appears only AFTER the claim succeeds.
 */
import { ensureStudentLogin, expect, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const TASK_URL = process.env.CQ_E2E_TASK_URL;
const EMPTY_TASK_URL = process.env.CQ_E2E_EMPTY_TASK_URL;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240001:correct-horse"

test.skip(!E2E_ENABLED, "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* URLs) to run this suite.");

const claimFlowReady = TASK_URL !== undefined && STUDENT !== undefined;
test.skip(
  !claimFlowReady,
  "claim flow needs CQ_E2E_TASK_URL (a published task with AVAILABLE assignments) and CQ_E2E_STUDENT (seeded credentials); Plan 10's fixture provides both.",
);

/** Open the shared student's session through the suite's resume chain
 * (the backend's auth:login window — 10 form attempts / 5 min per
 * username — is a real bound a whole-suite run trips; the login UX
 * itself stays owned by auth.spec). */
async function loginAsStudent(page: import("@playwright/test").Page): Promise<void> {
  await ensureStudentLogin(page);
}

test.describe("student task claim", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsStudent(page);
  });

  test("detail page hides assignment payloads before claim", async ({ page }) => {
    await page.goto(TASK_URL!);

    // Spec §42: cards/details carry counts, never an assignment list.
    // exact: the definition TERM is "可领取" and its value "可领取 N 个"
    // also substring-matches a bare getByText.
    await expect(page.getByText("可领取", { exact: true })).toBeVisible();
    await expect(page.locator("[data-assignments-list]")).toHaveCount(0);
    await expect(page.locator("input[name='assignment_id']")).toHaveCount(0);
    // The assigned platform/keyword panel is absent pre-allocation.
    await expect(page.locator(".claim-panel")).toHaveCount(0);
  });

  test.describe("mobile 375x812", () => {
    test.use({ viewport: { width: 375, height: 812 } });

    test("claim CTA is visible without scroll-hunt", async ({ page }) => {
      // Runs BEFORE the claiming test below: once THIS student holds a
      // claim on the task, the same-task rule replaces the CTA with
      // the claim panel (the world's task carries only one claim slot
      // per student).
      await page.goto(TASK_URL!);

      const cta = page.getByRole("button", { name: "领取任务" });
      await expect(cta).toBeVisible();
      await expect(cta).toBeInViewport();
      // Single-column card grid on the narrow workload class.
      await page.goto(`${BASE_URL}/tasks`);
      const firstCard = page.locator(".task-card").first();
      await expect(firstCard).toBeVisible();
    });
  });

  test("claim allocates server-side and reveals only the user's assignment", async ({ page }) => {
    await page.goto(TASK_URL!);

    await page.getByRole("button", { name: "领取任务" }).click();

    // The after-allocation panel appears (role=status announces success).
    const panel = page.locator(".claim-panel");
    await expect(panel).toBeVisible();
    // Exactly ONE platform + ONE keyword — the allocated pair, never a list.
    await expect(panel.getByText("平台", { exact: true })).toHaveCount(1);
    await expect(panel.getByText("关键词", { exact: true })).toHaveCount(1);
    // Still no assignment list or id input anywhere on the page.
    await expect(page.locator("[data-assignments-list]")).toHaveCount(0);
    await expect(page.locator("input[name='assignment_id']")).toHaveCount(0);
    // Server-authoritative deadline copy rides the panel (UX-only
    // display). The panel carries TWO 截止 texts (deadline line + the
    // grace note), so the pin targets the deadline line itself.
    await expect(panel.locator(".deadline-line")).toBeVisible();
  });

  test("conflict shows typed copy and stays retry-friendly", async ({ page }) => {
    test.skip(EMPTY_TASK_URL === undefined, "needs CQ_E2E_EMPTY_TASK_URL (all assignments taken)");
    await page.goto(EMPTY_TASK_URL!);

    await page.getByRole("button", { name: "领取任务" }).click();

    // Typed NO_ASSIGNMENT_AVAILABLE copy, not a generic crash.
    await expect(page.getByRole("alert")).toContainText("当前没有可领取的任务单元");
    // Retry-friendly: the button re-enables so another attempt is possible.
    await expect(page.getByRole("button", { name: "领取任务" })).toBeEnabled();
  });
});
