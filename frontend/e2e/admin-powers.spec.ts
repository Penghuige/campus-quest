/**
 * Wave 2a admin power-pack e2e (V1.1 spec §3/§4) — contract-first
 * against the 2026-10-18 frozen shapes; rides the merged backend at
 * the final battery run (the reviewer's sequencing).
 *
 * Coverage: the dashboard landing, the detail page's three actions
 * (one-time reset link / kick-then-401 on a second context / the role
 * round-trip), the invitation lifecycle panel, and the batch verbs.
 *
 * State discipline (the battery is one world, serial): every mutation
 * restores its subject — the role flips back, the suspended rows
 * reactivate, the kick only burns sessions (re-login is the next
 * spec's own business).
 */
import { expect, test, type Page } from "@playwright/test";

import { mintToken, staffLogin } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;
const ADMIN_ID = process.env.CQ_E2E_ADMIN_ID;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;
const STAFF = process.env.CQ_E2E_STAFF;
const STAFF_TOTP_SECRET = process.env.CQ_E2E_STAFF_TOTP_SECRET;

test.skip(
  !(E2E_ENABLED && RUN && ADMIN_ID && ADMIN && ADMIN_TOTP_SECRET && STAFF && STAFF_TOTP_SECRET),
  "set CQ_E2E=1 (and the CQ_E2E_ADMIN/STAFF + TOTP vars) to run this suite.",
);

