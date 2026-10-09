import AxeBuilder from "@axe-core/playwright";
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
  await page.getByRole("button", { name: "保存成果草稿", exact: true }).click();
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
});
