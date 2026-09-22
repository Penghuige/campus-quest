/**
 * CampusQuest rewards + rankings e2e — Plan 09 Task 5.
 *
 * STATUS: SPEC ONLY — NOT WIRED TO A RUNNER YET.
 *
 * Same guard pattern as `e2e/auth.spec.ts` / `e2e/task-claim.spec.ts`:
 * Playwright itself is installed by Plan 10 (no `@playwright/test`
 * dependency and no `test:e2e` script yet). Until then this file stays
 * invisible to the gates (tsconfig includes only `src/**`, eslint
 * globalIgnores lists `e2e/**`, `next build` never leaves `src/app`).
 * Once Plan 10 installs Playwright, remove the eslint ignore, add the
 * `test:e2e` script, and run with `CQ_E2E=1`.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1                 enable the suite (required);
 * - CQ_E2E_BASE_URL          frontend origin (default http://localhost:3000);
 * - CQ_E2E_LOGIN_URL         login page (default $CQ_E2E_BASE_URL/login);
 * - CQ_E2E_STUDENT           pre-seeded student credentials
 *                            "student-number:password" with ENOUGH
 *                            spendable points for the cheapest reward
 *                            (required for the redemption flows; the
 *                            fixture seeds the account and a reward);
 * - CQ_E2E_POOR_STUDENT      seeded student with LESS spendable points
 *                            than the cheapest open reward (optional;
 *                            drives the INSUFFICIENT_POINTS conflict
 *                            test);
 * - CQ_E2E_PRIVATE_STRINGS   comma-separated seeded identity values
 *                            (student numbers, phones, emails of OTHER
 *                            seeded users that appear on the boards)
 *                            driving the ranking-privacy assertions;
 *                            when unset only the structural privacy
 *                            pins run (no user-id attributes, no
 *                            6+ digit runs in the rendered boards).
 *
 * Privacy pins under test (spec §17/§40): the ranking surfaces render
 * EXACTLY nickname / display_honor / score / rank — never a student
 * number, phone, email, or user id.
 */
import { expect, test } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const LOGIN_URL = process.env.CQ_E2E_LOGIN_URL ?? `${BASE_URL}/login`;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240001:correct-horse"
const POOR_STUDENT = process.env.CQ_E2E_POOR_STUDENT;
const PRIVATE_STRINGS = (process.env.CQ_E2E_PRIVATE_STRINGS ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter((value) => value.length > 0);

test.skip(
  !E2E_ENABLED,
  "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* fixtures) to run this suite.",
);

const redeemReady = STUDENT !== undefined;
test.skip(
  !redeemReady,
  "redemption flow needs CQ_E2E_STUDENT (seeded student with spendable points and a stocked reward); Plan 10's fixture provides both.",
);

/** Log in through the student login page (T2 flow). */
async function loginAs(
  page: import("@playwright/test").Page,
  credentials: string,
): Promise<void> {
  const [username, password] = credentials.split(":");
  await page.goto(LOGIN_URL);
  await page.getByLabel("学号").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
}

test.describe("student rewards redemption", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, STUDENT!);
  });

  test("wallet strip shows available / earned / spendable", async ({ page }) => {
    await page.goto(`${BASE_URL}/rewards`);

    await expect(page.getByLabel("积分余额")).toBeVisible();
    await expect(page.getByText("可用积分")).toBeVisible();
    await expect(page.getByText("累计获得")).toBeVisible();
    await expect(page.getByText("可花费")).toBeVisible();
  });

  test("redeem flow: confirm dialog -> pending state -> frozen spendability", async ({
    page,
  }) => {
    await page.goto(`${BASE_URL}/rewards`);

    // Open the first redeemable card's dialog (server window/stock
    // verdicts drive which cards stay enabled).
    const confirmButton = page
      .locator(".reward-card", {
        has: page.getByRole("button", { name: "兑换", exact: true }),
      })
      .first()
      .getByRole("button", { name: "兑换", exact: true });
    await expect(confirmButton).toBeEnabled();
    await confirmButton.click();

    const dialog = page.locator("dialog.dialog");
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole("heading", { name: "确认兑换" }),
    ).toBeVisible();
    await expect(dialog.getByText("消耗积分")).toBeVisible();
    await expect(dialog.getByText("当前可花费")).toBeVisible();

    // Confirm -> the server freezes the points; the SUCCESS state shows
    // the pending-review lifecycle (spec §16.1), not a fake fulfillment.
    await dialog.getByRole("button", { name: "确认兑换" }).click();
    await expect(
      dialog.getByRole("heading", { name: "兑换申请已提交" }),
    ).toBeVisible();
    await expect(dialog.locator(".badge", { hasText: "待审核" })).toBeVisible();

    await dialog.getByRole("button", { name: "完成" }).click();
    await expect(dialog).not.toBeVisible();

    // The wallet refetch is authoritative: the freeze note appears and
    // the 最近兑换 receipt carries the server's own figures.
    const wallet = page.getByLabel("积分余额");
    await expect(wallet.getByText(/冻结在兑换申请中/)).toBeVisible();
    const latest = page.getByLabel("最近兑换");
    await expect(latest.getByText(/积分/)).toBeVisible();
    await expect(latest.locator(".badge", { hasText: "待审核" })).toBeVisible();
  });

  test("conflict shows typed INSUFFICIENT_POINTS copy and stays retry-friendly", async ({
    page,
  }) => {
    test.skip(
      POOR_STUDENT === undefined,
      "needs CQ_E2E_POOR_STUDENT (spendable below the cheapest reward)",
    );
    await loginAs(page, POOR_STUDENT!);
    await page.goto(`${BASE_URL}/rewards`);

    const confirmButton = page
      .locator(".reward-card", {
        has: page.getByRole("button", { name: "兑换", exact: true }),
      })
      .first()
      .getByRole("button", { name: "兑换", exact: true });
    await confirmButton.click();

    const dialog = page.locator("dialog.dialog");
    await dialog.getByRole("button", { name: "确认兑换" }).click();

    // Typed conflict copy (code-keyed, spec §29/patterns §15), and the
    // dialog re-arms so another attempt is possible.
    await expect(dialog.getByRole("alert")).toContainText("可花费积分不足");
    await expect(dialog.getByRole("button", { name: "确认兑换" })).toBeEnabled();
  });
});

