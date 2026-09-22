/**
 * CampusQuest notifications e2e — Plan 09 Task 7 (inbox + bell).
 *
 * STATUS: SPEC ONLY — NOT WIRED TO A RUNNER YET.
 *
 * Same guard pattern as `e2e/auth.spec.ts` / `e2e/community.spec.ts`:
 * Playwright itself is installed by Plan 10 (no `@playwright/test`
 * dependency and no `test:e2e` script yet). Until then this file stays
 * invisible to the gates (tsconfig includes only `src/**`, eslint
 * globalIgnores lists `e2e/**`, `next build` never leaves `src/app`).
 * Once Plan 10 installs Playwright, remove the eslint ignore, add the
 * `test:e2e` script, and run with `CQ_E2E=1`.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1           enable the suite (required);
 * - CQ_E2E_BASE_URL    frontend origin (default http://localhost:3000);
 * - CQ_E2E_LOGIN_URL   login page (default $CQ_E2E_BASE_URL/login);
 * - CQ_E2E_STUDENT     pre-seeded student credentials
 *                      "student-number:password" (required for the
 *                      authenticated tests);
 * - the mark-read test additionally needs at least one UNREAD
 *                      notification for that student (Plan 10's fixture
 *                      seeds one, e.g. an approved submission or a
 *                      deadline reminder); it self-skips without it.
 *
 * OWNER-ONLY under test (spec §28 + notifications router): the inbox is
 * the caller's OWN rows and mark-read is owner-only server-side — the
 * frontend makes no access decision. Here that means: an anonymous
 * visitor gets the login CTA (the 401 path through the shell gate), and
 * the owner flow marks a row read from the server echo. The 403 paths
 * (staff token, foreign id -> PERMISSION_DENIED) are backend contract,
 * pinned by the S3 router tests.
 */
import { expect, test } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const LOGIN_URL = process.env.CQ_E2E_LOGIN_URL ?? `${BASE_URL}/login`;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240002:correct-horse"

test.skip(
  !E2E_ENABLED,
  "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* fixtures) to run this suite.",
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
    await loginAs(page, STUDENT!);
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
});

test.describe("owner mark-read (the §28 owner-only surface)", () => {
  test.skip(
    STUDENT === undefined,
    "needs CQ_E2E_STUDENT (seeded student credentials); Plan 10's fixture provides them.",
  );

  test("marking a row read flips it in place and drops it from the unread tab", async ({
    page,
  }) => {
    await loginAs(page, STUDENT!);
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
