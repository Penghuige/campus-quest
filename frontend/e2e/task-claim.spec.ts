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
 * - CQ_E2E_BASE_URL        frontend origin (default https://localhost:3000);
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
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const TASK_URL = process.env.CQ_E2E_TASK_URL;
const EMPTY_TASK_URL = process.env.CQ_E2E_EMPTY_TASK_URL;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240001:correct-horse"

test.skip(!E2E_ENABLED, "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* URLs) to run this suite.");

/** Scoped to the claim-flow describe ONLY (r4 M2): the defect-#5 card
 * tests below need just CQ_E2E_STUDENT + the /tasks square, so a
 * module-level skip here would silently unwatch them under the legal
 * "CQ_E2E=1 + STUDENT without TASK_URL" configuration. */
const claimFlowReady = TASK_URL !== undefined && STUDENT !== undefined;

/** Open the shared student's session through the suite's resume chain
 * (the backend's auth:login window — 10 form attempts / 5 min per
 * username — is a real bound a whole-suite run trips; the login UX
 * itself stays owned by auth.spec). */
async function loginAsStudent(page: import("@playwright/test").Page): Promise<void> {
  await ensureStudentLogin(page);
}

test.describe("student task claim", () => {
  test.skip(
    !claimFlowReady,
    "claim flow needs CQ_E2E_TASK_URL (a published task with AVAILABLE assignments) and CQ_E2E_STUDENT (the seeded account); Plan 10's fixture provides both.",
  );
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

/* --- Defect #5 (QA 2026-09-30): task-card affordances on the square ---------- */

test.describe("task card affordances (defect #5)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsStudent(page);
    await page.goto(`${BASE_URL}/tasks`);
    await expect(page.locator(".task-card").first()).toBeVisible();
  });

  test("every card carries its tier: data-rarity + shaped badge glyph + label", async ({ page }) => {
    const cards = page.locator(".task-card");
    const count = await cards.count();
    for (let i = 0; i < count; i += 1) {
      const card = cards.nth(i);
      const rarity = await card.getAttribute("data-rarity");
      // rarityView's canonical keys only (unknown values normalize).
      expect(["NORMAL", "RARE", "EPIC", "LEGENDARY"]).toContain(rarity);
      const badge = card.locator(".task-card-rarity");
      await expect(badge).toHaveAttribute("data-rarity", rarity!);
      // The shape glyph rides inside the badge next to the text label.
      await expect(badge.locator("svg")).toHaveCount(1);
      await expect(badge).toContainText(
        rarity === "NORMAL" ? "普通"
          : rarity === "RARE" ? "稀有"
          : rarity === "EPIC" ? "史诗"
          : "传说",
      );
    }
  });

  test("the title reads as a link at rest (quiet underline, defect #5.3)", async ({ page }) => {
    const title = page.locator(".task-card .task-card-title a").first();
    await expect(title).toBeVisible();
    await expect(title).toHaveCSS("text-decoration-line", "underline");
    // And it still points at the task's own detail route.
    const href = await title.getAttribute("href");
    expect(href).toMatch(/^\/tasks\/[0-9a-f-]{36}$/);
  });

  test("focusing the title outlines the whole card in primary (keyboard selection)", async ({ page }) => {
    const card = page.locator(".task-card").first();
    const title = card.locator(".task-card-title a");
    // S1 (r4): settle the resting border first (entrance transitions
    // are done long before, but be explicit), then read the token's
    // COMPUTED value through a probe element — comparing serialized
    // colors avoids string-matching the oklch token text.
    const before = await card.evaluate((node) => getComputedStyle(node).borderColor);
    const primary = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.style.color = "var(--primary)";
      document.body.appendChild(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return color;
    });
    expect(primary).not.toBe(before);
    // A programmatic focus() alone does not light :focus-visible —
    // first establish the KEYBOARD interaction modality (one real Tab),
    // then move focus; Chromium's heuristic then matches the selector.
    await page.keyboard.press("Tab");
    await title.focus();
    // The card border TRANSITIONS (140ms) — poll for the settled
    // value, then demand EQUALITY with --primary: :focus-within alone
    // (the pre-existing rule) only reaches border-strong, so a deleted
    // focus-visible rule cannot satisfy this assertion.
    await expect
      .poll(() => card.evaluate((node) => getComputedStyle(node).borderColor))
      .toBe(primary);
  });

  test("the rarity glyph fills and centers its 20-unit viewBox", async ({ page }) => {
    // Codex P2 regression pin: the shapes must be drawn around the
    // StrokeIcon (10, 10) center, not a corner of the 0 0 20 20 box.
    const shape = page.locator(".task-card-rarity svg path, .task-card-rarity svg circle").first();
    const box = await shape.evaluate((el) => {
      const bbox = (el as SVGGraphicsElement).getBBox();
      return { cx: bbox.x + bbox.width / 2, cy: bbox.y + bbox.height / 2, w: bbox.width, h: bbox.height };
    });
    expect(box.w).toBeGreaterThanOrEqual(10);
    expect(box.h).toBeGreaterThanOrEqual(10);
    expect(box.cx).toBeGreaterThan(8.5);
    expect(box.cx).toBeLessThan(11.5);
    expect(box.cy).toBeGreaterThan(8.5);
    expect(box.cy).toBeLessThan(11.5);
  });

  test("a fully-claimed task stays browsable and carries the depleted badge (defect #20)", async ({ page }) => {
    // The world seeds task_r with ONE assignment already claimed by
    // the redeemer — the square's deterministic depleted card. The
    // document-family task F joins it once the §10.1 e2e (earlier in
    // the battery) claims both its slots — so assert on the FIRST
    // depleted card's shape, never an exact square-wide count.
    const depleted = page.locator(".task-card", { hasText: "已被领完" }).first();
    await expect(depleted).toBeVisible();
    // The MARK is a real text badge (never a color-only cue), and the
    // meta row keeps its numeric shape — the phrase rides the badge
    // exactly once per card.
    await expect(depleted.locator(".badge", { hasText: "已被领完" })).toBeVisible();
    await expect(depleted.getByText("可领取 0 个")).toBeVisible();
    // Browsable means the title link still navigates to the detail.
    const href = await depleted.locator(".task-card-title a").getAttribute("href");
    expect(href).toMatch(/^\/tasks\/[0-9a-f-]{36}$/);
    // Batch ④ (2026-10-10): the recede treatment rides the same
    // verdict — the marker attribute plus the non-color cues (dashed
    // border, dimmed content). The badge keeps full contrast.
    await expect(depleted).toHaveAttribute("data-depleted", "true");
    await expect(depleted).toHaveCSS("border-style", "dashed");
    const contentOpacity = await depleted
      .locator(".task-card-meta")
      .evaluate((el) => getComputedStyle(el).opacity);
    expect(Number.parseFloat(contentOpacity)).toBeCloseTo(0.62, 2);
  });

  test("back-navigation renders the cached square without a skeleton pass (batch ①)", async ({ page }) => {
    // The flicker root cause: the square island refetched from zero on
    // every mount — a task-detail round trip re-entered through a
    // skeleton flash. With the first page on the shared data cache, a
    // remount inside the fresh window renders the cached snapshot
    // with NO skeleton pass and NO refetch.
    const taskRequests: number[] = [];
    page.on("request", (req) => {
      if (new URL(req.url()).pathname === "/api/v1/tasks") {
        taskRequests.push(Date.now());
      }
    });

    await page.goto(`${BASE_URL}/tasks`);
    await expect(page.locator(".task-card").first()).toBeVisible();
    const requestsAfterLoad = taskRequests.length;

    // Watch for any skeleton recurrence during the round trip.
    await page.evaluate(() => {
      (window as unknown as { __sawSkeleton?: boolean }).__sawSkeleton = false;
      const look = () => {
        if (document.querySelector(".skeleton-cards") !== null) {
          (window as unknown as { __sawSkeleton?: boolean }).__sawSkeleton = true;
        }
      };
      look();
      new MutationObserver(look).observe(document.body, {
        childList: true,
        subtree: true,
      });
    });

    await page.locator(".task-card-title a").first().click();
    await page.waitForURL(/\/tasks\/[0-9a-f-]{36}/);
    await page.goBack();
    await expect(page.locator(".task-card").first()).toBeVisible();
    // Cards are back WITHOUT a skeleton pass…
    const sawSkeleton = await page.evaluate(
      () => (window as unknown as { __sawSkeleton?: boolean }).__sawSkeleton,
    );
    expect(sawSkeleton).toBe(false);
    // …and without a refetch (the fresh cache window serves read-only).
    expect(taskRequests.length).toBe(requestsAfterLoad);
  });

  test("every card takes the orchestrated entrance, bounded in total (owner ruling 2026-10-10)", async ({ page }) => {
    // Batch ②: the old choreography animated only the first three
    // children — every later card (and every load-more append) popped
    // in with no entrance. The ruling: ALL cards enter, and the grid
    // stays fast at any size (§11's ~300ms ceiling).
    const cards = page.locator(".task-grid > li");
    const count = await cards.count();
    expect(count).toBeGreaterThanOrEqual(4);
    let previousDelay = -1;
    for (let i = 0; i < count; i += 1) {
      const { name, delay } = await cards.nth(i).evaluate((el) => ({
        name: getComputedStyle(el).animationName,
        delay: getComputedStyle(el).animationDelay,
      }));
      // EVERY card rises — not just the first three.
      expect(name).toBe("cq-rise");
      const ms = Number.parseFloat(delay);
      // Delays increase monotonically (the stagger is ordered)…
      expect(ms).toBeGreaterThanOrEqual(previousDelay);
      previousDelay = ms;
    }
    // …and the whole grid settles inside the §11 budget: last delay +
    // --motion-in 220ms ≤ ~320ms (small float slack).
    expect(previousDelay + 220).toBeLessThanOrEqual(320);
  });

  test("cards in the same grid row share one height (batch ③)", async ({ page }) => {
    // Owner ruling: rows are uniform — the card fills its stretched
    // li (heights were content-ragged: 1-line vs 2-line titles, badge
    // presence). Group by row via bounding-rect top and compare.
    const rows = await page.evaluate(() => {
      const byRow = new Map<number, number[]>();
      for (const card of document.querySelectorAll<HTMLElement>(".task-card")) {
        const rect = card.getBoundingClientRect();
        const top = Math.round(rect.top);
        const heights = byRow.get(top) ?? [];
        heights.push(Math.round(rect.height));
        byRow.set(top, heights);
      }
      return [...byRow.values()];
    });
    expect(rows.length).toBeGreaterThanOrEqual(1);
    for (const row of rows) {
      const tallest = Math.max(...row);
      for (const height of row) {
        expect(Math.abs(height - tallest)).toBeLessThanOrEqual(1);
      }
    }
  });
});
