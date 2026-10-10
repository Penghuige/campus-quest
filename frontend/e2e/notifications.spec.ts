/**
 * CampusQuest notifications e2e — Plan 09 Task 7 (inbox + bell).
 *
 * Gate discipline: every test is skipped unless CQ_E2E=1, so importing
 * the file never depends on a live backend during ordinary development.
 * ESLint covers e2e/**; tsc does not (tsconfig's include stops at src).
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1           enable the suite (required);
 * - CQ_E2E_BASE_URL    frontend origin (default https://localhost:3000);
 * - CQ_E2E_STUDENT     pre-seeded student credentials
 *                      "student-number:password" (required for the
 *                      authenticated tests);
 * - the mark-read test depends on a side-effect unread row from an
 *                      earlier battery test (the world seeds zero
 *                      notifications — see the noskip exemption
 *                      entry); it self-skips without one.
 *
 * OWNER-ONLY under test (spec §28 + notifications router): the inbox is
 * the caller's OWN rows and mark-read is owner-only server-side — the
 * frontend makes no access decision. Here that means: an anonymous
 * visitor gets the login CTA (the 401 path through the shell gate), and
 * the owner flow marks a row read from the server echo. The 403 paths
 * (staff token, foreign id -> PERMISSION_DENIED) are backend contract,
 * pinned by the S3 router tests.
 */
import { expect, test } from "./fixtures";

import { ensureStudentLogin } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const STUDENT = process.env.CQ_E2E_STUDENT; // "username:password" (factories' DEFAULT_PASSWORD)

test.skip(
  !E2E_ENABLED,
  "set CQ_E2E=1 (and the CQ_E2E_* fixtures) to run this suite.",
);

/**
 * Open the shared student session (cookie resume; the suite's first run
 * goes through the real login form — see fixtures.ensureStudentLogin,
 * which also keeps the whole suite under the auth:login rate limit).
 */
async function loginAs(page: import("@playwright/test").Page): Promise<void> {
  await ensureStudentLogin(page);
}

test.describe("anonymous visitor (the guard's 401 path)", () => {
  test("/notifications shows the login CTA, not a broken page", async ({
    page,
  }) => {
    await page.goto(`${BASE_URL}/notifications`);
    await expect(page.getByRole("heading", { name: "登录 CampusQuest" })).toBeVisible();
    await expect(page.getByRole("link", { name: "去登录" })).toBeVisible();
    // No inbox affordances leak to the anonymous shell.
    await expect(page.getByLabel("通知收件箱")).toHaveCount(0);
  });
});