test.describe("ranking privacy (spec §17/§40)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, STUDENT!);
  });

  const PERIODS = ["daily", "monthly", "all"] as const;

  for (const period of PERIODS) {
    test(`${period} board renders nickname/honor/score/rank only`, async ({ page }) => {
      await page.goto(`${BASE_URL}/rankings?period=${period}`);

      // Structural privacy pins: no user-id hooks, no contact-looking
      // attributes anywhere on the page.
      await expect(page.locator("[data-user-id]")).toHaveCount(0);

      // Seeded identity values (student numbers / phones / emails of
      // other seeded users) must never reach the page content.
      const body = await page.locator("body").innerText();
      for (const secret of PRIVATE_STRINGS) {
        expect(body, `page content must not contain ${secret}`).not.toContain(secret);
      }

      // Boards render rank-annotated rows in server order; scores in
      // the fixture stay below 6 digits, so a 6+ digit run would be a
      // leaked student number, not a score.
      const rows = page.locator(".board-row");
      const count = await rows.count();
      if (count > 0) {
        const boardText = await page.locator(".board-rows").first().innerText();
        expect(boardText, "no student-number-like digit runs on the board").not.toMatch(
          /[0-9]{6,}/,
        );
        await expect(rows.first().locator(".board-rank")).toHaveText(/^#\d+$/);
      }
    });
  }

  test("around-me anchors the current user by the visible 我 tag", async ({ page }) => {
    await page.goto(`${BASE_URL}/rankings?period=all`);

    const around = page.getByLabel("我的附近");
    await expect(around).toBeVisible();
    const rows = around.locator(".board-row");
    const count = await rows.count();
    if (count > 0) {
      // Exactly one anchored row (the caller's own), marked with the
      // non-color 我 cue; the row count stays within the server radius.
      await expect(around.locator(".board-me-tag")).toHaveCount(1);
      expect(count).toBeLessThanOrEqual(11); // radius 5 above + below + me
    } else {
      await expect(around.getByText("暂无附近排名")).toBeVisible();
    }
  });

  test("tabs switch boards through the URL (shareable period state)", async ({ page }) => {
    await page.goto(`${BASE_URL}/rankings`);
    // No param yet: the server defaults the board internally without
    // rewriting the address bar; the daily tab is still the current one.
    await expect(page.getByRole("link", { name: "今日榜" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await page.getByRole("link", { name: "本月榜" }).click();
    await expect(page).toHaveURL(/period=monthly/);
    await expect(page.getByRole("link", { name: "本月榜" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test.describe("mobile 375x812", () => {
    test.use({ viewport: { width: 375, height: 812 } });

    test("rewards shelf and ranking tabs stay usable", async ({ page }) => {
      await page.goto(`${BASE_URL}/rewards`);
      await expect(page.getByLabel("奖励货架")).toBeVisible();
      const firstCard = page.locator(".reward-card").first();
      if (await firstCard.isVisible()) {
        await expect(firstCard).toBeInViewport();
      }
      await page.goto(`${BASE_URL}/rankings`);
      await expect(page.getByRole("link", { name: "总榜" })).toBeVisible();
    });
  });
});
