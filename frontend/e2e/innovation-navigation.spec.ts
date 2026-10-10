import AxeBuilder from "@axe-core/playwright";
import { API_URL, BASE_URL, ensureStudentLogin, expect, loginThroughUi, mintToken, parseSeededAccount, staffLogin, test } from "./fixtures";
import type { Page } from "@playwright/test";

test.skip(process.env.CQ_E2E !== "1", "CQ_E2E=1 with an isolated real backend world");

async function setOperations(page: Page, enabled: boolean) {
  const headers = { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_ADMIN_ID!)}` };
  const url = `${API_URL}/admin/ie/operations-grants/${process.env.CQ_E2E_STUDENT_ID!}`;
  const read = await page.request.get(url, { headers });
  expect(read.status()).toBe(200);
  const grant = await read.json() as { version: number; enabled: boolean };
  if (grant.enabled !== enabled) {
    const saved = await page.request.put(url, { headers, data: { version: grant.version, enabled, reason: "R1 导航测试授权与恢复" } });
    expect(saved.status()).toBe(200);
  }
}

test("R1: desktop public navigation is separate from personal management and permission-scoped operations", async ({ page }) => {
  await ensureStudentLogin(page);
  await setOperations(page, false);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${BASE_URL}/`);
  const navigation = page.getByRole("navigation", { name: "主导航", exact: true }).filter({ visible: true });
  await navigation.getByRole("link", { name: "创新创业", exact: true }).click();
  await expect(page.getByRole("heading", { name: "创新创业", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "校内已核实成果" })).toBeVisible();
  await expect(page.getByRole("region", { name: "双创三库" })).toContainText("人才库尚未开放");
  await expect(page.getByRole("link", { name: "双创运营工作台", exact: true })).toHaveCount(0);
  await page.goto(`${BASE_URL}/profile`);
  await expect(page.getByRole("link", { name: "我的项目", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "浏览校内成果", exact: true })).toHaveCount(0);
  await page.goto(`${BASE_URL}/innovation/reviews`);
  await expect(page.getByRole("alert").filter({ hasText: "尚未获" })).toBeVisible();
  const forbidden = await page.request.get(`${API_URL}/ie/ops/achievement-reviews`, { headers: { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_STUDENT_ID!)}` } });
  expect(forbidden.status()).toBe(403);
  await setOperations(page, true);
  await page.reload();
  const operations = page.getByRole("navigation", { name: "双创运营", exact: true });
  await expect(operations.getByRole("link", { name: "双创运营工作台", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
  await setOperations(page, false);
});

test("R1: five mobile destinations and full menu preserve browsing, rewards and operations reachability", async ({ page }) => {
  await ensureStudentLogin(page);
  await setOperations(page, true);
  for (const width of [320, 390, 800]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${BASE_URL}/innovation`);
    await expect(page.getByRole("heading", { name: "创新创业", exact: true })).toBeVisible();
    if (width < 640) {
      const bottom = page.getByRole("navigation", { name: "主导航", exact: true }).filter({ visible: true });
      await expect(bottom.getByRole("link")).toHaveCount(5);
      await expect(bottom.getByRole("link", { name: "创新创业", exact: true })).toHaveAttribute("aria-current", "page");
      await page.getByRole("button", { name: "全部导航", exact: true }).click();
      const menu = page.getByRole("dialog", { name: "全部导航", exact: true });
      await expect(menu.getByRole("link", { name: "双创运营工作台", exact: true })).toBeVisible();
      await menu.getByRole("link", { name: "积分奖励", exact: true }).click();
      await expect(page).toHaveURL(/\/rewards$/);
      await expect(menu).not.toBeVisible();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
  await page.goto(`${BASE_URL}/innovation`);
  await expect(page.getByRole("heading", { name: "创新创业", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "校内已核实成果" })).toBeVisible();
  await page.addStyleTag({ content: "*, *::before, *::after { animation: none !important; transition: none !important; }" });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await setOperations(page, false);
});

