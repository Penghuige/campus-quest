/**
 * The recent-logins quick-switch e2e (batch 2, owner brief
 * 2026-10-20): the login page's remembered-account cards. ONE test,
 * one browser context (the ledger is per-context localStorage):
 *
 * - a completed login records its account — the teacher's email code
 *   login and the seeded student's password login each leave a card;
 * - the cards show the G11 masks + role badges (never the full
 *   address), newest first;
 * - clicking a card PREFILLS the identifier (the input the user
 *   themselves typed into — the remembered-username convention);
 * - 从本机移除 is a pure jar delete: the card vanishes and STAYS
 *   gone across a reload (no backend call exists to make).
 */
import { expect, test } from "@playwright/test";

import { mintToken, recoverEmailCode, recoverMobileCode, recoverOtpCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;

test.skip(!(E2E_ENABLED && RUN), "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.");

test("cards record on login, prefill on click, and removal is device-local", async ({ page }) => {
  test.setTimeout(120_000);
  const [teacherEmail] = (process.env.CQ_E2E_STAFF ?? "").split(":");

  // The TEACHER rides the email-code flow (the six chains' helper
  // shape, inline): request → recover → verify → workspace.
  await page.goto(`${BASE_URL}/login`);
  const field = page.getByLabel("账号", { exact: true });
  await field.fill(teacherEmail);
  await page.getByRole("button", { name: "获取验证码", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page.getByLabel("验证码", { exact: true }).fill(recoverEmailCode(teacherEmail));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));

  // Logout: the card for the teacher session greets the login page.
  // The mask keeps the address's FIRST local char (e2e-teacher… → e***).
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));
  const teacherMask = `${teacherEmail[0]}***@school.edu`;
  const teacherCard = page.locator(".login-recent-card", { hasText: teacherMask });
  await expect(teacherCard).toBeVisible();
  await expect(teacherCard.getByText("教师")).toBeVisible();

  // The SEEDED STUDENT's password login adds the second card (newest
  // first: the student rides on top).
  const [studentNumber, studentPassword] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(studentNumber);
  await page.getByLabel("密码").fill(studentPassword);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  // The student's logout rides the profile's INFO tab (the growth tab
  // is the default; defect #11's section lives under ?tab=info).
  await page.goto(`${BASE_URL}/profile?tab=info`);
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  const cards = page.locator(".login-recent-card");
  await expect(cards).toHaveCount(2);
  const studentCard = cards.first();
  await expect(studentCard.getByText("学生")).toBeVisible();
  await expect(studentCard.getByText(new RegExp(`^\\d{2}\\*\\*\\*\\*\\d{2}$`))).toBeVisible();

  // The click PREFILLS the identifier with the teacher's full address
  // (the field the user typed into — G11 lives on the CARD, not the input).
  await teacherCard.getByRole("button", { name: /切换到/ }).click();
  await expect(page.getByLabel("账号", { exact: true })).toHaveValue(teacherEmail);

  // 从本机移除: the student's card leaves, and stays gone on reload.
  await studentCard.getByRole("button", { name: /从本机移除/ }).click();
  await expect(page.locator(".login-recent-card")).toHaveCount(1);
  await page.reload();
  await expect(page.locator(".login-recent-card")).toHaveCount(1);
  await expect(teacherCard).toBeVisible();
});

/**
 * Batch 3 (owner feedback 2026-10-21): the smart click. A card whose
 * mask MATCHES the remembered-device probe lands in ONE click (the
 * cookie-only verify, zero code requests); a card without a stored
 * credential prefills and AUTO-SENDS its code (the owner's second-click
 * complaint), the cursor landing in the code field.
 */
test("a card matching the remembered device lands in ONE click (zero code requests)", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const ADMIN_ID = process.env.CQ_E2E_ADMIN_ID;
  // A DEDICATED invited teacher (a fresh mailbox bucket — the shared
  // seeded teacher's 60s resend window is burnt by the first chain).
  const email = `e2e-click-${RUN}@school.edu`;
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
  await page.getByRole("button", { name: "激活账号" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // The code login WITH the remember checkbox: arms the device cookie
  // AND records the card.
  await page.getByLabel("账号", { exact: true }).fill(email);
  await page.getByRole("button", { name: "获取验证码", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page.getByLabel("验证码", { exact: true }).fill(recoverEmailCode(email));
  await page.getByRole("checkbox", { name: /30 天内免登录/ }).check();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // The probe answers the SAME mask the card carries: banner + card
  // coexist (the banner's display logic is unchanged).
  const mask = `${email[0]}***@school.edu`;
  await expect(page.getByText(`继续以 ${mask} 登录`)).toBeVisible();
  const card = page.locator(".login-recent-card", { hasText: mask });
  await expect(card).toBeVisible();

  // ONE click: the matching card rides the cookie-only verify — NO
  // code request is ever made.
  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });
  await card.getByRole("button", { name: /切换到/ }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
  expect(requestCount, "a remembered click must not request a code").toBe(0);
});

test("a card without a stored credential prefills and AUTO-SENDS its code", async ({ page }) => {
  test.setTimeout(180_000);
  // A fresh phone-identifier student, registered through the real
  // register page (its own SMS bucket is fresh).
  const phone = `157${String(Date.now()).slice(-8)}`;
  const studentNumber = `8${String(Date.now()).slice(-9)}`;
  await page.goto(`${BASE_URL}/register`);
  await page.getByLabel("学号").fill(studentNumber);
  await page.getByLabel("昵称").fill(`点击${RUN!.slice(0, 3)}`);
  await page.getByLabel("手机号", { exact: true }).fill(phone);
  await page.getByRole("button", { name: "获取验证码" }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page.getByLabel("短信验证码").fill(recoverOtpCode(`+86${phone}`));
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`), { timeout: 15_000 });

  // A code-form login WITHOUT the remember checkbox: records the card,
  // arms NO device cookie (the register ride left no device); a 202 or
  // the mirrored 429 both leave the challenge row recoverable.
  await page.goto(`${BASE_URL}/profile?tab=info`);
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));
  await page.getByLabel("账号", { exact: true }).fill(phone);
  await page.getByRole("button", { name: "获取验证码", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page.getByLabel("验证码", { exact: true }).fill(recoverMobileCode(`+86${phone}`));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  await page.goto(`${BASE_URL}/profile?tab=info`);
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // No cookie → no banner; the card is the only shortcut. The click
  // prefills, fires ONE code request on its own, and lands the cursor in
  // the code field (the 429-mirrored cooldown keeps the row open).
  await expect(page.getByRole("button", { name: "一键继续" })).toHaveCount(0);
  const mask = `+86****${phone.slice(-4)}`;
  const card = page.locator(".login-recent-card", { hasText: mask });
  await expect(card).toBeVisible();
  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });
  await card.getByRole("button", { name: /切换到/ }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible({
    timeout: 10_000,
  });
  const codeField = page.getByLabel("验证码", { exact: true });
  await expect(codeField).toBeVisible();
  await expect(codeField).toBeFocused();
  expect(requestCount, "the click auto-sends exactly one request").toBe(1);
});
