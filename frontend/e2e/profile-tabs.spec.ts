/**
 * CampusQuest profile tabs e2e — defect #4 (QA 2026-09-30): the "我"
 * page splits into URL-state tabs (patterns §4); the 个人信息 subpage
 * owns ALL account editing. Since #21 landed, the avatar section is
 * live end to end — the final test drives the REAL raw-body chain
 * (upload → display → remove → fallback → rate limit) against the
 * backend, the reviewer-locked integration verification.
 *
 * Environment contract (same guard as every spec):
 * - CQ_E2E=1        enable the suite (required);
 * - CQ_E2E_STUDENT  seeded credentials for ensureStudentLogin.
 */
import { ensureStudentLogin, expect, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";

/** A valid 1x1 red PNG (magic bytes intact) — the upload payload. */
const PNG_BYTES_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

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
    // moved AccountSettings) and the avatar section — the capability
    // gate flipped OPEN when #21 landed (has_avatar now always on
    // /me); the full upload chain is covered by the test below.
    for (const title of ["昵称", "头像", "手机号", "邮箱", "密码"]) {
      await expect(page.getByRole("region", { name: title })).toBeVisible();
    }
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

  // The #21 integration flip: the REAL raw-body chain against the live
  // backend (the reviewer-locked verification). One linear test — the
  // account's avatar state AND the 10-minute rate limit persist across
  // tests in a shared world, so the chain must own its full sequence.
  test("avatar chain: raw-body upload → display → remove → fallback → rate limit", async ({ page }) => {
    await page.goto(`${BASE}/profile?tab=info`);
    const section = page.getByRole("region", { name: "头像" });
    await expect(section).toBeVisible();
    // Fresh world: no avatar yet — the initial-letter fallback (D5).
    await expect(section.locator(".avatar-figure-initial")).toBeVisible();

    // Upload through the REAL picker: client crop + raw-body POST.
    const pick = () =>
      section.locator("input[type=file]").setInputFiles({
        name: "avatar.png",
        mimeType: "image/png",
        buffer: Buffer.from(PNG_BYTES_BASE64, "base64"),
      });
    await pick();
    await expect(section.getByText("头像已更新。")).toBeVisible({ timeout: 20_000 });
    // The bearer-fetch display path replaces the fallback figure.
    await expect(section.locator(".avatar-figure img")).toBeVisible({ timeout: 15_000 });

    // Remove: the inline two-step confirm, then the D5 fallback again.
    await section.getByRole("button", { name: "移除头像" }).click();
    await section.getByRole("button", { name: "确认移除？" }).click();
    await expect(section.getByText("已恢复默认头像。")).toBeVisible();
    await expect(section.locator(".avatar-figure-initial")).toBeVisible();

    // The D2 rate limit (1 change / 10 min): a second upload inside
    // the window gets the typed copy — the RATE_LIMITED branch of
    // avatarErrorText, live against the backend's frozen code.
    await pick();
    await expect(section.getByText(/10 分钟/)).toBeVisible({ timeout: 20_000 });
  });
});
