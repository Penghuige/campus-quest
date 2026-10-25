import AxeBuilder from "@axe-core/playwright";
import { randomUUID } from "node:crypto";
import { API_URL, BASE_URL, ensureStudentLogin, expect, mintToken, test } from "./fixtures";

test.skip(process.env.CQ_E2E !== "1" || process.env.CQ_VISUAL !== "1" || process.env.CQ_E2E_FIXED_LABELS !== "1", "CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1 pins Linux fonts and disposable labels");
test.use({ viewport: { width: 1440, height: 900 } });

test("achievement review visual: owner proof workspace, operator queue and campus empty list", async ({ page }) => {
  test.setTimeout(120_000);
  const studentId = process.env.CQ_E2E_STUDENT_ID!;
  const studentHeaders = { Authorization: `Bearer ${mintToken(studentId)}` };
  const adminHeaders = { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_ADMIN_ID!)}` };
  // Setup through real APIs. Only screen stability is tested here; the separate
  // functional spec owns UI approval, upload, withdrawal and decision behavior.
  const profile = await page.request.put(`${API_URL}/ie/me/owner-profile`, { headers: studentHeaders, data: { name: "视觉示例负责人", student_no: "00889900", major: "示例专业", grade: "2026级", version: 0 } });
  expect(profile.status()).toBe(200);
  const savedProfile = await profile.json() as { version: number };
  const applied = await page.request.post(`${API_URL}/ie/me/owner-qualification`, { headers: studentHeaders, data: { version: 0, profile_version: savedProfile.version } });
  expect(applied.status()).toBe(200);
  const application = await applied.json() as { version: number };
  const approved = await page.request.post(`${API_URL}/admin/ie/owner-qualifications/${studentId}/approve`, { headers: adminHeaders, data: { version: application.version } });
  expect(approved.status()).toBe(200);
  const grant = await page.request.put(`${API_URL}/admin/ie/operations-grants/${studentId}`, { headers: adminHeaders, data: { version: 0, enabled: true, reason: "独立视觉世界运营身份" } });
  expect(grant.status()).toBe(200);
  await ensureStudentLogin(page);
  await page.goto(`${BASE_URL}/profile/project-drafts`);
  await page.getByRole("button", { name: "新建项目草稿", exact: true }).click();
  for (const [label, value] of Object.entries({ 项目名称: "成果核实示例项目", 项目简介: "校园循环利用调研与原型验证。", 项目方向: "环境保护", 项目阶段: "原型验证", 团队现状: "示例团队" })) await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("草稿已保存，仅自己可见。");
  await page.getByRole("link", { name: "管理成果草稿", exact: true }).click();
  await page.getByRole("button", { name: "新建成果草稿", exact: true }).click();
  await page.getByLabel("成果名称", { exact: true }).fill("校园循环利用原型");
  await page.getByLabel("作品与阶段成果说明", { exact: true }).fill("已完成可操作的第一轮原型，等待核对证明后提交。" );
  const created = page.waitForResponse((response) => response.request().method() === "POST" && /\/achievements$/.test(new URL(response.url()).pathname));
  await page.getByRole("button", { name: "保存成果草稿", exact: true }).click();
  const achievement = await (await created).json() as { id: string; version: number };
  const ownerPageUrl = page.url();
  const projectId = new URL((await created).url()).pathname.match(/\/project-drafts\/([^/]+)\/achievements$/)![1];
  await expect(page.getByRole("region", { name: "成果核实与证明" })).toContainText("还没有证明材料");
  await page.getByLabel("成果名称", { exact: true }).blur();
  await page.addStyleTag({ content: ':root { --font-sans: "Noto Sans CJK SC", sans-serif; } .mono { font-family: "Liberation Mono", monospace !important; } * { animation: none !important; transition: none !important; }' });
  await expect(page).toHaveScreenshot("achievement-owner-proof-workspace.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.goto(`${BASE_URL}/innovation/reviews`);
  await expect(page.getByRole("region", { name: "成果核实待办" })).toContainText("暂无可领取的成果");
  await page.addStyleTag({ content: ':root { --font-sans: "Noto Sans CJK SC", sans-serif; } * { animation: none !important; transition: none !important; }' });
  await expect(page).toHaveScreenshot("achievement-operator-empty-queue.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.goto(`${BASE_URL}/innovation/achievements`);
  await expect(page.getByRole("region", { name: "校内已核实成果" })).toContainText("暂无可浏览的成果");
  await page.addStyleTag({ content: ':root { --font-sans: "Noto Sans CJK SC", sans-serif; } * { animation: none !important; transition: none !important; }' });
  await expect(page).toHaveScreenshot("achievement-campus-empty-list.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

  // R2 stable states use real revisions and decisions. Create only the two
  // new baselines; the three existing screens above retain their exact files.
  // browser_world exports the secondary student's UUID as the final entry
  // of its negative-DOM-search contract; this ID stays in test setup only.
  const operatorId = process.env.CQ_E2E_AUTHOR_SECRETS!.split(",").at(-1)!;
  expect(operatorId).toMatch(/^[0-9a-f-]{36}$/);
  const operatorHeaders = { Authorization: `Bearer ${mintToken(operatorId)}` };
  const operatorGrant = await page.request.put(`${API_URL}/admin/ie/operations-grants/${operatorId}`, { headers: adminHeaders, data: { version: 0, enabled: true, reason: "模拟更新复审视觉资料" } });
  expect(operatorGrant.status()).toBe(200);
  const base = `${API_URL}/ie/me/project-drafts/${projectId}/achievements/${achievement.id}`;
  const pdf = Buffer.from("%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n");
  const intent = await page.request.post(`${base}/evidence`, { headers: studentHeaders, data: { request_id: randomUUID(), size: pdf.length, content_type: "application/pdf" } });
  expect(intent.status()).toBe(201);
  const signed = await intent.json() as { upload_url: string; client_headers: Record<string, string>; evidence: { id: string } };
  expect((await page.request.put(signed.upload_url, { headers: signed.client_headers, data: pdf })).status()).toBe(200);
  expect((await page.request.post(`${base}/evidence/${signed.evidence.id}/complete`, { headers: studentHeaders })).status()).toBe(200);
  const project = await (await page.request.get(`${API_URL}/ie/me/project-drafts/${projectId}`, { headers: studentHeaders })).json() as { version: number };
  async function submit(operation: "submit" | "submit-update", achievementVersion: number) {
    const workflow = await (await page.request.get(`${base}/workflow`, { headers: studentHeaders })).json() as { version: number };
    const response = await page.request.post(`${base}/${operation}`, { headers: studentHeaders, data: { request_id: randomUUID(), workflow_version: workflow.version, project_version: project.version, achievement_version: achievementVersion, evidence_ids: [signed.evidence.id] } });
    expect(response.status()).toBe(200);
    return await response.json() as { review_case: { id: string; revision_id: string; version: number } };
  }
  async function approve(submitted: Awaited<ReturnType<typeof submit>>) {
    const item = submitted.review_case;
    const claimed = await page.request.post(`${API_URL}/ie/ops/achievement-reviews/${item.id}/claim`, { headers: operatorHeaders, data: { version: item.version } });
    expect(claimed.status()).toBe(200);
    const assigned = await claimed.json() as { version: number };
    expect((await page.request.post(`${API_URL}/ie/ops/achievement-reviews/${item.id}/decision`, { headers: operatorHeaders, data: { request_id: randomUUID(), version: assigned.version, revision_id: item.revision_id, decision: "APPROVED", reason: "" } })).status()).toBe(200);
  }
  await approve(await submit("submit", achievement.version));
  const saved = await page.request.patch(base, { headers: studentHeaders, data: { version: achievement.version, title: "校园循环利用原型", description: "第二轮模拟验证已完成，更新内容须重新核实。", work_url: "", award_text: "" } });
  expect(saved.status()).toBe(200);
  const newer = await saved.json() as { version: number };
  const pending = await submit("submit-update", newer.version);
  await page.goto(ownerPageUrl);
  await page.getByRole("button", { name: "编辑成果：校园循环利用原型", exact: true }).click();
  const ownerPanel = page.getByRole("region", { name: "成果核实与证明" });
  await expect(ownerPanel).toContainText("等待更新复审");
  await page.addStyleTag({ content: ':root { --font-sans: "Noto Sans CJK SC", sans-serif; } * { animation: none !important; transition: none !important; }' });
  await expect(ownerPanel).toHaveScreenshot("achievement-update-pending.png", { maxDiffPixelRatio: 0.01 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await approve(pending);
  await page.goto(`${BASE_URL}/innovation/achievements/${achievement.id}`);
  const publicDetail = page.getByRole("article", { name: "校内成果详情" });
  await expect(publicDetail).toContainText("当前为更新复审通过的版本。");
  await expect(publicDetail).not.toContainText("00889900");
  await expect(publicDetail.locator("time")).toHaveCount(2);
  await page.addStyleTag({ content: ':root { --font-sans: "Noto Sans CJK SC", sans-serif; } * { animation: none !important; transition: none !important; }' });
  await expect(publicDetail).toHaveScreenshot("achievement-update-approved.png", { mask: [publicDetail.locator("time")], maxDiffPixelRatio: 0.01 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});
