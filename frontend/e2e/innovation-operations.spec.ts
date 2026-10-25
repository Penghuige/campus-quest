import type { Page } from "@playwright/test";
import { API_URL, BASE_URL, ensureStudentLogin, expect, loginThroughUi, mintToken, parseSeededAccount, staffLogin, test } from "./fixtures";

test.skip(process.env.CQ_E2E !== "1", "set CQ_E2E=1 with the disposable browser world");

/** The operations rail entry arrives with batch 3 (its workbench page is
 *  the batch-3 deliverable); batch 2 pins the identity fact via the
 *  capabilities API the entry will read. */
async function operationsEnabled(page: Page, userId: string): Promise<boolean> {
  const response = await page.request.get(`${API_URL}/ie/me/capabilities`, { headers: { Authorization: `Bearer ${mintToken(userId)}` } });
  expect(response.status()).toBe(200);
  return (await response.json() as { operations_enabled: boolean }).operations_enabled;
}

async function openAdmin(page: Page) {
  const credentials = process.env.CQ_E2E_ADMIN;
  const secret = process.env.CQ_E2E_ADMIN_TOTP_SECRET;
  if (!credentials || !secret) throw new Error("browser world must export genuine admin TOTP credentials");
  await staffLogin(page, credentials, secret);
  await page.goto(`${BASE_URL}/admin/innovation-operations`);
  await chooseStudent(page);
}

async function chooseStudent(page: Page) {
  const username = process.env.CQ_E2E_STUDENT?.split(":")[0];
  if (!username) throw new Error("browser world must export student credentials");
  const select = page.getByLabel("授权对象", { exact: true });
  await expect(select).toBeVisible();
  for (let pageNumber = 0; pageNumber < 20; pageNumber += 1) {
    const option = select.getByRole("option").filter({ hasText: `${username} ·` });
    if (await option.count()) {
      await select.selectOption(await option.getAttribute("value") as string);
      await expect(page.getByRole("region", { name: "当前账号运营授权" })).toBeVisible();
      return;
    }
    const next = page.getByRole("button", { name: "下一页账号", exact: true });
    await expect(next).toBeEnabled();
    await next.click();
    await expect(select).toBeVisible();
  }
  throw new Error("seeded student missing from account directory");
}

async function grant(page: Page, reason = "演示授权运营") {
  await page.getByLabel("操作原因", { exact: true }).fill(reason);
  await page.getByRole("button", { name: "授予运营身份", exact: true }).click();
  await expect(page.getByText("已授予双创运营身份。", { exact: true })).toBeVisible();
}

test("operations identity: genuine admin grant, student visibility and confirmed revoke on mobile", async ({ page, browser }, testInfo) => {
  const studentId = process.env.CQ_E2E_STUDENT_ID!;
  const authorId = (process.env.CQ_E2E_AUTHOR_SECRETS ?? "").split(",")[4];
  await ensureStudentLogin(page);
  await page.goto(`${BASE_URL}/profile`);
  expect(await operationsEnabled(page, studentId)).toBe(false);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, ignoreHTTPSErrors: true });
  try {
    const admin = await context.newPage();
    await openAdmin(admin);
    await admin.getByRole("button", { name: "授予运营身份", exact: true }).click();
    await expect(admin.getByLabel("操作原因", { exact: true })).toBeFocused();
    await grant(admin);
    // Granting flips the identity fact the (batch-3) rail entry reads.
    expect(await operationsEnabled(page, studentId)).toBe(true);
    await page.reload();
    expect(await operationsEnabled(page, studentId)).toBe(true);
    // The fact is per account: the sibling student never inherits it.
    const sibling = await page.context().newPage();
    try {
      await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
      expect(await operationsEnabled(page, authorId)).toBe(false);
      expect(await operationsEnabled(page, studentId)).toBe(true);
    } finally { await sibling.close(); }
    await admin.screenshot({ path: testInfo.outputPath("operations-mobile.png"), fullPage: true });
    // The admin management face is a desktop workbench (design system §8:
    // table + filters); main's admin shell has no narrow-width fallback
    // for these pages. Assert the desktop layout instead, and leave the
    // narrow fallback to a dedicated admin-responsive round.
    await admin.setViewportSize({ width: 1440, height: 900 });
    expect(await admin.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await admin.getByLabel("操作原因", { exact: true }).fill("演示结束撤回");
    await admin.getByRole("button", { name: "撤回运营身份", exact: true }).click();
    const dialog = admin.getByRole("dialog", { name: "撤回双创运营身份" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await expect(admin.getByRole("button", { name: "撤回运营身份", exact: true })).toBeFocused();
    await expect(admin.getByText("当前已授权", { exact: true })).toBeVisible();
    await admin.keyboard.press("Enter");
    await expect(dialog).toBeVisible();
    await admin.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(admin.getByRole("button", { name: "撤回运营身份", exact: true })).toBeFocused();
    await admin.getByRole("button", { name: "撤回运营身份", exact: true }).click();
    await dialog.getByRole("button", { name: "确认撤回", exact: true }).click();
    await expect(admin.getByText("已撤回双创运营身份。", { exact: true })).toBeVisible();
    await expect(admin.getByRole("button", { name: "授予运营身份", exact: true })).toBeFocused();
    expect(await operationsEnabled(page, studentId)).toBe(false);
  } finally { await context.close(); }
});

test("operations identity: stale page and lost acknowledgement force reconciliation", async ({ page, browser }, testInfo) => {
  // Keep the fixture's persistent page a student session.
  await ensureStudentLogin(page);
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const first = await context.newPage();
    await openAdmin(first);
    const second = await context.newPage();
    await second.goto(`${BASE_URL}/admin/innovation-operations`);
    await chooseStudent(second);
    await grant(first);
    await second.getByLabel("操作原因", { exact: true }).fill("旧页面授权原因");
    await second.getByRole("button", { name: "授予运营身份", exact: true }).click();
    await expect(second.getByRole("region", { name: "当前账号运营授权" }).getByRole("alert")).toContainText("请重新读取");
    await expect(second.getByRole("button", { name: "授予运营身份", exact: true })).toBeDisabled();
    await second.getByRole("button", { name: "重新读取授权状态", exact: true }).click();
    await expect(second.getByText("当前已授权", { exact: true })).toBeVisible();
    await expect(second.getByLabel("操作原因", { exact: true })).toHaveValue("旧页面授权原因");
    await second.route("**/api/v1/admin/ie/operations-grants/*", async (route) => {
      if (route.request().method() !== "PUT") return route.continue();
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      await route.abort("connectionfailed");
    });
    await second.getByRole("button", { name: "撤回运营身份", exact: true }).click();
    await second.getByRole("button", { name: "确认撤回", exact: true }).click();
    await expect(second.getByRole("region", { name: "当前账号运营授权" }).getByRole("alert")).toContainText("尚未确认操作结果");
    await expect(second.getByRole("button", { name: "撤回运营身份", exact: true })).toBeDisabled();
    await expect(second.getByRole("button", { name: "重新读取授权状态", exact: true })).toBeFocused();
    await second.getByRole("button", { name: "重新读取授权状态", exact: true }).click();
    await expect(second.getByText("当前未授权", { exact: true })).toBeVisible();
    await second.screenshot({ path: testInfo.outputPath("operations-desktop.png"), fullPage: true });
  } finally { await context.close(); }
});
