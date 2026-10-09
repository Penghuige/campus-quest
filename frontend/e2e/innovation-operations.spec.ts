import type { Page } from "@playwright/test";
import { BASE_URL, ensureStudentLogin, expect, loginThroughUi, parseSeededAccount, staffLogin, test } from "./fixtures";

test.skip(process.env.CQ_E2E !== "1", "set CQ_E2E=1 with the disposable browser world");

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
  await ensureStudentLogin(page);
  await page.goto(`${BASE_URL}/profile`);
  const identity = page.getByRole("region", { name: "我的双创身份" });
  await expect(identity).toContainText("尚未获双创运营授权");
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, ignoreHTTPSErrors: true });
  try {
    const admin = await context.newPage();
    await openAdmin(admin);
    await admin.getByRole("button", { name: "授予运营身份", exact: true }).click();
    await expect(admin.getByLabel("操作原因", { exact: true })).toBeFocused();
    await grant(admin);
    await page.getByRole("button", { name: "重新读取身份", exact: true }).click();
    await expect(identity).toContainText("已获双创运营授权");
    await expect(identity).toContainText("不包含积分调整权限");
    // A mounted page must switch from A's granted identity to B's ungranted
    // identity without reloading; restore A for the remaining revoke flow.
    const sibling = await page.context().newPage();
    try {
      await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
      await expect(identity).toContainText("尚未获双创运营授权");
      await expect(identity).not.toContainText("已获双创运营授权");
      await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"));
      await expect(identity).toContainText("已获双创运营授权");
    } finally { await sibling.close(); }
    await admin.screenshot({ path: testInfo.outputPath("operations-mobile.png"), fullPage: true });
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
    await page.getByRole("button", { name: "重新读取身份", exact: true }).click();
    await expect(identity).toContainText("尚未获双创运营授权");
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
