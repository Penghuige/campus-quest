/** Batch-2 navigation contract: the campus entry from the main sidebar,
 *  the project-library links on the landing page, and the personal
 *  management entries. The upstream R1 spec also pinned its own /logout
 *  confirmation page and the operations workbench — neither is part of
 *  this merge (the main product keeps its LogoutSection; the workbench
 *  arrives with batch 3), so this spec pins only what batch 2 ships. */
import AxeBuilder from "@axe-core/playwright";
import { BASE_URL, ensureStudentLogin, expect, loginThroughUi, parseSeededAccount, test } from "./fixtures";

test.skip(process.env.CQ_E2E !== "1", "CQ_E2E=1 with an isolated real backend world");

test("R2: the campus entry opens the landing page with the project-library and talent doors", async ({ page }) => {
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${BASE_URL}/`);
  const navigation = page.getByRole("navigation", { name: "主导航", exact: true }).filter({ visible: true });
  await navigation.getByRole("link", { name: "创新创业", exact: true }).click();
  await expect(page.getByRole("heading", { name: "创新创业", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "双创板块入口", exact: true }).getByRole("link", { name: "项目库", exact: true })).toHaveAttribute("href", "/innovation/projects");
  await expect(page.getByRole("region", { name: "双创板块入口", exact: true }).getByRole("link", { name: "人才库 · 找同学", exact: true })).toHaveAttribute("href", "/innovation/talents");
  // The operations workbench arrives with batch 3; nothing may link there yet.
  await expect(page.getByRole("link", { name: "双创运营工作台", exact: true })).toHaveCount(0);
  await page.goto(`${BASE_URL}/profile`);
  await expect(page.getByRole("link", { name: "我的项目", exact: true })).toHaveAttribute("href", "/profile/project-drafts");
  await expect(page.getByRole("link", { name: "负责人资料与资格", exact: true })).toHaveAttribute("href", "/profile/owner-profile");
  await expect(page.getByRole("link", { name: "我的人才档案", exact: true })).toHaveAttribute("href", "/profile/talent-profile");
  await page.addStyleTag({ content: "*, *::before, *::after { animation: none !important; transition: none !important; }" });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("R2: narrow viewports reach the campus pages without horizontal scroll", async ({ page }) => {
  await ensureStudentLogin(page);
  for (const width of [320, 390, 800]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ["/innovation", "/profile/project-drafts", "/profile/owner-profile"]) {
      await page.goto(`${BASE_URL}${path}`);
      await expect(page.getByRole("main")).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  }
});

test("R2: a private draft does not leak across accounts after logout", async ({ page }) => {
  await ensureStudentLogin(page);
  const sentinel = "R2 前账号未保存的私密内容";
  await page.goto(`${BASE_URL}/profile/project-drafts`);
  await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
  await page.getByLabel("项目名称", { exact: true }).fill(sentinel);
  await page.goto(`${BASE_URL}/profile?tab=info`);
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page).toHaveURL(/\/login/);
  await loginThroughUi(page, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
  await page.goto(`${BASE_URL}/profile/project-drafts`);
  await expect(page.getByText(sentinel, { exact: true })).toHaveCount(0);
  await page.goBack();
  await expect(page.getByText(sentinel, { exact: true })).toHaveCount(0);
  await loginThroughUi(page, parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"));
});
