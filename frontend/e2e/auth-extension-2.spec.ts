/**
 * Auth extension II e2e (spec 2026-10-16 §1-§5) — runs against the
 * MERGED backend (the register/mobile-code/approval endpoints). The
 * register→banner→approve→unlock chain and the mobile-code login family
 * including the remembered-device fast path.
 *
 * Contract-first: drafted against the backend session's confirmed
 * answers (mobile-code 202-always; verify = full login for students;
 * the no-code fast login {phone} + cookie; register password
  * null-able; the pending queue via the status filter).
 */
import { expect, test, type Page } from "@playwright/test";

import { ensureStudentLogin, staffLogin } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;
const ADMIN_ID = process.env.CQ_E2E_ADMIN_ID;
const STUDENT = process.env.CQ_E2E_STUDENT;

test.skip(
  !(E2E_ENABLED && RUN && ADMIN && ADMIN_TOTP_SECRET && ADMIN_ID),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

/** A time-unique student number (registration has no whitelist now). */
function freshStudentNumber(): string {
  return `7${String(Date.now()).slice(-9)}`;
}

/** A time-unique phone (registered users are teardown-invisible). */
function freshPhone(): string {
  return `139${String(Date.now()).slice(-8)}`;
}

async function registerThroughUi(
  page: Page,
  options: { withPassword: boolean },
): Promise<{ number: string; phone: string; password: string | null }> {
  const studentNumber = freshStudentNumber();
  const phone = freshPhone();
  const password = options.withPassword ? "e2e-correct-horse" : null;
  await page.goto(`${BASE_URL}/register`);
  await page.getByLabel("学号").fill(studentNumber);
  await page.getByLabel("昵称").fill(`端到端待审${RUN!.slice(0, 4)}`);
  await page.getByLabel("手机号").fill(phone);
  await page.getByRole("button", { name: "获取验证码" }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  const { recoverOtpCode } = await import("./fixtures");
  await page.getByLabel("短信验证码").fill(recoverOtpCode(`+86${phone}`));
  if (password !== null) {
    await page.getByLabel("密码（可选）").fill(password);
  }
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login\\?registered=1`));
  return { number: studentNumber, phone, password };
}

async function loginMobileThroughUi(page: Page, phone: string): Promise<void> {
  await page.goto(`${BASE_URL}/login`);
  // The mobile tab is the default.
  await expect(page.getByRole("button", { name: "手机验证码登录" })).toBeVisible();
  await page.getByLabel("手机号").fill(phone);
  await page.getByRole("button", { name: "重新发送", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  const { recoverOtpCode } = await import("./fixtures");
  const code = recoverOtpCode(`+86${phone}`);
  await page.getByLabel("短信验证码").fill(code);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
}

test.describe("the register → approval → unlock chain (spec §1/§2/§5)", () => {
  test("register without a password → banner → admin approves → frozen surface unlocks", async ({
    page,
  }) => {
    const { phone } = await registerThroughUi(page, { withPassword: false });

    // The PENDING banner greets the mobile-code login.
    await loginMobileThroughUi(page, phone);
    await expect(page.getByText("账号待管理员审核。")).toBeVisible();

    // A frozen action answers the typed copy (the claim button is the
    // canonical acting surface on the square).
    await page.goto(`${BASE_URL}/tasks`);
    await expect(page.getByText("账号待管理员审核。")).toBeVisible();

    // The admin approves through the queue; the student's banner is
    // gone on the next load and the frozen copy with it.
    await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
    await page.goto(`${BASE_URL}/admin/approvals`);
    const row = page.locator("tr", { hasText: `139****${phone.slice(7)}` });
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "通过" }).click();
    await expect(row).toHaveCount(0);

    await ensureStudentLogin(page); // the seeded student: the square is open
    await expect(page.getByText("账号待管理员审核。")).toHaveCount(0);
  });

  test("reject with a reason removes the student from the queue", async ({ page }) => {
    const { phone } = await registerThroughUi(page, { withPassword: false });
    await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
    await page.goto(`${BASE_URL}/admin/approvals`);
    const row = page.locator("tr", { hasText: `139****${phone.slice(7)}` });
    await row.getByRole("button", { name: "拒绝" }).click();
    await page.getByLabel(/拒绝理由/).fill("端到端拒绝用例");
    await page.getByRole("button", { name: "确认拒绝" }).click();
    await expect(row).toHaveCount(0);
  });
});

test.describe("mobile-code login (spec §3/§4)", () => {
  test("the password tab still works for a passworded account; the mobile tab is default", async ({
    page,
  }) => {
    await page.goto(`${BASE_URL}/login`);
    // Mobile is the default tab; switch to password and log the seeded
    // student in through the classic channel.
    await page.getByRole("button", { name: "密码登录" }).click();
    const [username, password] = (STUDENT ?? "").split(":");
    await page.getByLabel("学号").fill(username);
    await page.getByLabel("密码").fill(password);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  });

  test("remember this device: the fast login button lands without an SMS", async ({
    page,
    browser,
  }) => {
    const { phone } = await registerThroughUi(page, { withPassword: false });
    // First login WITH the remember checkbox: arms the cookie.
    await page.goto(`${BASE_URL}/login`);
    await page.getByLabel("手机号").fill(phone);
    await page.getByRole("button", { name: "重新发送", exact: true }).click();
    await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
    const { recoverOtpCode } = await import("./fixtures");
    await page.getByLabel("短信验证码").fill(recoverOtpCode(`+86${phone}`));
    await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));

    // The replay context (cookie rides it): fast login, NO code request.
    const state = await page.context().storageState();
    const replay = await browser.newContext({ storageState: state });
    const second = await replay.newPage();
    second.goto(`${BASE_URL}/login`);
    await second.getByLabel("手机号").fill(phone);
    await second.getByRole("button", { name: "快速登录（已记住的设备）" }).click();
    await expect(second).toHaveURL(new RegExp(`${BASE_URL}/$`), { timeout: 8_000 });
    await replay.close();
  });

  test("a cookie-less fast login falls back to the code form honestly", async ({ page }) => {
    await page.goto(`${BASE_URL}/login`);
    await page.getByLabel("手机号").fill(freshPhone());
    await page.getByRole("button", { name: "快速登录（已记住的设备）" }).click();
    await expect(
      page.getByText("该设备尚未记住或已过期，请获取短信验证码登录"),
    ).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));
  });
});
