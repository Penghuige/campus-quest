import type { Page } from "@playwright/test";
import { BASE_URL, ensureStudentLogin, expect, loginThroughUi, parseSeededAccount, test } from "./fixtures";

test.skip(process.env.CQ_E2E !== "1", "set CQ_E2E=1 with the disposable browser world");

async function openAchievements(page: Page) {
  await page.goto(`${BASE_URL}/profile/project-drafts`);
  await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
  await page.getByLabel("项目名称", { exact: true }).fill(`成果所属项目 ${Date.now()}`);
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
  await page.getByRole("link", { name: "管理成果草稿", exact: true }).click();
  await page.getByRole("button", { name: "新建成果草稿", exact: true }).click();
}

const editor = (page: Page) => page.getByRole("region", { name: "成果草稿编辑器" });
const save = (page: Page) => page.getByRole("button", { name: "保存成果草稿", exact: true });
const saved = (page: Page) => expect(editor(page).getByRole("status")).toHaveText("成果草稿已保存，仅自己可见。");

test("private achievements: mobile keyboard, literal preview, persistence and multiple works", async ({ page }, testInfo) => {
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await openAchievements(page);
  await save(page).click();
  await expect(page.getByLabel("成果名称", { exact: true })).toBeFocused();
  await expect(page.getByText("请输入成果名称", { exact: true })).toBeVisible();
  await page.getByLabel("成果名称", { exact: true }).fill("校园回收原型");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("作品与阶段成果说明", { exact: true })).toBeFocused();
  await page.getByLabel("作品与阶段成果说明", { exact: true }).fill('<img src=x onerror="alert(1)"> 第一轮原型');
  await page.getByLabel("作品链接", { exact: true }).fill("javascript:alert(1)");
  await save(page).click();
  await expect(page.getByLabel("作品链接", { exact: true })).toBeFocused();
  await page.getByLabel("作品链接", { exact: true }).fill("https://example.com/work");
  await page.getByLabel("立项或获奖说明", { exact: true }).fill("暂无获奖，已有可操作原型。");
  const outbound: string[] = [];
  page.on("request", (request) => { if (request.url().startsWith("https://example.com")) outbound.push(request.url()); });
  await page.getByRole("button", { name: "查看私有预览", exact: true }).click();
  const preview = page.getByRole("region", { name: "私有成果预览" });
  await expect(preview).toContainText('<img src=x onerror="alert(1)"> 第一轮原型');
  await expect(preview.locator("img")).toHaveCount(0);
  await expect(preview).toContainText("未保存输入预览");
  await expect(preview.getByRole("link", { name: "打开作品链接" })).toHaveAttribute("rel", "noopener noreferrer");
  expect(outbound).toEqual([]);
  await save(page).focus();
  await page.keyboard.press("Enter");
  await saved(page);
  await expect(preview).toContainText("已保存草稿预览");
  await page.screenshot({ path: testInfo.outputPath("achievement-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.reload();
  await page.getByRole("button", { name: "编辑成果：校园回收原型", exact: true }).click();
  await expect(page.getByLabel("作品链接", { exact: true })).toHaveValue("https://example.com/work");
  await page.getByLabel("作品与阶段成果说明", { exact: true }).fill("已完成第二轮原型。");
  await save(page).click(); await saved(page);
  await page.getByRole("button", { name: "返回成果列表", exact: true }).click();
  await page.getByRole("button", { name: "新建成果草稿", exact: true }).click();
  await page.getByLabel("成果名称", { exact: true }).fill("访谈报告");
  await save(page).click(); await saved(page);
  await page.getByRole("button", { name: "返回成果列表", exact: true }).click();
  await page.reload();
  await expect(page.getByRole("button", { name: "编辑成果：校园回收原型", exact: true })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "编辑成果：访谈报告", exact: true })).toHaveCount(1);
});

test("private achievements: stale edits retain input until explicit reload", async ({ page }, testInfo) => {
  await ensureStudentLogin(page);
  await openAchievements(page);
  await page.getByLabel("成果名称", { exact: true }).fill("并发成果");
  await save(page).click(); await saved(page);
  const other = await page.context().newPage();
  try {
    await other.goto(page.url());
    await other.getByRole("button", { name: "编辑成果：并发成果", exact: true }).click();
    await expect(other.getByLabel("成果名称", { exact: true })).toHaveValue("并发成果");
    await page.getByLabel("作品与阶段成果说明", { exact: true }).fill("第一处已保存。");
    await save(page).click(); await saved(page);
    await other.getByLabel("作品与阶段成果说明", { exact: true }).fill("第二处尚未保存。");
    await save(other).click();
    await expect(editor(other).getByRole("alert")).toContainText("当前输入已保留");
    await expect(save(other)).toBeDisabled();
    await other.getByRole("button", { name: "读取最新版本（保留当前输入）", exact: true }).click();
    await expect(other.getByRole("region", { name: "最新已保存成果" })).toContainText("第一处已保存。");
    await expect(other.getByLabel("作品与阶段成果说明", { exact: true })).toHaveValue("第二处尚未保存。");
    await other.screenshot({ path: testInfo.outputPath("achievement-conflict-desktop.png"), fullPage: true });
    await other.getByRole("button", { name: "载入此版本（替换当前输入）", exact: true }).click();
    await expect(other.getByLabel("作品与阶段成果说明", { exact: true })).toHaveValue("第一处已保存。");
    await expect(save(other)).toBeEnabled();
  } finally { await other.close(); }
});

test("private achievements: lost create acknowledgement reuses payload and account switch clears private UI", async ({ page }) => {
  await ensureStudentLogin(page);
  await openAchievements(page);
  const requests: unknown[] = [];
  await page.route("**/api/v1/ie/me/project-drafts/*/achievements", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      return route.abort("connectionfailed");
    }
    return route.continue();
  });
  await page.getByLabel("成果名称", { exact: true }).fill("丢包成果");
  await save(page).click();
  await expect(editor(page).getByRole("alert")).toContainText("尚未确认新建结果");
  await expect(page.getByLabel("成果名称", { exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "重试这次新建（不会重复创建）", exact: true }).click();
  await saved(page);
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
  await page.getByRole("button", { name: "返回成果列表", exact: true }).click();
  await expect(page.getByRole("button", { name: "编辑成果：丢包成果", exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "编辑成果：丢包成果", exact: true }).click();
  const sibling = await page.context().newPage();
  try {
    await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
    await expect(editor(page)).toHaveCount(0);
    await expect(page.getByText("丢包成果", { exact: true })).toHaveCount(0);
    await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"));
    await page.getByRole("button", { name: "编辑成果：丢包成果", exact: true }).click();
    await expect(page.getByLabel("成果名称", { exact: true })).toHaveValue("丢包成果");
  } finally { await sibling.close(); }
});