test("R1: staff workspaces reach active campus browsing without acquiring operations privileges", async ({ page }) => {
  for (const [env, start] of [["CQ_E2E_TEACHER", "/teacher/reviews"], ["CQ_E2E_ADMIN", "/admin/users"]] as const) {
    const secret = env === "CQ_E2E_TEACHER" ? process.env.CQ_E2E_TEACHER_TOTP_SECRET : process.env.CQ_E2E_ADMIN_TOTP_SECRET;
    await staffLogin(page, process.env[env]!, secret!);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${BASE_URL}${start}`);
    await page.getByRole("navigation").filter({ visible: true }).getByRole("link", { name: "创新创业", exact: true }).click();
    await expect(page.getByRole("region", { name: "校内已核实成果" })).toBeVisible();
    await expect(page.getByRole("link", { name: "双创运营工作台", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "返回工作台", exact: true })).toBeVisible();
  }
});

test("R1: real logout removes private forms across tabs, revokes refresh and allows another account", async ({ page }) => {
  await ensureStudentLogin(page);
  await page.goto(`${BASE_URL}/profile/project-drafts`);
  await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
  await page.getByLabel("项目名称", { exact: true }).fill("R1 前账号未保存的私密内容");
  const sibling = await page.context().newPage();
  try {
    await sibling.goto(`${BASE_URL}/profile/project-drafts`);
    await expect(sibling.getByRole("button", { name: "新建项目草稿", exact: true })).toBeVisible();
    await page.getByRole("link", { name: "退出登录", exact: true }).filter({ visible: true }).click();
    await expect(page).toHaveURL(/\/logout$/);
    await expect(page.getByLabel("项目名称", { exact: true })).toHaveCount(0);
    const revoked = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/auth/logout" && response.request().method() === "POST");
    await page.getByRole("button", { name: "确认退出登录", exact: true }).click();
    expect((await revoked).status()).toBe(204);
    await expect(page).toHaveURL(/\/login$/);
    await expect(sibling.getByRole("link", { name: "去登录", exact: true })).toBeVisible();
    expect((await page.context().cookies()).some((cookie) => cookie.name === "refresh_token")).toBe(false);
    await page.goBack();
    await expect(page.getByLabel("项目名称", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "去登录", exact: true })).toBeVisible();
    await page.goto(`${BASE_URL}/innovation/achievements`);
    await expect(page.getByRole("link", { name: "登录校内账号", exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("region", { name: "校内已核实成果" })).toHaveCount(0);
    await loginThroughUi(page, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
    await page.goto(`${BASE_URL}/profile/project-drafts`);
    await expect(page.getByText("R1 前账号未保存的私密内容", { exact: true })).toHaveCount(0);
    await page.goBack();
    await expect(page.getByText("R1 前账号未保存的私密内容", { exact: true })).toHaveCount(0);
    await loginThroughUi(page, parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"));
  } finally { await sibling.close(); }
});

test("R1: settings logout cannot report success after server failure or lost acknowledgement", async ({ page }) => {
  await ensureStudentLogin(page);
  await page.goto(`${BASE_URL}/profile?tab=info`);
  await page.getByRole("region", { name: "退出当前账号" }).getByRole("link", { name: "退出登录", exact: true }).click();
  let attempts = 0;
  await page.route("**/api/v1/auth/logout", async (route) => {
    attempts += 1;
    if (attempts === 1) await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { code: "SERVICE_UNAVAILABLE", message: "unavailable", request_id: "r1-logout-failed" } }) });
    else if (attempts === 2) { const committed = await route.fetch(); expect(committed.status()).toBe(204); await route.abort("connectionfailed"); }
    else await route.continue();
  });
  await page.getByRole("button", { name: "确认退出登录", exact: true }).click();
  await expect(page.getByRole("alert", { name: "退出结果" })).toContainText("退出尚未确认");
  await expect(page).toHaveURL(/\/logout$/);
  await expect(page.getByRole("link", { name: "返回", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "重试退出登录", exact: true }).click();
  await expect(page.getByRole("alert", { name: "退出结果" })).toContainText("退出尚未确认");
  await expect(page).toHaveURL(/\/logout$/);
  await page.getByRole("button", { name: "重试退出登录", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
});
