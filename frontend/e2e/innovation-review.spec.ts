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

test("achievement review: private proof and every update reviewed across three accounts", async ({ page, browser }, testInfo) => {
  test.setTimeout(300_000);
  await ensureStudentLogin(page);
  const ownerContext = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 390, height: 844 } });
  const viewerContext = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const owner = await ownerContext.newPage();
    const ownerIdentity = owner.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/me" && response.status() === 200);
    await loginThroughUi(owner, parseSeededAccount(process.env.CQ_E2E_AUTHOR_STUDENT, "CQ_E2E_AUTHOR_STUDENT"));
    const ownerId = (await (await ownerIdentity).json() as { id: string }).id;
    await owner.goto(`${BASE_URL}/profile/owner-profile`);
    const profile = owner.getByRole("region", { name: "负责人资料编辑器" });
    for (const [label, value] of Object.entries({ 姓名: "核实演示负责人", 学号: "00998877", 专业: "示例专业", 年级: "2026级" })) await profile.getByLabel(label, { exact: true }).fill(value);
    await profile.getByRole("button", { name: "保存负责人资料", exact: true }).click();
    await expect(owner.getByRole("status", { name: "负责人资料保存状态" })).toContainText("资料已保存");
    // Qualification/admin-grant UI is covered in its own specs. Use the
    // real APIs with the world's confirmed admin for setup here, rather
    // than cumulatively exhausting the real 10-logins/5-minute limiter.
    const adminHeaders = { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_ADMIN_ID!)}` };
    const qualification = owner.getByRole("status", { name: "负责人资格状态" });
    await expect(qualification).toBeVisible();
    if (!(await qualification.innerText()).includes("已开通")) {
      const applied = owner.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/v1/ie/me/owner-qualification" && response.status() === 200);
      await owner.getByRole("button", { name: "申请负责人资格", exact: true }).click();
      await expect(qualification).toContainText("等待管理员");
      const application = await (await applied).json() as { version: number };
      const approved = await owner.request.post(`${API_URL}/admin/ie/owner-qualifications/${ownerId}/approve`, { headers: adminHeaders, data: { version: application.version } });
      expect(approved.status()).toBe(200);
    }
    async function setOperator(enabled: boolean) {
      const path = `${API_URL}/admin/ie/operations-grants/${process.env.CQ_E2E_STUDENT_ID!}`;
      const current = await owner.request.get(path, { headers: adminHeaders }); expect(current.status()).toBe(200);
      const grant = await current.json() as { version: number; enabled: boolean };
      if (grant.enabled === enabled) return;
      const saved = await owner.request.put(path, { headers: adminHeaders, data: { version: grant.version, enabled, reason: "成果闭环测试准备与恢复" } });
      expect(saved.status()).toBe(200);
    }
    await setOperator(true);
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
    let releaseWorkflow!: () => void;
    const rereadGate = new Promise<void>((resolve) => { releaseWorkflow = resolve; });
    await owner.route("**/api/v1/ie/me/project-drafts/*/achievements/*/workflow", async (route) => {
      if (submissions.length === 1) await rereadGate;
      await route.continue();
    });
    await owner.route("**/api/v1/ie/me/project-drafts/*/achievements/*/submit", async (route) => {
      submissions.push(route.request().postDataJSON());
      if (submissions.length === 1) { const committed = await route.fetch(); expect(committed.status()).toBe(200); await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: "SERVICE_UNAVAILABLE", message: "upstream response lost", request_id: "review-lost-ack" }) }); }
      else await route.continue();
    });
    await owner.keyboard.press("Enter"); await confirm(owner, "提交首次核实");
    await expect(panel(owner).getByRole("button", { name: "确认上次提交结果（沿用原请求）", exact: true })).toBeFocused();
    await expect(owner.getByRole("button", { name: "确认上次提交结果（沿用原请求）", exact: true })).toBeVisible();
    try {
      // Retry must replay the frozen command even while the background read
      // is waiting. A visible, enabled retry must never silently discard a click.
      await owner.getByRole("button", { name: "确认上次提交结果（沿用原请求）", exact: true }).click();
      await expect.poll(() => submissions.length).toBe(2);
      expect(submissions[1]).toEqual(submissions[0]);
    } finally { releaseWorkflow(); }
    await expect(panel(owner)).toContainText("等待首次核实");
    await expect(panel(owner)).toBeFocused();
    await owner.unroute("**/api/v1/ie/me/project-drafts/*/achievements/*/workflow");
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
    await expect(page.getByRole("region", { name: "核实处理结果" })).toBeFocused();
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
    await expect(page.getByRole("region", { name: "核实处理结果" })).toBeFocused();
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
    const firstApprovedAt = await publicDetail.locator("time").first().getAttribute("datetime");
    const updates: unknown[] = [];
    await owner.route("**/api/v1/ie/me/project-drafts/*/achievements/*/submit-update", async (route) => {
      updates.push(route.request().postDataJSON());
      if (updates.length === 1) {
        const committed = await route.fetch(); expect(committed.status()).toBe(200);
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: "SERVICE_UNAVAILABLE", message: "update response lost", request_id: "update-lost-ack" }) });
      } else await route.continue();
    });
    await owner.getByRole("button", { name: "提交更新复审", exact: true }).click(); await confirm(owner, "提交更新复审");
    await expect(owner.getByRole("button", { name: "确认上次提交结果（沿用原请求）", exact: true })).toBeVisible();
    await owner.getByRole("button", { name: "确认上次提交结果（沿用原请求）", exact: true }).click();
    await expect.poll(() => updates.length).toBe(2); expect(updates[1]).toEqual(updates[0]);
    await owner.unroute("**/api/v1/ie/me/project-drafts/*/achievements/*/submit-update");
    await expect(panel(owner)).toContainText("等待更新复审");
    await expect(owner.getByLabel("成果名称", { exact: true })).toBeDisabled();
    async function stillOldPublic() {
      await viewer.reload(); await expect(publicDetail).toContainText("已补充第一次验证结果。");
      await expect(publicDetail).not.toContainText("第二版公开进展。");
      expect(await publicDetail.locator("time").first().getAttribute("datetime")).toBe(firstApprovedAt);
    }
    await stillOldPublic();
    await owner.getByRole("button", { name: "撤回更新复审", exact: true }).click(); await confirm(owner, "撤回更新复审");
    await expect(panel(owner)).toContainText("更新已撤回"); await stillOldPublic();
    await expect(owner.getByLabel("成果名称", { exact: true })).toBeEnabled();
    await owner.getByRole("button", { name: "提交更新复审", exact: true }).click(); await confirm(owner, "提交更新复审");
    await expect(panel(owner)).toContainText("等待更新复审");
    await page.getByRole("button", { name: "返回核实待办", exact: true }).click();
    await expect(queue).toContainText("更新复审");
    await page.getByRole("button", { name: "领取并核实", exact: true }).click();
    await expect(detail).toContainText("第二版公开进展。");
    await expect(detail).toContainText("更新复审：通过后才替换公开版本");
    await page.getByLabel("核实备注／退回原因", { exact: true }).fill("请补充第二版的验证依据。");
    await page.getByRole("button", { name: "退回修改", exact: true }).click(); await confirm(page, "退回成果修改");
    await expect(page.getByRole("status")).toContainText("更新已退回");
    await owner.getByRole("button", { name: "重新读取核实与材料状态", exact: true }).click();
    await expect(panel(owner)).toContainText("退回原因：请补充第二版的验证依据。"); await stillOldPublic();
    await owner.getByRole("button", { name: "提交更新复审", exact: true }).click(); await confirm(owner, "提交更新复审");
    await expect(panel(owner)).toContainText("等待更新复审");
    await page.getByRole("button", { name: "返回核实待办", exact: true }).click();
    await page.getByRole("button", { name: "领取并核实", exact: true }).click();
    await page.getByRole("button", { name: "通过更新复审", exact: true }).click(); await confirm(page, "通过更新复审");
    await expect(page.getByRole("status")).toContainText("核实通过");
    await viewer.reload(); await expect(publicDetail).toContainText("第二版公开进展。");
    await expect(publicDetail).toContainText("当前为更新复审通过的版本。"); await expect(publicDetail).not.toContainText("未逐项复审");
    expect(await publicDetail.locator("time").first().getAttribute("datetime")).toBe(firstApprovedAt);
    await expect(publicDetail).not.toContainText("00998877");
    await owner.getByRole("button", { name: "重新读取核实与材料状态", exact: true }).click();
    await expect(panel(owner)).toContainText("更新复审已通过");
    await page.getByRole("button", { name: "返回核实待办", exact: true }).click(); await expect(queue).toContainText("暂无可领取的成果");
    await auditAxe(viewer);
    expect(await owner.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await owner.screenshot({ path: testInfo.outputPath("achievement-review-owner-mobile.png"), fullPage: true });
    await viewer.screenshot({ path: testInfo.outputPath("achievement-public-desktop.png"), fullPage: true });
    // Restore the shared student grant so the existing operations suite retains its own starting contract.
    await setOperator(false);
  } finally { await ownerContext.close(); await viewerContext.close(); }
});
