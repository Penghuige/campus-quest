import AxeBuilder from "@axe-core/playwright";
import { API_URL, BASE_URL, ensureStudentLogin, expect, mintToken, test, loginThroughUi, parseSeededAccount, staffLogin } from "./fixtures";
import type { Page } from "@playwright/test";

test.skip(process.env.CQ_E2E !== "1", "CQ_E2E=1 with an owned real browser database");

async function prepare(page: Page) {
  await ensureStudentLogin(page);
  await page.goto(`${BASE_URL}/profile/owner-profile`);
  const editor = page.getByRole("region", { name: "负责人资料编辑器" });
  for (const [label, value] of Object.entries({ 姓名: "模拟招募负责人", 学号: "00123456", 专业: "模拟专业", 年级: "2026级" })) await editor.getByLabel(label, { exact: true }).fill(value);
  await editor.getByRole("button", { name: "保存负责人资料", exact: true }).click();
  await expect(editor.getByRole("status", { name: "负责人资料保存状态" })).toContainText("资料已保存");
  const state = page.getByRole("status", { name: "负责人资格状态" });
  await expect(state).toBeVisible();
  if (!(await state.innerText()).includes("已开通")) {
    const applied = page.waitForResponse((r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/v1/ie/me/owner-qualification" && r.status() === 200);
    await page.getByRole("button", { name: "申请负责人资格", exact: true }).click();
    const application = await (await applied).json();
    const approval = await page.request.post(`${API_URL}/admin/ie/owner-qualifications/${process.env.CQ_E2E_STUDENT_ID}/approve`, { headers: { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_ADMIN_ID!)}` }, data: { version: application.version } });
    expect(approval.status()).toBe(200);
  }
  const project = await page.request.post(`${API_URL}/ie/me/project-drafts`, { headers: { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_STUDENT_ID!)}` }, data: { request_id: crypto.randomUUID(), title: `模拟招募项目 ${Date.now()}`, summary: "PRIVATE_PARENT_SENTINEL", direction: "", stage: "", team_status: "" } });
  expect(project.status()).toBe(201);
  const parent = await project.json();
  await page.goto(`${BASE_URL}/profile/project-drafts/${parent.id}/recruitments`);
  return parent.id as string;
}
async function fillRole(page: Page) {
  await page.getByRole("button", { name: "新建招募岗位", exact: true }).click();
  const editor = page.getByRole("region", { name: "招募岗位编辑器" });
  for (const [label, value] of Object.entries({ "项目名称": "模拟学习实验", "项目方向": "学习", "项目阶段": "构想", "项目介绍": "模拟资料：访谈项目", "阶段目标": "验证学习方法", "初步方案": "先做访谈", "岗位名称": "研究伙伴", "招募人数": "1", "主要职责": "参与访谈", "能力要求": "愿意学习", "时间投入": "每周两小时" })) await editor.getByLabel(label, { exact: true }).fill(value);
  await editor.getByLabel("创意起步", { exact: true }).selectOption("true");
  await editor.getByLabel("零基础友好", { exact: true }).selectOption("true");
  await editor.getByLabel("授权微信", { exact: true }).check();
  await editor.getByLabel("微信", { exact: true }).fill("demo_partner");
  return editor;
}
async function publish(page: Page) {
  await page.getByRole("button", { name: "预览发布招募", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "发布招募", exact: true });
  await expect(dialog).toContainText("独立免审");
  await dialog.getByRole("button", { name: "确认", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("正在招募");
}

test("R3: independent publication, private saves, deliberate contact grant and last revoke", async ({ page }) => {
  const project = await prepare(page);
  const editor = await fillRole(page);
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  await publish(page);
  await expect(page.getByRole("status", { name: "招募操作结果" })).toBeFocused();
  const publicUrl = await editor.getByRole("link", { name: "查看校内岗位", exact: true }).getAttribute("href");
  expect(publicUrl).toMatch(/\/innovation\/recruitments\//);
  await editor.getByLabel("岗位名称", { exact: true }).fill("PRIVATE_ROLE_SENTINEL");
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  const browsing = await page.context().newPage();
  try {
    let reads = 0;
    browsing.on("request", (r) => { if (new URL(r.url()).pathname.endsWith("/contacts")) reads++; });
    await browsing.goto(`${BASE_URL}${publicUrl}`);
    await expect(browsing.getByRole("heading", { name: "研究伙伴", exact: true })).toBeVisible();
    await expect(browsing.locator("body")).not.toContainText("PRIVATE_ROLE_SENTINEL");
    await expect(browsing.locator("body")).not.toContainText("PRIVATE_PARENT_SENTINEL");
    await expect(browsing.locator("body")).not.toContainText("demo_partner");
    expect(reads).toBe(0);
    await browsing.getByRole("button", { name: "查看联系方式", exact: true }).click();
    await expect(browsing.getByRole("region", { name: "授权联系方式" })).toContainText("demo_partner");
    expect(reads).toBe(1);
    await editor.getByRole("button", { name: "撤销联系授权：微信", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "撤销最后一项联系授权", exact: true });
    await expect(dialog).toContainText("同时关闭");
    await dialog.getByRole("button", { name: "确认", exact: true }).click();
    await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已关闭");
    await expect(editor.getByLabel("授权微信", { exact: true })).not.toBeChecked();
    await browsing.reload();
    await expect(browsing.getByRole("alert")).toBeVisible();
    const projectRead = await browsing.request.get(`${API_URL}/ie/projects/${project}`, { headers: { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_STUDENT_ID!)}` } });
    expect(projectRead.status()).toBe(404);
  } finally { await browsing.close(); }
});

test("R3: campus recruitment entry has accessible empty/mobile content", async ({ page }) => {
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE_URL}/innovation/recruitments`);
  await expect(page.getByRole("heading", { name: "正在招募", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.addStyleTag({ content: "*,*::before,*::after{animation:none!important;transition:none!important}" });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("R3: dated publication and maximum unbroken content fit mobile", async ({ page }) => {
  await prepare(page); const editor = await fillRole(page);
  await editor.getByLabel("项目名称", { exact: true }).fill("P".repeat(120));
  await editor.getByLabel("岗位名称", { exact: true }).fill("R".repeat(120));
  await editor.getByLabel("项目方向", { exact: true }).fill("D".repeat(120));
  await editor.getByLabel("项目阶段", { exact: true }).fill("S".repeat(80));
  await editor.getByLabel("项目介绍", { exact: true }).fill("A".repeat(2000));
  await editor.getByLabel("截止时间", { exact: true }).fill("2099-10-11T18:30");
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  await publish(page);
  await expect(editor.getByRole("button", { name: "确认继续招募", exact: true })).toHaveCount(0);
  const href = await editor.getByRole("link", { name: "查看校内岗位", exact: true }).getAttribute("href");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE_URL}${href}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("R".repeat(120));
  await expect(page.getByRole("article", { name: "校内招募详情" }).locator("time")).toHaveAttribute("datetime", "2099-10-11T10:30:00Z");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("link", { name: "P".repeat(120), exact: true }).click();
  await expect(page.getByRole("article", { name: "校内项目详情" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("R3: successful response loss retries the exact command and two tabs reconcile", async ({ page }) => {
  test.setTimeout(120_000);
  const project = await prepare(page);
  const editor = await fillRole(page);
  const bodies: string[] = [];
  let lost = false;
  await page.route(`**/api/v1/ie/me/project-drafts/${project}/recruitments`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    bodies.push(route.request().postData()!);
    const response = await route.fetch();
    if (!lost) { lost = true; expect(response.status()).toBe(201); await route.abort("failed"); }
    else await route.fulfill({ response });
  });
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(editor.getByRole("alert")).toContainText("结果尚未确认");
  await expect(editor.getByLabel("岗位名称", { exact: true })).toBeDisabled();
  await editor.getByRole("button", { name: "重试原请求", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  expect(bodies).toHaveLength(2); expect(bodies[1]).toBe(bodies[0]);
  await page.unroute(`**/api/v1/ie/me/project-drafts/${project}/recruitments`);
  await publish(page);
  const sibling = await page.context().newPage();
  try {
    await sibling.goto(`${BASE_URL}/profile/project-drafts/${project}/recruitments`);
    await sibling.getByRole("button", { name: "编辑招募：研究伙伴", exact: true }).click();
    await sibling.getByLabel("岗位名称", { exact: true }).fill("另一标签页的内容");
    await sibling.getByRole("button", { name: "保存招募草稿", exact: true }).click();
    await expect(sibling.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
    await editor.getByLabel("岗位名称", { exact: true }).fill("本页输入应保留");
    await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
    await expect(editor.getByRole("alert")).toContainText("版本或状态已改变");
    await expect(editor.getByLabel("岗位名称", { exact: true })).toHaveValue("本页输入应保留");
    await editor.getByRole("button", { name: "读取最新版本（保留当前输入）", exact: true }).click();
    await expect(editor.getByRole("region", { name: "最新招募版本" })).toContainText("另一标签页的内容");
    await expect(editor.getByLabel("岗位名称", { exact: true })).toHaveValue("本页输入应保留");
    await editor.getByRole("button", { name: "载入最新版本", exact: true }).click();
    await expect(editor.getByLabel("岗位名称", { exact: true })).toHaveValue("另一标签页的内容");
  } finally { await sibling.close(); }
});

test("R3: publication ACK loss, update, close/reopen and deliberate fresh consent", async ({ page }) => {
  test.setTimeout(120_000);
  const project = await prepare(page); const editor = await fillRole(page);
  await editor.getByLabel("授权微信", { exact: true }).uncheck();
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  await editor.getByRole("button", { name: "预览发布招募", exact: true }).click();
  await expect(editor.getByRole("alert")).toContainText("至少选择");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const ownerHeaders = { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_STUDENT_ID!)}` };
  const beforeConsent = await (await page.request.get(`${API_URL}/ie/me/project-drafts/${project}/recruitments`, { headers: ownerHeaders })).json();
  const deniedPublication = await page.request.post(`${API_URL}/ie/me/project-drafts/${project}/recruitments/${beforeConsent.items[0].id}/publish`, { headers: ownerHeaders, data: { version: beforeConsent.items[0].version, request_id: crypto.randomUUID() } });
  expect(deniedPublication.status()).toBe(422);
  await editor.getByLabel("授权微信", { exact: true }).check();
  await editor.getByLabel("微信", { exact: true }).fill("demo_partner");
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  const bodies: string[] = []; let lost = false;
  await page.route("**/recruitments/*/publish", async (route) => {
    bodies.push(route.request().postData()!); const response = await route.fetch();
    if (!lost) { lost = true; expect(response.status()).toBe(200); await route.abort("failed"); } else await route.fulfill({ response });
  });
  await editor.getByRole("button", { name: "预览发布招募", exact: true }).click();
  const publication = page.getByRole("dialog", { name: "发布招募", exact: true });
  await publication.getByRole("button", { name: "确认", exact: true }).click();
  await expect(publication.getByRole("alert")).toContainText("结果尚未确认");
  await publication.getByRole("button", { name: "重试原请求", exact: true }).click();
  await expect(publication).not.toBeVisible();
  expect(bodies).toHaveLength(2); expect(bodies[1]).toBe(bodies[0]);
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("当前正在招募");
  await page.unroute("**/recruitments/*/publish");
  await editor.getByLabel("岗位名称", { exact: true }).fill("更新后的模拟岗位");
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  await editor.getByRole("button", { name: "发布招募更新", exact: true }).click();
  await page.getByRole("dialog", { name: "发布招募", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("正在招募");
  await editor.getByRole("button", { name: "关闭招募", exact: true }).click();
  await page.getByRole("dialog", { name: "关闭招募", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已关闭");
  await editor.getByRole("button", { name: "重新开放招募", exact: true }).click();
  await page.getByRole("dialog", { name: "重新开放招募", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("正在招募");
  await editor.getByRole("button", { name: "撤销联系授权：微信", exact: true }).click();
  await page.getByRole("dialog", { name: "撤销最后一项联系授权", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已关闭");
  await expect(editor.getByLabel("授权微信", { exact: true })).not.toBeChecked();
  await editor.getByRole("button", { name: "重新开放招募", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const list = await page.request.get(`${API_URL}/ie/me/project-drafts/${project}/recruitments`, { headers: { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_STUDENT_ID!)}` } });
  expect((await list.json()).total).toBe(1);
  await editor.getByLabel("授权微信", { exact: true }).check();
  await editor.getByLabel("微信", { exact: true }).fill("demo_newpartner");
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  await editor.getByRole("button", { name: "重新开放招募", exact: true }).click();
  const reopened = page.getByRole("dialog", { name: "重新开放招募", exact: true });
  await expect(reopened).toContainText("授权微信：demo_newpartner");
  await reopened.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("正在招募");
  await editor.getByRole("button", { name: "确认继续招募", exact: true }).click();
  const confirmed = page.getByRole("dialog", { name: "确认继续招募", exact: true });
  await expect(confirmed).toContainText("30 天后");
  await confirmed.getByRole("button", { name: "确认", exact: true }).click();
  await expect(confirmed).not.toBeVisible();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toBeFocused();
});

test("R3: 503 retains payload; logout discards private editor input", async ({ page }) => {
  test.setTimeout(120_000);
  const project = await prepare(page); const editor = await fillRole(page);
  const bodies: string[] = []; let failed = false;
  await page.route(`**/api/v1/ie/me/project-drafts/${project}/recruitments`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    bodies.push(route.request().postData()!);
    if (!failed) { failed = true; await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { code: "INTERNAL_ERROR", message: "temporary" } }) }); }
    else await route.continue();
  });
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(editor.getByRole("alert")).toContainText("结果尚未确认");
  await editor.getByRole("button", { name: "重试原请求", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存");
  expect(bodies[1]).toBe(bodies[0]);
  await page.unroute(`**/api/v1/ie/me/project-drafts/${project}/recruitments`);
  await editor.getByLabel("项目介绍", { exact: true }).fill("PRIVATE_ACCOUNT_SENTINEL");
  page.once("dialog", (dialog) => dialog.accept());
  await page.goto(`${BASE_URL}/logout`);
  await page.getByRole("button", { name: "确认退出登录", exact: true }).click();
  await expect(page).toHaveURL(/\/login/);
  await loginThroughUi(page, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
  await page.goto(`${BASE_URL}/profile/project-drafts/${project}/recruitments`);
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("PRIVATE_ACCOUNT_SENTINEL");
  await expect(page.getByRole("region", { name: "招募岗位编辑器" })).toHaveCount(0);
});

test("R3: real student viewer and staff cannot manage another owner's project", async ({ page, browser }) => {
  test.setTimeout(120_000);
  const project = await prepare(page); const editor = await fillRole(page);
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存"); await publish(page);
  const href = await editor.getByRole("link", { name: "查看校内岗位", exact: true }).getAttribute("href");
  const viewerContext = await browser.newContext({ ignoreHTTPSErrors: true });
  const staffContext = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const viewer = await viewerContext.newPage();
    await loginThroughUi(viewer, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
    await viewer.goto(`${BASE_URL}${href}`);
    await expect(viewer.getByRole("heading", { name: "研究伙伴", exact: true })).toBeVisible();
    await expect(viewer.locator("body")).not.toContainText("demo_partner");
    await viewer.getByRole("button", { name: "查看联系方式", exact: true }).click();
    await expect(viewer.getByRole("region", { name: "授权联系方式" })).toContainText("demo_partner");
    await viewer.goto(`${BASE_URL}/profile/project-drafts/${project}/recruitments`);
    await expect(viewer.getByRole("alert")).toBeVisible();
    await expect(viewer.getByRole("button", { name: "新建招募岗位", exact: true })).toHaveCount(0);
    const staff = await staffContext.newPage(); await staffLogin(staff, process.env.CQ_E2E_ADMIN!, process.env.CQ_E2E_ADMIN_TOTP_SECRET!);
    await staff.goto(`${BASE_URL}/innovation/projects/${project}`);
    await expect(staff.getByRole("article", { name: "校内项目详情" })).toContainText("负责人发布的招募版本（免审）");
    await expect(staff.locator("body")).not.toContainText("PRIVATE_PARENT_SENTINEL");
    await staff.goto(`${BASE_URL}/profile/project-drafts/${project}/recruitments`);
    await expect(staff.getByRole("button", { name: "新建招募岗位", exact: true })).toHaveCount(0);
    const mutation = await staff.request.post(`${API_URL}/ie/me/project-drafts/${project}/recruitments`, { headers: { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_ADMIN_ID!)}` }, data: { request_id: crypto.randomUUID(), fields: {} } });
    expect(mutation.status()).toBe(403);
  } finally { await viewerContext.close(); await staffContext.close(); }
});

test("R3: a late contact response cannot repopulate after cross-tab logout", async ({ page }) => {
  test.setTimeout(120_000);
  await prepare(page); const editor = await fillRole(page);
  await editor.getByRole("button", { name: "保存招募草稿", exact: true }).click();
  await expect(page.getByRole("status", { name: "招募操作结果" })).toContainText("已保存"); await publish(page);
  const href = await editor.getByRole("link", { name: "查看校内岗位", exact: true }).getAttribute("href");
  await page.goto(`${BASE_URL}${href}`);
  await expect(page.getByRole("article", { name: "校内招募详情" })).toBeVisible();
  let release!: () => void; let fetched!: () => void;
  const hold = new Promise<void>((resolve) => { release = resolve; });
  const received = new Promise<void>((resolve) => { fetched = resolve; });
  await page.route("**/recruitments/*/contacts", async (route) => {
    const response = await route.fetch(); expect(response.status()).toBe(200);
    expect((await response.json()).contacts[0].value).toBe("demo_partner");
    fetched(); await hold;
    try { await route.fulfill({ response }); } catch { /* Logout may already have destroyed the requesting page. */ }
  });
  await page.getByRole("button", { name: "查看联系方式", exact: true }).click();
  await received;
  const other = await page.context().newPage();
  try {
    await other.goto(`${BASE_URL}/logout`);
    await other.getByRole("button", { name: "确认退出登录", exact: true }).click();
    await expect(other).toHaveURL(/\/login/);
    await expect(page.getByRole("link", { name: "登录校内账号", exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "授权联系方式" })).toHaveCount(0);
    release();
    await loginThroughUi(other, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
    await page.goto(`${BASE_URL}/profile`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.locator("body")).not.toContainText("demo_partner");
    await expect(page.getByRole("region", { name: "授权联系方式" })).toHaveCount(0);
  } finally { release(); await other.close(); }
});