test.describe("inbox render + filter tabs (§28; patterns §4 URL state)", () => {
  test.skip(
    STUDENT === undefined,
    "needs CQ_E2E_STUDENT (seeded student credentials); Plan 10's fixture provides them.",
  );
  test.beforeEach(async ({ page }) => {
    await loginAs(page);
    await page.goto(`${BASE_URL}/notifications`);
  });

  test("the page renders its header, the section, and both filter tabs", async ({
    page,
  }) => {
    await expect(page.getByRole("heading", { name: "通知", exact: true })).toBeVisible();
    const inbox = page.getByLabel("通知收件箱");
    await expect(inbox).toBeVisible();
    await expect(inbox.getByRole("link", { name: "全部", exact: true })).toBeVisible();
    await expect(inbox.getByRole("link", { name: "未读", exact: true })).toBeVisible();
    // All|empty is a stated state, never a blank area (design §10).
    const rows = inbox.locator(".notif-item");
    const empty = inbox.getByText("暂无通知");
    await expect(rows.or(empty).first()).toBeVisible();
  });

  test("the unread filter rides the URL (shareable state)", async ({ page }) => {
    const inbox = page.getByLabel("通知收件箱");
    await expect(
      inbox.getByRole("link", { name: "全部", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await inbox.getByRole("link", { name: "未读", exact: true }).click();
    await expect(page).toHaveURL(/\/notifications\?filter=unread$/);
    await expect(
      inbox.getByRole("link", { name: "未读", exact: true }),
    ).toHaveAttribute("aria-current", "page");
  });

  test("the topbar bell links to the inbox", async ({ page }) => {
    const bell = page
      .getByRole("banner")
      .getByRole("link", { name: /未读通知/ });
    await expect(bell).toBeVisible();
    await bell.click();
    await expect(page).toHaveURL(/\/notifications/);
    await expect(page.getByLabel("通知收件箱")).toBeVisible();
  });

  test("the bell and the avatar chip share one circle language (owner feedback, 2026-10-10)", async ({ page }) => {
    // The bell used to be a control-height SQUARE with a square hover
    // block beside the round avatar chip. Both pill slots are 2rem
    // circles now, and BOTH have a hover treatment (the chip had
    // none — two adjacent interactive slots must agree).
    const bell = page
      .getByRole("banner")
      .getByRole("link", { name: /未读通知/ });
    const chip = page.getByRole("banner").getByRole("link", { name: "我的账户" });
    await expect(bell).toBeVisible();
    await expect(chip).toBeVisible();

    const shape = await page.evaluate(() => {
      const read = (selector: string) => {
        const el = document.querySelector(selector);
        if (el === null) {
          return null;
        }
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return {
          radius: style.borderRadius,
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          bg: style.backgroundColor,
        };
      };
      return { bell: read(".app-topbar-actions .topbar-bell"), chip: read(".app-topbar-actions .avatar-chip") };
    });
    expect(shape.bell?.radius).toBe("50%");
    expect(shape.bell?.width).toBe(32);
    expect(shape.bell?.height).toBe(32);
    expect(shape.chip?.radius).toBe("50%");
    expect(shape.chip?.width).toBe(32);
    expect(shape.chip?.height).toBe(32);

    // The hover fills: the bell takes the quiet surface-2, the chip
    // deepens its primary wash — each CHANGES on hover.
    const bgOf = () =>
      page.evaluate(() => ({
        bell: getComputedStyle(document.querySelector(".app-topbar-actions .topbar-bell")!).backgroundColor,
        chip: getComputedStyle(document.querySelector(".app-topbar-actions .avatar-chip")!).backgroundColor,
      }));
    const rest = await bgOf();
    await bell.hover();
    await expect.poll(async () => (await bgOf()).bell).not.toBe(rest.bell);
    await chip.hover();
    await expect.poll(async () => (await bgOf()).chip).not.toBe(rest.chip);
  });
});

test.describe("owner mark-read (the §28 owner-only surface)", () => {
  test.skip(
    STUDENT === undefined,
    "needs CQ_E2E_STUDENT (seeded student credentials); Plan 10's fixture provides them.",
  );

  test("marking a row read flips it in place and drops it from the unread tab", async ({
    page,
  }) => {
    await loginAs(page);
    await page.goto(`${BASE_URL}/notifications`);

    const unreadRow = page.locator(".notif-item[data-read='false']").first();
    if (!(await unreadRow.isVisible())) {
      test.skip(
        true,
        "needs a seeded UNREAD notification for CQ_E2E_STUDENT (Plan 10's fixture seeds one).",
      );
    }
    const title = (await unreadRow.locator(".notif-title").innerText()).trim();
    await expect(unreadRow.getByText("未读", { exact: true })).toBeVisible();
    await unreadRow.getByRole("button", { name: "标为已读" }).click();

    // Optimistic flip confirmed by the server echo: read state, no 未读
    // badge, no further action (design §10/§12 — weight + badge, not color).
    const row = page.locator(".notif-item", { hasText: title }).first();
    await expect(row).toHaveAttribute("data-read", "true");
    await expect(row.getByText("未读", { exact: true })).toHaveCount(0);
    await expect(row.getByRole("button", { name: "标为已读" })).toHaveCount(0);

    // A read row no longer matches the unread filter.
    await page.getByRole("link", { name: "未读", exact: true }).click();
    await expect(page.locator(".notif-item", { hasText: title })).toHaveCount(0);
  });
});
