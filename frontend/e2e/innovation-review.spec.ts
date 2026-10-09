import AxeBuilder from "@axe-core/playwright";
import { BASE_URL, API_URL, ensureStudentLogin, expect, loginThroughUi, mintToken, parseSeededAccount, staffLogin, test } from "./fixtures";
import type { Page } from "@playwright/test";

test.skip(process.env.CQ_E2E !== "1", "set CQ_E2E=1 with real PG, MinIO and ClamAV");
const panel = (page: Page) => page.getByRole("region", { name: "成果核实与证明" });
async function confirm(page: Page, title: string) { await page.getByRole("dialog", { name: title, exact: true }).getByRole("button", { name: "确认", exact: true }).click(); }
async function auditAxe(page: Page) {
  // Ignore entrance motion, not accessibility rules; the real content stays rendered.
  await page.addStyleTag({ content: "*, *::before, *::after { animation: none !important; transition: none !important; }" });
  const findings = await new AxeBuilder({ page }).analyze();
  expect(findings.violations.map((violation) => ({ id: violation.id, targets: violation.nodes.map((node) => node.target) }))).toEqual([]);
}

test("achievement review: real private upload, withdrawal, return, approval and explicit update across three accounts", async ({ page, browser }, testInfo) => {
  test.setTimeout(180_000);
  await ensureStudentLogin(page);
  const ownerContext = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 390, height: 844 } });
  const adminContext = await browser.newContext({ ignoreHTTPSErrors: true });
  const viewerContext = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const owner = await ownerContext.newPage();
    await loginThroughUi(owner, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
    await owner.goto(`${BASE_URL}/profile/owner-profile`);
    const profile = owner.getByRole("region", { name: "负责人资料编辑器" });
    for (const [label, value] of Object.entries({ 姓名: "核实演示负责人", 学号: "00998877", 专业: "示例专业", 年级: "2026级" })) await profile.getByLabel(label, { exact: true }).fill(value);
    await profile.getByRole("button", { name: "保存负责人资料", exact: true }).click();
    await expect(owner.getByRole("status", { name: "负责人资料保存状态" })).toContainText("资料已保存");
    const admin = await adminContext.newPage();
    await staffLogin(admin, process.env.CQ_E2E_ADMIN!, process.env.CQ_E2E_ADMIN_TOTP_SECRET!);
    const qualification = owner.getByRole("status", { name: "负责人资格状态" });
    await expect(qualification).toBeVisible();
    if (!(await qualification.innerText()).includes("已开通")) {
      await owner.getByRole("button", { name: "申请负责人资格", exact: true }).click();
      await expect(qualification).toContainText("等待管理员");
      await admin.goto(`${BASE_URL}/admin/owner-qualifications`);
      await admin.getByRole("button", { name: "查看申请", exact: true }).click();
      await admin.getByRole("button", { name: "开通负责人资格", exact: true }).click();
      await admin.getByRole("button", { name: "确认开通", exact: true }).click();
      await expect(admin.getByRole("status", { name: "管理员资格操作结果" })).toContainText("已开通");
    }
    await admin.goto(`${BASE_URL}/admin/innovation-operations`);
    const username = parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT").username;
    const select = admin.getByLabel("授权对象", { exact: true });
    await expect(select).toBeVisible();
    const option = select.getByRole("option").filter({ hasText: `${username} ·` });
    await expect(option).toHaveCount(1);
    await select.selectOption(await option.getAttribute("value") as string);
    await expect(admin.getByRole("region", { name: "当前账号运营授权" })).toBeVisible();
    if (await admin.getByRole("button", { name: "授予运营身份", exact: true }).count()) {
      await admin.getByLabel("操作原因", { exact: true }).fill("成果闭环核实演示");
      await admin.getByRole("button", { name: "授予运营身份", exact: true }).click();
      await expect(admin.getByText("已授予双创运营身份。", { exact: true })).toBeVisible();
    }
    await owner.goto(`${BASE_URL}/profile/project-drafts`);
    await owner.getByRole("button", { name: "新建项目草稿", exact: true }).click();
    for (const [label, value] of Object.entries({ 项目名称: "真实核实闭环项目", 项目简介: "校园回收调研和可操作原型。", 项目方向: "环境保护", 项目阶段: "原型验证", 团队现状: "两名同学负责设计和调研。" })) await owner.getByLabel(label, { exact: true }).fill(value);
    await owner.getByRole("button", { name: "保存草稿", exact: true }).click();
    await expect(owner.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
    await owner.getByRole("link", { name: "管理成果草稿", exact: true }).click();
    await owner.getByRole("button", { name: "新建成果草稿", exact: true }).click();
    await owner.getByLabel("成果名称", { exact: true }).fill("校园回收核实原型");
    await owner.getByLabel("作品与阶段成果说明", { exact: true }).fill("首次提交的可操作原型。");
    const created = owner.waitForResponse((response) => response.request().method() === "POST" && /\/achievements$/.test(new URL(response.url()).pathname));
    await owner.getByRole("button", { name: "保存成果草稿", exact: true }).click();
    const achievement = await (await created).json() as { id: string; project_id: string };
    await expect(panel(owner)).toContainText("尚未提交首次核实");
    // A failed signed PUT keeps its original retry key, but must not trap the
    // author after the upload intent is explicitly removed.
    await owner.route("**:9002/**", async (route) => {
      if (route.request().method() === "PUT") await route.fulfill({ status: 403, body: "upload rejected" });
      else await route.continue();
    });
    await owner.getByLabel("上传证明材料", { exact: true }).setInputFiles({ name: "retry.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%%EOF\n") });
    await expect(owner.getByRole("button", { name: "重试原文件上传与检查", exact: true })).toBeVisible();
    await expect(owner.getByLabel("上传证明材料", { exact: true })).toBeDisabled();
    await owner.getByRole("button", { name: "移除证明 1", exact: true }).click();
    await expect(panel(owner)).toContainText("还没有证明材料");
    await expect(owner.getByRole("button", { name: "放弃本次上传重试", exact: true })).toBeVisible();
    await owner.getByRole("button", { name: "放弃本次上传重试", exact: true }).click();
    await expect(owner.getByLabel("上传证明材料", { exact: true })).toBeEnabled();
    await expect(owner.getByRole("button", { name: "重试原文件上传与检查", exact: true })).toHaveCount(0);
    await owner.unroute("**:9002/**");
    await owner.getByLabel("上传证明材料", { exact: true }).setInputFiles({ name: "wrong.pdf", mimeType: "application/pdf", buffer: Buffer.from("this is not a PDF") });
    await expect(panel(owner)).toContainText("文件检查未通过");
    await owner.getByRole("button", { name: "移除证明 1", exact: true }).click();
    await expect(panel(owner)).toContainText("还没有证明材料");
    const proofResponse = owner.waitForResponse((response) => response.request().method() === "POST" && /\/evidence\/[^/]+\/complete$/.test(new URL(response.url()).pathname) && response.status() === 200);
    const pdf = Buffer.from("%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n");
    await owner.getByLabel("上传证明材料", { exact: true }).setInputFiles({ name: "proof.pdf", mimeType: "application/pdf", buffer: pdf });
    const proof = await (await proofResponse).json() as { id: string };
    await expect(panel(owner).getByRole("checkbox", { name: "选择证明 1" })).toBeEnabled();
    await panel(owner).getByRole("checkbox", { name: "选择证明 1" }).check();
    await owner.getByRole("button", { name: "提交首次核实", exact: true }).focus(); await owner.keyboard.press("Enter");
    await expect(owner.getByRole("dialog", { name: "提交首次核实", exact: true })).toBeVisible();
    await owner.keyboard.press("Escape");
    await expect(owner.getByRole("button", { name: "提交首次核实", exact: true })).toBeFocused();
    const submissions: unknown[] = [];
    await owner.route("**/api/v1/ie/me/project-drafts/*/achievements/*/submit", async (route) => {
      submissions.push(route.request().postDataJSON());
      if (submissions.length === 1) { const committed = await route.fetch(); expect(committed.status()).toBe(200); await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: "SERVICE_UNAVAILABLE", message: "upstream response lost", request_id: "review-lost-ack" }) }); }
      else await route.continue();
    });
    await owner.keyboard.press("Enter"); await confirm(owner, "提交首次核实");
    await expect(owner.getByRole("button", { name: "确认上次提交结果（沿用原请求）", exact: true })).toBeVisible();
    await owner.getByRole("button", { name: "确认上次提交结果（沿用原请求）", exact: true }).click();
    await expect(panel(owner)).toContainText("等待首次核实");
    await expect.poll(() => submissions.length).toBe(2); expect(submissions[1]).toEqual(submissions[0]);
    await owner.unroute("**/api/v1/ie/me/project-drafts/*/achievements/*/submit");
    await expect(owner.getByLabel("成果名称", { exact: true })).toBeDisabled();
    await owner.getByRole("button", { name: "撤回首次核实", exact: true }).click(); await confirm(owner, "撤回首次核实");
    await expect(panel(owner)).toContainText("尚未提交首次核实");
    await expect(owner.getByLabel("成果名称", { exact: true })).toBeEnabled();
    await owner.getByRole("button", { name: "提交首次核实", exact: true }).click(); await confirm(owner, "提交首次核实");
    await expect(panel(owner)).toContainText("等待首次核实");
    const viewer = await viewerContext.newPage();
    await staffLogin(viewer, process.env.CQ_E2E_TEACHER!, process.env.CQ_E2E_TEACHER_TOTP_SECRET!);
    await viewer.goto(`${BASE_URL}/innovation/achievements`);
    await expect(viewer.getByRole("region", { name: "校内已核实成果" })).not.toContainText("校园回收核实原型");
    const token = mintToken(process.env.CQ_E2E_STAFF_ID ?? process.env.CQ_E2E_TEACHER_ID!);
    const denied = await viewer.request.get(`${API_URL}/ie/me/project-drafts/${achievement.project_id}/achievements/${achievement.id}/evidence/${proof.id}/content`, { headers: { Authorization: `Bearer ${token}` } });
    expect(denied.status()).toBe(403);
    await page.goto(`${BASE_URL}/innovation/reviews`);
    const queue = page.getByRole("region", { name: "成果核实待办" });
    await expect(queue).toContainText("校园回收核实原型");
    await expect(queue).not.toContainText("00998877");
    await page.getByRole("button", { name: "领取并核实", exact: true }).click();
    const detail = page.getByRole("region", { name: "成果核实快照" });
    await expect(detail).toContainText("00998877");
    const sibling = await page.context().newPage();
    try {
      await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
      await expect(page.getByText("尚未获管理员指定的双创运营授权。", { exact: true })).toBeVisible();
      await expect(detail).toHaveCount(0); await expect(page.locator("body")).not.toContainText("00998877");
      await loginThroughUi(sibling, parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"));
      await expect(queue).toContainText("校园回收核实原型");
      await page.getByRole("button", { name: "查看核实快照", exact: true }).click();
      await expect(detail).toContainText("00998877");
    } finally { await sibling.close(); }
    const download = page.waitForEvent("download"); await page.getByRole("button", { name: "下载核实证明 1", exact: true }).click();
    expect((await download).suggestedFilename()).toBe("evidence.pdf");
    await auditAxe(page); await auditAxe(owner);
    await page.getByLabel("核实备注／退回原因", { exact: true }).fill("请补充原型验证结果。");
    await page.getByRole("button", { name: "退回修改", exact: true }).click(); await confirm(page, "退回成果修改");
    await expect(page.getByRole("status")).toContainText("已退回");
    await owner.getByRole("button", { name: "重新读取核实与材料状态", exact: true }).click();
    await expect(panel(owner)).toContainText("退回原因：请补充原型验证结果。");
    await owner.getByLabel("作品与阶段成果说明", { exact: true }).fill("已补充第一次验证结果。");
    await owner.getByRole("button", { name: "保存成果草稿", exact: true }).click();
    await expect(owner.getByRole("status").first()).toContainText("成果草稿已保存");
    await owner.getByRole("button", { name: "提交首次核实", exact: true }).click(); await confirm(owner, "提交首次核实");
    await expect(panel(owner)).toContainText("等待首次核实");
    await page.getByRole("button", { name: "返回核实待办", exact: true }).click();
    await page.getByRole("button", { name: "领取并核实", exact: true }).click();
    await expect(detail).toContainText("已补充第一次验证结果。");
    const decisions: unknown[] = [];
    await page.route("**/api/v1/ie/ops/achievement-reviews/*/decision", async (route) => {
      decisions.push(route.request().postDataJSON());
      if (decisions.length === 1) { const committed = await route.fetch(); expect(committed.status()).toBe(200); await route.abort("failed"); }
      else await route.continue();
    });
    await page.getByRole("button", { name: "通过首次核实", exact: true }).click(); await confirm(page, "通过首次核实");
    await expect(page.getByRole("button", { name: "确认上次决定结果（沿用原请求）", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "确认上次决定结果（沿用原请求）", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("核实通过");
    await expect.poll(() => decisions.length).toBe(2); expect(decisions[1]).toEqual(decisions[0]);
    await page.unroute("**/api/v1/ie/ops/achievement-reviews/*/decision");
    await viewer.goto(`${BASE_URL}/innovation/achievements/${achievement.id}`);
    const publicDetail = viewer.getByRole("article", { name: "校内成果详情" });
    await expect(publicDetail).toContainText("已补充第一次验证结果。");
    await expect(publicDetail).not.toContainText("00998877"); await expect(publicDetail).not.toContainText("核实演示负责人");
    await expect(publicDetail.getByRole("button", { name: /证明/ })).toHaveCount(0);
    await owner.getByRole("button", { name: "重新读取核实与材料状态", exact: true }).click();
    await expect(panel(owner)).toContainText("首次核实已通过");
    await owner.getByLabel("作品与阶段成果说明", { exact: true }).fill("第二版公开进展。");
    await owner.getByRole("button", { name: "保存成果草稿", exact: true }).click();
    await expect(owner.getByRole("status").first()).toContainText("成果草稿已保存");
    await viewer.reload(); await expect(publicDetail).toContainText("已补充第一次验证结果。"); await expect(publicDetail).not.toContainText("第二版公开进展。");
    await owner.getByRole("button", { name: "发布更新（免复审）", exact: true }).click(); await confirm(owner, "发布更新");
    await expect(panel(owner).getByRole("status")).toContainText("更新已发布");
    await viewer.reload(); await expect(publicDetail).toContainText("第二版公开进展。"); await expect(publicDetail).toContainText("未逐项复审");
    await page.getByRole("button", { name: "返回核实待办", exact: true }).click(); await expect(queue).toContainText("暂无可领取的成果");
    await auditAxe(viewer);
    expect(await owner.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await owner.screenshot({ path: testInfo.outputPath("achievement-review-owner-mobile.png"), fullPage: true });
    await viewer.screenshot({ path: testInfo.outputPath("achievement-public-desktop.png"), fullPage: true });
    // Restore the shared student grant so the existing operations suite retains its own starting contract.
    await admin.getByLabel("操作原因", { exact: true }).fill("闭环演示结束撤回授权");
    await admin.getByRole("button", { name: "撤回运营身份", exact: true }).click();
    await admin.getByRole("button", { name: "确认撤回", exact: true }).click();
  } finally { await ownerContext.close(); await adminContext.close(); await viewerContext.close(); }
});
