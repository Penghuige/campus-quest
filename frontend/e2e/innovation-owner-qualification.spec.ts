import type { BrowserContext, Page } from "@playwright/test";
import { BASE_URL, ensureStudentLogin, expect, loginThroughUi, parseSeededAccount, staffLogin, test } from "./fixtures";

test.skip(process.env.CQ_E2E !== "1", "set CQ_E2E=1 with the disposable browser world");

async function saveProfile(page: Page, name: string, major: string) {
  await page.goto(`${BASE_URL}/profile/owner-profile`);
  const editor = page.getByRole("region", { name: "负责人资料编辑器" });
  await expect(editor).toBeVisible();
  for (const [label, value] of Object.entries({ 姓名: name, 学号: "00123456", 专业: major, 年级: "2026级" })) {
    await editor.getByLabel(label, { exact: true }).fill(value);
  }
  await editor.getByRole("button", { name: "保存负责人资料", exact: true }).click();
  await expect(editor.getByRole("status", { name: "负责人资料保存状态" })).toContainText("资料已保存");
}

// This file runs serially (the runner has one worker). Keep only the genuine
// admin session's latest cookies in worker memory: every app navigation rotates
// the single-use refresh cookie, so a snapshot taken at login becomes stale.
// Each test still gets a fresh context and drives the real qualification UI.
let adminCookies: Awaited<ReturnType<BrowserContext["cookies"]>> | undefined;
const authenticatedAdminContexts = new WeakSet<BrowserContext>();

async function rememberAdminSession(context: BrowserContext) {
  if (authenticatedAdminContexts.has(context)) adminCookies = await context.cookies();
}

async function adminLogin(page: Page) {
  const account = process.env.CQ_E2E_ADMIN;
  const secret = process.env.CQ_E2E_ADMIN_TOTP_SECRET;
  if (!account || !secret) throw new Error("world must export genuine admin TOTP credentials");
  if (adminCookies) {
    await page.context().addCookies(adminCookies);
    const meResponse = page.waitForResponse((response) =>
      response.url().includes("/api/v1/me") && response.status() === 200);
    await page.goto(`${BASE_URL}/`);
    const me = await (await meResponse).json();
    expect(me.username).toBe(account.split(":")[0]);
    expect(me.role).toBe("ADMIN");
  } else {
    await staffLogin(page, account, secret);
  }
  authenticatedAdminContexts.add(page.context());
  await page.goto(`${BASE_URL}/admin/owner-qualifications`);
}

async function viewOnlyPendingApplication(page: Page) {
  const queue = page.getByRole("region", { name: "负责人资格申请队列" });
  await expect(queue).toBeVisible();
  const view = queue.getByRole("button", { name: "查看申请", exact: true });
  await expect(view).toHaveCount(1);
  await view.click();
  await expect(page.getByRole("region", { name: "负责人资格申请详情" })).toBeVisible();
}

