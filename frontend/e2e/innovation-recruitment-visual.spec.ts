import AxeBuilder from "@axe-core/playwright";
import { API_URL, BASE_URL, ensureStudentLogin, expect, mintToken, test } from "./fixtures";
import type { Page } from "@playwright/test";

test.skip(process.env.CQ_E2E !== "1" || process.env.CQ_VISUAL !== "1" || process.env.CQ_E2E_FIXED_LABELS !== "1", "separate Linux pinned-font R3 visual world");

async function stable(page: Page, modal = false) {
  // Next's own empty live alert is present before the authenticated page.
  // Sample the product only after its accessible heading has rendered.
  if (modal) await expect(page.getByRole("dialog", { name: "发布招募", exact: true })).toBeVisible();
  else await expect(page.getByRole("main").getByRole("heading", { level: 1 })).toBeVisible();
  await page.addStyleTag({ content: ':root { --font-sans: "Noto Sans CJK SC", sans-serif; } *, *::before, *::after { animation: none !important; transition: none !important; }' });
  await page.evaluate(() => document.fonts.ready);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
}
test("R3 visual: owner form, consent preview and public project/recruitment", async ({ page }) => {
  test.setTimeout(150_000);
  const studentId = process.env.CQ_E2E_STUDENT_ID!;
  const headers = { Authorization: `Bearer ${mintToken(studentId)}` };
  const adminHeaders = { Authorization: `Bearer ${mintToken(process.env.CQ_E2E_ADMIN_ID!)}` };
  // Playlist order seeds this student first; review-visual re-reads these rows.
  const profile = await page.request.put(`${API_URL}/ie/me/owner-profile`, { headers, data: { name: "模拟招募负责人", student_no: "00889900", major: "模拟专业", grade: "2026级", version: 0 } }); expect(profile.status()).toBe(200);
  const application = await page.request.post(`${API_URL}/ie/me/owner-qualification`, { headers, data: { version: 0, profile_version: (await profile.json()).version } }); expect(application.status()).toBe(200);
  expect((await page.request.post(`${API_URL}/admin/ie/owner-qualifications/${studentId}/approve`, { headers: adminHeaders, data: { version: (await application.json()).version } })).status()).toBe(200);
  const created = await page.request.post(`${API_URL}/ie/me/project-drafts`, { headers, data: { request_id: crypto.randomUUID(), title: "模拟学习实验", summary: "私有父项目内容不可公开", direction: "", stage: "", team_status: "" } }); expect(created.status()).toBe(201);
  const project = await created.json();
  const fields = { project_title: "模拟学习实验", direction: "学习与实践", stage: "创意起步", introduction: "模拟资料：通过访谈了解大学生的学习过程，先完成一轮小规模实验，再共同讨论结果。\n我们希望把实际观察整理成可复用的学习方法。", idea_stage: true, goal: "完成第一轮访谈和记录", initial_plan: "先访谈，再整理发现并尝试原型。", role_name: "学习研究伙伴", headcount: 2, duties: "参与访谈、记录与讨论", requirements: "愿意倾听与整理，零基础也可以参加", time_commitment: "每周两小时，按共同约定安排", beginner_friendly: true, deadline_at: null, contacts: [{ channel: "WECHAT", value: "demo_partner" }] };
  const draft = await page.request.post(`${API_URL}/ie/me/project-drafts/${project.id}/recruitments`, { headers, data: { request_id: crypto.randomUUID(), fields } }); expect(draft.status()).toBe(201);
  const recruitment = await draft.json();
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${BASE_URL}/profile/project-drafts/${project.id}/recruitments`);
  await page.getByRole("button", { name: "编辑招募：学习研究伙伴", exact: true }).click();
  const editor = page.getByRole("region", { name: "招募岗位编辑器" });
  await expect(editor.getByLabel("微信", { exact: true })).toHaveValue("demo_partner");
  await stable(page); await expect(editor).toHaveScreenshot("recruitment-owner-form.png", { maxDiffPixelRatio: 0.01 });
  await page.setViewportSize({ width: 390, height: 844 });
  await editor.getByRole("button", { name: "预览发布招募", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "发布招募", exact: true });
  await expect(dialog).toContainText("授权微信：demo_partner");
  await stable(page, true); await expect(dialog).toHaveScreenshot("recruitment-mobile-consent.png", { maxDiffPixelRatio: 0.01 });
  await page.keyboard.press("Escape");
  await expect(editor.getByRole("button", { name: "预览发布招募", exact: true })).toBeFocused();
  const published = await page.request.post(`${API_URL}/ie/me/project-drafts/${project.id}/recruitments/${recruitment.id}/publish`, { headers, data: { version: recruitment.version, request_id: crypto.randomUUID() } }); expect(published.status()).toBe(200);
  await page.goto(`${BASE_URL}/innovation/recruitments/${recruitment.id}`);
  await expect(page.getByRole("article", { name: "校内招募详情" })).toBeVisible({ timeout: 15_000 });
  await expect(page.locator("body")).not.toContainText("demo_partner");
  await stable(page); await expect(page).toHaveScreenshot("recruitment-mobile-detail.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  await page.getByRole("button", { name: "查看联系方式", exact: true }).click();
  const contacts = page.getByRole("region", { name: "授权联系方式" });
  await expect(contacts).toContainText("demo_partner");
  await stable(page); await expect(contacts).toHaveScreenshot("recruitment-authorized-contact.png", { maxDiffPixelRatio: 0.01 });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${BASE_URL}/innovation/projects/${project.id}`);
  await expect(page.getByRole("article", { name: "校内项目详情" })).toContainText("负责人发布的招募版本（免审）");
  await stable(page); await expect(page).toHaveScreenshot("recruitment-project-detail.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  const current = (await published.json()).current;
  const closed = await page.request.post(`${API_URL}/ie/me/project-drafts/${project.id}/recruitments/${recruitment.id}/close`, { headers, data: { version: current.version, request_id: crypto.randomUUID() } }); expect(closed.status()).toBe(200);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE_URL}/innovation/recruitments/${recruitment.id}`);
  await expect(page.getByRole("main").getByRole("alert")).toContainText("不存在");
  await expect(page.locator("body")).not.toContainText("demo_partner");
  await stable(page); await expect(page).toHaveScreenshot("recruitment-mobile-closed.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
});

test("R3 visual: filtered empty and unavailable recruitment", async ({ page }) => {
  await ensureStudentLogin(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE_URL}/innovation/recruitments?q=不存在的模拟岗位`);
  await expect(page.getByRole("region", { name: "校内招募岗位" })).toContainText("暂无符合条件的招募");
  await stable(page); await expect(page).toHaveScreenshot("recruitment-mobile-empty.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  await page.goto(`${BASE_URL}/innovation/recruitments/00000000-0000-4000-8000-000000000000`);
  await expect(page.getByRole("main").getByRole("alert")).toContainText("不存在");
  await stable(page); await expect(page).toHaveScreenshot("recruitment-mobile-unavailable.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
});
