/** Real private-draft persistence. Use only the disposable e2e world;
 * the standard runner's globalSetup must never target a user's demo data. */
import { ensureStudentLogin, expect, test, BASE_URL } from "./fixtures";
import type { Page } from "@playwright/test";

test.skip(process.env.CQ_E2E !== "1", "set CQ_E2E=1 with the disposable browser world");

async function startDraft(page: Page, title: string) {
  await page.goto(`${BASE_URL}/profile/project-drafts`);
  await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
  await page.getByLabel("项目名称", { exact: true }).fill(title);
}

test("private draft: create, reload, edit and keyboard-save on mobile", async ({ page }) => {
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE_URL}/profile`);
  await page.getByRole("link", { name: "我的项目", exact: true }).click();
  await expect(page.getByRole("heading", { name: "我的项目草稿", exact: true })).toBeVisible();
  const title = `校园环保草稿 ${Date.now()}`;
  await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
  await page.getByLabel("项目名称", { exact: true }).fill(title);
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("项目简介", { exact: true })).toBeFocused();
  await page.getByLabel("项目简介", { exact: true }).fill("先从校园回收调研开始。");
  await page.getByLabel("项目方向", { exact: true }).fill("环保");
  await page.getByLabel("项目阶段", { exact: true }).fill("调研中");
  await page.getByLabel("团队现状", { exact: true }).fill("两位同学共同整理问题。");
  await page.getByRole("button", { name: "保存草稿", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
  await page.getByRole("button", { name: "返回草稿列表", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: `编辑项目：${title}`, exact: true }).click();
  await expect(page.getByLabel("项目简介", { exact: true })).toHaveValue("先从校园回收调研开始。");
  await expect(page.getByLabel("项目阶段", { exact: true })).toHaveValue("调研中");
  await page.getByLabel("团队现状", { exact: true }).fill("已完成第一轮访谈。");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
  await page.getByRole("button", { name: "返回草稿列表", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: `编辑项目：${title}`, exact: true }).click();
  await expect(page.getByLabel("团队现状", { exact: true })).toHaveValue("已完成第一轮访谈。");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("private draft: a stale save preserves local input while reading the latest version", async ({ page }) => {
  await ensureStudentLogin(page);
  const title = `并发编辑草稿 ${Date.now()}`;
  await startDraft(page, title);
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
  const other = await page.context().newPage();
  try {
    await other.goto(`${BASE_URL}/profile/project-drafts`);
    await other.getByRole("button", { name: `编辑项目：${title}`, exact: true }).click();
    await expect(other.getByLabel("项目名称", { exact: true })).toHaveValue(title);
    await page.getByLabel("项目简介", { exact: true }).fill("第一处保存的版本。");
    await page.getByRole("button", { name: "保存草稿", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
    await other.getByLabel("项目简介", { exact: true }).fill("另一处尚未保存的内容。");
    await other.getByRole("button", { name: "保存草稿", exact: true }).click();
    await expect(other.getByRole("region", { name: "项目草稿编辑器" }).getByRole("alert")).toContainText("你的未保存内容已保留");
    await expect(other.getByLabel("项目简介", { exact: true })).toHaveValue("另一处尚未保存的内容。");
    await other.getByRole("button", { name: "读取最新版本（保留当前输入）", exact: true }).click();
    await expect(other.getByRole("region", { name: "最新已保存版本" })).toContainText("第一处保存的版本。");
    await expect(other.getByLabel("项目简介", { exact: true })).toHaveValue("另一处尚未保存的内容。");
    await other.getByRole("button", { name: "载入此版本（替换当前输入）", exact: true }).click();
    await expect(other.getByLabel("项目简介", { exact: true })).toHaveValue("第一处保存的版本。");
  } finally {
    await other.close();
  }
});

test("private draft: retrying a lost create response reuses the request key", async ({ page }) => {
  await ensureStudentLogin(page);
  const title = `网络重试草稿 ${Date.now()}`;
  const requestIds: string[] = [];
  await page.route("**/api/v1/ie/me/project-drafts", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    requestIds.push(route.request().postDataJSON().request_id);
    if (requestIds.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      return route.abort("failed"); // The real backend has committed; the browser did not learn the result.
    }
    return route.continue();
  });
  await startDraft(page, title);
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("region", { name: "项目草稿编辑器" }).getByRole("alert")).toContainText("尚未确认保存结果");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
  expect(requestIds).toHaveLength(2);
  expect(requestIds[1]).toBe(requestIds[0]);
  await page.getByRole("button", { name: "返回草稿列表", exact: true }).click();
  await expect(page.getByRole("button", { name: `编辑项目：${title}`, exact: true })).toHaveCount(1);
});
