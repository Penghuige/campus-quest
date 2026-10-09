import { ensureStudentLogin, expect, test, BASE_URL } from "./fixtures";
import type { Page } from "@playwright/test";

test.skip(process.env.CQ_E2E !== "1", "set CQ_E2E=1 with the disposable browser world");

async function openProfile(page: Page) {
  await page.goto(`${BASE_URL}/profile/owner-profile`);
  await expect(page.getByRole("region", { name: "负责人资料编辑器" })).toBeVisible();
}

test("private owner profile: validate, save, reload and edit on mobile", async ({ page }) => {
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE_URL}/profile`);
  await page.getByRole("link", { name: "负责人资料", exact: true }).click();
  await expect(page.getByRole("heading", { name: "负责人资料", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "负责人资料编辑器" })).toContainText("保存不会自动开通负责人资格");
  await page.getByLabel("姓名", { exact: true }).fill("");
  await page.getByRole("button", { name: "保存负责人资料", exact: true }).click();
  await expect(page.getByLabel("姓名", { exact: true })).toBeFocused();
  await expect(page.getByText("请输入姓名", { exact: true })).toBeVisible();
  await page.getByLabel("姓名", { exact: true }).fill("演示同学");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("学号", { exact: true })).toBeFocused();
  await page.getByLabel("学号", { exact: true }).fill("001234");
  await page.getByLabel("专业", { exact: true }).fill("计算机");
  await page.getByLabel("年级", { exact: true }).fill("2026级");
  await page.getByRole("button", { name: "保存负责人资料", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status")).toHaveText("资料已保存；负责人资格尚未由此开通。");
  await page.reload();
  await expect(page.getByLabel("学号", { exact: true })).toHaveValue("001234");
  await expect(page.getByLabel("专业", { exact: true })).toHaveValue("计算机");
  await page.getByLabel("年级", { exact: true }).fill("大一");
  await page.getByRole("button", { name: "保存负责人资料", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("资料已保存");
  await page.reload();
  await expect(page.getByLabel("年级", { exact: true })).toHaveValue("大一");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("private owner profile: stale save preserves inputs until explicit reconciliation", async ({ page }) => {
  await ensureStudentLogin(page);
  await openProfile(page);
  for (const [label, value] of Object.entries({ 姓名: "演示同学", 学号: "001234", 专业: "计算机", 年级: "大一" })) {
    await page.getByLabel(label, { exact: true }).fill(value);
  }
  await page.getByRole("button", { name: "保存负责人资料", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("资料已保存");
  const other = await page.context().newPage();
  try {
    await openProfile(other);
    await page.getByLabel("专业", { exact: true }).fill("第一处已保存专业");
    await page.getByRole("button", { name: "保存负责人资料", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("资料已保存");
    await other.getByLabel("专业", { exact: true }).fill("第二处未保存专业");
    await other.getByRole("button", { name: "保存负责人资料", exact: true }).click();
    await expect(other.getByRole("region", { name: "负责人资料编辑器" }).getByRole("alert")).toContainText("你的未保存内容已保留");
    await other.getByRole("button", { name: "读取最新资料（保留当前输入）", exact: true }).click();
    await expect(other.getByRole("region", { name: "最新负责人资料" })).toContainText("第一处已保存专业");
    await expect(other.getByLabel("专业", { exact: true })).toHaveValue("第二处未保存专业");
    await other.getByRole("button", { name: "载入此资料（替换当前输入）", exact: true }).click();
    await expect(other.getByLabel("专业", { exact: true })).toHaveValue("第一处已保存专业");
  } finally { await other.close(); }
});