test("owner qualification: student applies, admin confirms, account switch clears private state", async ({ page, browser }, testInfo) => {
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await saveProfile(page, "开通演示甲", "示例专业甲");
  const qualification = page.getByRole("region", { name: "负责人资格", exact: true });
  await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("未申请");
  await qualification.getByRole("button", { name: "申请负责人资格", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("等待管理员");
  await page.reload();
  await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("等待管理员");
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, ignoreHTTPSErrors: true });
  try {
    const admin = await context.newPage();
    await adminLogin(admin);
    const queue = admin.getByRole("region", { name: "负责人资格申请队列" });
    await expect(queue).toBeVisible();
    await expect(queue).not.toContainText("00123456");
    await expect(queue).not.toContainText("开通演示甲");
    await viewOnlyPendingApplication(admin);
    const detail = admin.getByRole("region", { name: "负责人资格申请详情" });
    await expect(detail).toContainText("示例专业甲");
    const open = detail.getByRole("button", { name: "开通负责人资格", exact: true });
    const dialog = admin.getByRole("dialog", { name: "开通负责人资格", exact: true });
    await open.click();
    await expect(dialog).toContainText("成果仍需单独核实");
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await expect(open).toBeFocused();
    await admin.keyboard.press("Enter");
    await expect(dialog).toBeVisible();
    await admin.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(open).toBeFocused();
    await open.click();
    await dialog.getByRole("button", { name: "确认开通", exact: true }).click();
    await expect(detail.getByRole("status", { name: "管理员资格操作结果" })).toHaveText("负责人资格已开通。");
    await expect(detail.getByRole("button", { name: "重新读取申请详情", exact: true })).toBeFocused();
    await qualification.getByRole("button", { name: "重新读取资格状态", exact: true }).click();
    await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("已开通");
    await admin.screenshot({ path: testInfo.outputPath("owner-qualification-admin-mobile.png"), fullPage: true });
    await page.screenshot({ path: testInfo.outputPath("owner-qualification-student-mobile.png"), fullPage: true });
    expect(await admin.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const sibling = await page.context().newPage();
    try {
      await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
      await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("未申请");
      await expect(page.getByLabel("姓名", { exact: true })).toHaveValue("");
      await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"));
      await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("已开通");
      await expect(page.getByLabel("姓名", { exact: true })).toHaveValue("开通演示甲");
    } finally { await sibling.close(); }
  } finally { await rememberAdminSession(context); await context.close(); }
});

test("owner qualification: updated snapshot rejects old admin page and lost acknowledgement reconciles", async ({ browser }, testInfo) => {
  const studentContext = await browser.newContext({ ignoreHTTPSErrors: true });
  const adminContext = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const student = await studentContext.newPage();
    await loginThroughUi(student, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
    await saveProfile(student, "开通演示乙", "申请快照一");
    const qualification = student.getByRole("region", { name: "负责人资格", exact: true });
    await qualification.getByRole("button", { name: "申请负责人资格", exact: true }).click();
    await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("等待管理员");
    const first = await adminContext.newPage();
    await adminLogin(first);
    await viewOnlyPendingApplication(first);
    const second = await adminContext.newPage();
    await second.goto(`${BASE_URL}/admin/owner-qualifications`);
    await viewOnlyPendingApplication(second);
    await student.getByLabel("专业", { exact: true }).fill("申请快照二");
    await expect(qualification.getByRole("button", { name: "更新资格申请", exact: true })).toBeDisabled();
    await student.getByRole("button", { name: "保存负责人资料", exact: true }).click();
    await expect(student.getByRole("status", { name: "负责人资料保存状态" })).toContainText("资料已保存");
    await qualification.getByRole("button", { name: "更新资格申请", exact: true }).click();
    await expect(qualification.getByRole("status", { name: "资格操作结果" })).toContainText("资格申请已提交");
    const detail = second.getByRole("region", { name: "负责人资格申请详情" });
    await expect(detail).toContainText("申请快照一");
    await detail.getByRole("button", { name: "开通负责人资格", exact: true }).click();
    await second.getByRole("button", { name: "确认开通", exact: true }).click();
    await expect(detail.getByRole("alert")).toContainText("重新读取");
    await expect(detail.getByRole("button", { name: "开通负责人资格", exact: true })).toBeDisabled();
    await expect(detail.getByRole("button", { name: "重新读取申请详情", exact: true })).toBeFocused();
    await detail.getByRole("button", { name: "重新读取申请详情", exact: true }).click();
    await expect(detail).toContainText("申请快照二");
    await second.route("**/api/v1/admin/ie/owner-qualifications/*/approve", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      await route.abort("connectionfailed");
    });
    await detail.getByRole("button", { name: "开通负责人资格", exact: true }).click();
    await second.getByRole("button", { name: "确认开通", exact: true }).click();
    await expect(detail.getByRole("alert")).toContainText("重新读取");
    await expect(detail.getByRole("button", { name: "开通负责人资格", exact: true })).toBeDisabled();
    await detail.getByRole("button", { name: "重新读取申请详情", exact: true }).click();
    await expect(detail.getByRole("status", { name: "申请开通状态" })).toContainText("已开通");
    await qualification.getByRole("button", { name: "重新读取资格状态", exact: true }).click();
    await expect(qualification.getByRole("status", { name: "负责人资格状态" })).toContainText("已开通");
    await second.screenshot({ path: testInfo.outputPath("owner-qualification-admin-desktop.png"), fullPage: true });
  } finally { await rememberAdminSession(adminContext); await studentContext.close(); await adminContext.close(); }
});

test("owner qualification: empty later queue page retains navigation after refresh", async ({ page }) => {
  const rows = Array.from({ length: 21 }, (_, index) => ({
    user_id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
    version: 1, requested_at: "2026-10-09T01:00:00Z",
  }));
  let remaining = rows;
  // This read-only fixture isolates pagination; approval is exercised through
  // the real backend in the two tests above.
  await page.route("**/api/v1/admin/ie/owner-qualifications?*", async (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset"));
    await route.fulfill({ json: { items: remaining.slice(offset, offset + 20), total: remaining.length } });
  });
  await adminLogin(page);
  const queue = page.getByRole("region", { name: "负责人资格申请队列" });
  await expect(queue.getByRole("button", { name: "查看申请", exact: true })).toHaveCount(20);
  await queue.getByRole("button", { name: "下一页申请", exact: true }).click();
  await expect(queue.getByRole("button", { name: "查看申请", exact: true })).toHaveCount(1);
  remaining = rows.slice(0, 20);
  await queue.getByRole("button", { name: "刷新申请队列", exact: true }).click();
  await expect(queue.getByText("暂无等待开通的资格申请", { exact: true })).toBeVisible();
  await queue.getByRole("button", { name: "上一页申请", exact: true }).click();
  await expect(queue.getByRole("button", { name: "查看申请", exact: true })).toHaveCount(20);
});
