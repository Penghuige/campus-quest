/**
 * CampusQuest profile tabs e2e — defect #4 frontend half (QA
 * 2026-09-30): the "我" page splits into URL-state tabs (patterns §4);
 * the 个人信息 subpage owns ALL account editing. The avatar section
 * is capability-gated — until the backend avatar PR deploys (the /me
 * payload grows a boolean has_avatar), it must render NOTHING: this
 * spec pins that gate so the integration PR flips it deliberately,
 * not by drift.
 *
 * Environment contract (same guard as every spec):
 * - CQ_E2E=1        enable the suite (required);
 * - CQ_E2E_STUDENT  seeded credentials for ensureStudentLogin.
 */
import { ensureStudentLogin, expect, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* env) to run this suite.");

test.describe("profile tabs (defect #4)", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
  });

  test("bare /profile lands on 我的档案 with both tabs announced", async ({ page }) => {
    await page.goto(`${BASE}/profile`);
    // Growth island renders; the account sections do NOT (no 杂糅).
    await expect(page.getByRole("region", { name: "我的成长" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "个人信息" })).toHaveCount(0);

    const bar = page.getByRole("navigation", { name: "个人主页分区" });
    await expect(bar).toBeVisible();
    await expect(bar.getByRole("link", { name: "我的档案", exact: true })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const info = bar.getByRole("link", { name: "个人信息", exact: true });
    await expect(info).toBeVisible();
    await expect(info).not.toHaveAttribute("aria-current");
  });

  test("个人信息 owns every account-editing section (the defect's 归并)", async ({ page }) => {
    await page.goto(`${BASE}/profile?tab=info`);
    await expect(page).toHaveURL(/\/profile\?tab=info/);
    await expect(
      page.getByRole("navigation", { name: "个人主页分区" }).getByRole("link", {
        name: "个人信息",
        exact: true,
      }),
    ).toHaveAttribute("aria-current", "page");

    // All five sections live here: nickname/phone/email/password (the
    // moved AccountSettings) — and the avatar section is GATED OFF
    // until the backend lands has_avatar (flip this pin in the
    // integration PR, together with the upload flow tests).
    for (const title of ["昵称", "手机号", "邮箱", "密码"]) {
      await expect(page.getByRole("region", { name: title })).toBeVisible();
    }
    await expect(page.getByRole("region", { name: "头像" })).toHaveCount(0);
    // The growth island does NOT render on this tab.
    await expect(page.getByRole("region", { name: "我的成长" })).toHaveCount(0);
  });

  test("the tab rides the URL (shareable deep link)", async ({ page }) => {
    await page.goto(`${BASE}/profile?tab=info`);
    await expect(page.getByRole("region", { name: "昵称" })).toBeVisible();
    // The landing tab's link is the clean /profile URL.
    await page
      .getByRole("navigation", { name: "个人主页分区" })
      .getByRole("link", { name: "我的档案", exact: true })
      .click();
    await expect(page).toHaveURL(/\/profile$/);
    await expect(page.getByRole("region", { name: "我的成长" })).toBeVisible();
  });

  test("garbage tab values degrade to the landing tab, never an error page", async ({ page }) => {
    await page.goto(`${BASE}/profile?tab=personal`);
    await expect(page.getByRole("region", { name: "我的成长" })).toBeVisible();
    await expect(page.getByRole("region", { name: "昵称" })).toHaveCount(0);
  });
});