test("the dashboard landing renders the overview census", async ({ page }) => {
  await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/admin$`));
  await expect(page.getByRole("heading", { name: "管理台概览" })).toBeVisible();
  await expect(page.getByText("今日新增用户")).toBeVisible();
  await expect(page.getByText("待审提交")).toBeVisible();
  await expect(page.getByText("任务状态分布")).toBeVisible();
  await expect(page.getByText("已归档")).toBeVisible(); // a zero-valued fixed key still renders
});

test("the user detail: reset link once, kick answers 401, the role round-trips", async ({
  page,
  browser,
}) => {
  const [staffEmail] = STAFF!.split(":");

  // A second context holds the victim teacher's session BEFORE the
  // kick; its post-kick request must answer 401 (the spec's verdict).
  const victim = await browser.newContext({ ignoreHTTPSErrors: true } as never);
  const victimPage = await victim.newPage();
  await staffLogin(victimPage, STAFF!, STAFF_TOTP_SECRET!);
  const victimState = await victim.storageState();

  await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
  // The listing's username links into the detail page.
  await page.goto(`${BASE_URL}/admin/users`);
  const row = page.locator("tr", { hasText: staffEmail });
  await row.getByRole("link", { name: staffEmail }).click();
  await expect(page.getByRole("heading", { name: "用户详情" })).toBeVisible();
  await expect(page.getByText("钱包余额")).toBeVisible();
  await expect(page.getByText("最近提交")).toBeVisible();

  // 代重置密码: the ONE-TIME link renders with the copy affordance.
  await page.getByRole("button", { name: "代重置密码" }).click();
  const linkBox = page.getByText("一次性重置链接（48 小时内有效");
  await expect(linkBox).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "复制重置链接" })).toBeVisible();

  // 踢线: BOTH counts answer; the victim's held session dies.
  await page.getByRole("button", { name: "踢出全部会话" }).click();
  await expect(
    page.getByText(/已撤销 \d+ 项会话与 \d+ 项设备凭据/),
  ).toBeVisible({ timeout: 10_000 });
  const probe = await victim.newPage();
  await probe.context().addCookies(victimState.cookies);
  const me = await probe.request.get(`${BASE_URL}/api/v1/me`);
  expect(me.status()).toBe(401);

  // 角色升降 round-trip: TEACHER → ADMIN → TEACHER (restore).
  await page.getByRole("button", { name: "提升为管理员" }).click();
  await expect(page.getByText("角色已调整为管理员")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "降级为教师" }).click();
  await expect(page.getByText("角色已调整为教师")).toBeVisible({ timeout: 10_000 });
  await victim.close();
});

test("the reset token's full loop: issue → consume page sets the password → fresh login", async ({
  page,
  browser,
}) => {
  // A DEDICATED invited teacher (password set at accept): the seeded
  // staff's password must never move (battery poison).
  const email = `e2e-wave2a-reset-${RUN}@school.edu`;
  const token = await mintToken(ADMIN_ID!);
  const response = await page.request.post(
    `${process.env.CQ_E2E_API_URL ?? "http://localhost:8100/api/v1"}/staff/invitations`,
    {
      headers: { Authorization: `Bearer ${token}` },
      data: { email, role: "TEACHER" },
    },
  );
  expect(response.ok(), await response.text()).toBe(true);
  const { token: inviteToken } = (await response.json()) as { token: string };
  await page.goto(`${BASE_URL}/staff/invite/${encodeURIComponent(inviteToken)}`);
  await page.getByLabel("设置密码").fill("e2e-old-horse-123");
  await page.getByLabel("确认密码").fill("e2e-old-horse-123");
  await page.getByRole("button", { name: "激活账号" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));

  // The admin issues the one-time token; the composed PUBLIC link
  // renders for exactly-once copy.
  await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
  await page.goto(`${BASE_URL}/admin/users`);
  const row = page.locator("tr", { hasText: email });
  await row.getByRole("link", { name: email }).click();
  await expect(page.getByRole("heading", { name: "用户详情" })).toBeVisible();
  await page.getByRole("button", { name: "代重置密码" }).click();
  const linkBox = page.getByText(/reset-password\?token=/);
  await expect(linkBox).toBeVisible({ timeout: 10_000 });
  const resetUrl = (await linkBox.textContent()) ?? "";

  // A FRESH context (the victim's own session was revoked server-side
  // at spend) opens the link: the token prefills, the new password
  // sets, the page lands the login's reset note.
  const spender = await browser.newContext({ ignoreHTTPSErrors: true } as never);
  const spenderPage = await spender.newPage();
  await spenderPage.goto(resetUrl.trim());
  await expect(spenderPage.getByLabel("重置口令")).toHaveValue(/.+/);
  await spenderPage.getByLabel("新密码").fill("e2e-new-horse-456");
  await spenderPage.getByRole("button", { name: "重置密码" }).click();
  await expect(spenderPage).toHaveURL(new RegExp(`${BASE_URL}/login\\?reset=1`), {
    timeout: 10_000,
  });
  await expect(spenderPage.getByText("密码已重置，请使用新密码登录")).toBeVisible();
  await spender.close();

  // The fresh session rides the unified page's code channel (the
  // invited teacher carries NO totp credential, and the password form
  // would demand a second factor) — the code proves the account is
  // alive under the NEW credential set either way.
  await page.getByRole("button", { name: "退出登录" }).click();
  await page.goto(`${BASE_URL}/login`);
  await page.getByLabel("手机号或邮箱", { exact: true }).fill(email);
  await page.getByRole("button", { name: "获取验证码", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  const { recoverEmailCode } = await import("./fixtures");
  await page.getByLabel("验证码", { exact: true }).fill(recoverEmailCode(email));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
});

test("the invitation panel: create → revoke → resend a one-time link", async ({ page }) => {
  await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
  await page.goto(`${BASE_URL}/admin/invitations`);

  // A fresh PENDING row through the existing create form.
  const email = `e2e-wave2a-invite-${Date.now()}@school.edu`;
  await page.getByLabel("受邀人邮箱").fill(email);
  await page.getByRole("button", { name: "创建邀请" }).click();
  await expect(page.getByTestId("invitation-link")).toBeVisible();
  await page.getByRole("button", { name: "再发一个邀请" }).click();

  // The lifecycle list carries it as 待接受. The list MASKS emails
  // (G11: first char + domain), so the row discriminates on the
  // state AND the domain (battery earlier tests leave their own
  // PENDING rows on other domains; accepted rows carry no verbs).
  const row = page
    .locator("tr")
    .filter({ hasText: "school.edu" })
    .filter({ hasText: "待接受" });
  await expect(row).toBeVisible({ timeout: 10_000 });

  // 撤销 (dialog confirm) flips the state; the token dies server-side.
  // The row re-locates BY its state each phase — the state IS the
  // discriminator (masked emails can collide, accepted rows carry no
  // verbs), and a stale pre-revoke reference stops matching the
  // moment the cell flips.
  await row.getByRole("button", { name: "撤销" }).click();
  await page.getByRole("button", { name: "确认撤销" }).click();
  await expect(page.getByText(/已撤销 .* 的邀请/)).toBeVisible();
  const revokedRow = page.locator("tr").filter({ hasText: "已撤销" });
  await expect(revokedRow).toBeVisible();

  // 重发 mints a fresh one-time link (the old token is dead; the row
  // reverts to PENDING — the window reopened).
  await revokedRow.getByRole("button", { name: "重发" }).click();
  await expect(page.getByTestId("resend-link")).toBeVisible({ timeout: 10_000 });
  // The note renders the row's MASKED email (the plain address never
  // reaches the admin surface — G11/AP7).
  await expect(page.getByText(/重发给 e\*\*\*@school\.edu/)).toBeVisible();
  await expect(
    page.locator("tr").filter({ hasText: "school.edu" }).filter({ hasText: "待接受" }),
  ).toBeVisible();
});

test("batch suspend/reactivate flips the selected rows", async ({ page }) => {
  await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
  await page.goto(`${BASE_URL}/admin/users`);
  // The first two student rows (deterministic order: the listing is
  // created_at-desc, the seeded accounts lead).
  const boxes = page.locator("tbody input[type=checkbox]");
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  await expect(page.getByText("已选 2 项")).toBeVisible();

  await page.getByRole("button", { name: "批量停用" }).click();
  await page.getByRole("button", { name: "确认执行" }).click();
  await expect(page.getByText(/批量操作完成：2 项成功/)).toBeVisible({ timeout: 10_000 });

  // Restore (state discipline): the same selection reactivates.
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  await page.getByRole("button", { name: "批量恢复" }).click();
  await page.getByRole("button", { name: "确认执行" }).click();
  await expect(page.getByText(/批量操作完成：\d+ 项成功/)).toBeVisible({ timeout: 10_000 });
});
