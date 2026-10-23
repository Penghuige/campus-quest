/**
 * Auth extension II e2e (spec 2026-10-16 §1-§5 + the 2026-10-17
 * login-iteration batch ④③) — runs against the MERGED backend (the
 * register/mobile-code/approval endpoints). The register→banner→
 * approve→unlock chain and the mobile-code login family including the
 * remembered-device banner.
 *
 * Contract-first: drafted against the backend session's confirmed
 * answers (mobile-code 202-always; verify = full login for students;
 * the register {user, tokens} envelope IS the login — straight onto
 * the student home; the GET remembered probe answers the mask or a
 * uniform 204; the cookie-only verify leg posts an empty body;
 * register password null-able; the pending queue via the status
 * filter).
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

/** A time-unique student number (registration has no whitelist now).
 *
 * 6-led TWELVE digits: ten-digit CN numerals are mostly valid phone
 * numbers to libphonenumber (7-led samples collide 60%), and the
 * unified login resolver classifies a valid number as PHONE — the
 * password walk later in this file fills this number as the identifier,
 * so a colliding handle answers 该手机号未注册. 6-led 12-digit samples
 * clean (0/300); the student-number band accepts 6-20 digits. */
function freshStudentNumber(): string {
  return `6${String(Date.now()).slice(-11)}`;
}

/** A time-unique phone (registered users are teardown-invisible). */
function freshPhone(): string {
  return `139${String(Date.now()).slice(-8)}`;
}

/** Settle-verified phone fill (the hydration-wipe lesson: a fill that
 * lands before the client island hydrates is wiped by React's state
 * init; verify after a beat, refill until it sticks). */
async function fillPhoneVerified(page: Page, phone: string): Promise<void> {
  const field = page.getByLabel("手机号", { exact: true });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await field.fill(phone);
    await page.waitForTimeout(400);
    if ((await field.inputValue()) === phone) {
      return;
    }
  }
  await expect(field).toHaveValue(phone);
}

/** The unified LOGIN page's identifier field (手机号或邮箱). */
async function fillLoginIdentifierVerified(page: Page, identifier: string): Promise<void> {
  const field = page.getByLabel("账号", { exact: true });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await field.fill(identifier);
    await page.waitForTimeout(400);
    if ((await field.inputValue()) === identifier) {
      return;
    }
  }
  await expect(field).toHaveValue(identifier);
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
  await fillPhoneVerified(page, phone);
  await page.getByRole("button", { name: "获取验证码" }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  const { recoverOtpCode } = await import("./fixtures");
  await page.getByLabel("短信验证码").fill(recoverOtpCode(`+86${phone}`));
  if (password !== null) {
    await page.getByLabel("密码（可选）").fill(password);
  }
  await page.getByRole("button", { name: "注册", exact: true }).click();
  // ④ (owner feedback 2026-10-17): the register response's {user,
  // tokens} envelope IS the login — straight onto the student home
  // (PENDING_APPROVAL browse-frozen surface), never a login-page round
  // trip. 15s: the first hit on "/" pays the dev server's cold compile.
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`), { timeout: 15_000 });
  return { number: studentNumber, phone, password };
}

test.describe("the register → approval → unlock chain (spec §1/§2/§5)", () => {
  test("register without a password → straight in → banner → admin approves → frozen surface unlocks", async ({
    page,
  }) => {
    const { phone } = await registerThroughUi(page, { withPassword: false });

    // ④ straight-in: the registration's tokens opened the session —
    // the PENDING banner greets the landing itself, no login round
    // trip (the URL assertion inside registerThroughUi is ④'s pin).
    await expect(page.getByText("账号待管理员审核。")).toBeVisible();

    // A frozen action answers the typed copy (the claim button is the
    // canonical acting surface on the square).
    await page.goto(`${BASE_URL}/tasks`);
    await expect(page.getByText("账号待管理员审核。")).toBeVisible();

    // The admin approves through the queue; the student's banner is
    // gone on the next load and the frozen copy with it.
    await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
    await page.goto(`${BASE_URL}/admin/approvals`);
    const row = page.locator("tr", { hasText: `+86****${phone.slice(-4)}` });
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
    const row = page.locator("tr", { hasText: `+86****${phone.slice(-4)}` });
    await row.getByRole("button", { name: "拒绝" }).click();
    await page.getByLabel(/拒绝理由/).fill("端到端拒绝用例");
    await page.getByRole("button", { name: "确认拒绝" }).click();
    await expect(row).toHaveCount(0);
  });
});

test.describe("mobile-code login (spec §3/§4)", () => {
  test("the password expansion still works for a passworded account; the code form is default", async ({
    page,
  }) => {
    await page.goto(`${BASE_URL}/login`);
    // The code form is the default; the weak link expands the password
    // form and the three-state identifier carries the seeded 学号.
    await page.getByRole("button", { name: "使用密码登录" }).click();
    const [username, password] = (STUDENT ?? "").split(":");
    await page.getByLabel("账号", { exact: true }).fill(username);
    await page.getByLabel("密码").fill(password);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  });

  test("remember this device: the banner's one click lands without an SMS", async ({
    page,
    browser,
  }) => {
    const { phone } = await registerThroughUi(page, { withPassword: false });
    // ④ leaves the account logged in; log out to reach the code form
    // (register itself arms NO device cookie — there is no checkbox on
    // the register surface).
    await page.goto(`${BASE_URL}/profile?tab=info`);
    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

    // First login WITH the remember checkbox: arms the cookie.
    await fillLoginIdentifierVerified(page, phone);
    await page.getByRole("button", { name: "获取验证码", exact: true }).click();
    await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
    const { recoverMobileCode } = await import("./fixtures");
    await page.getByLabel("验证码", { exact: true }).fill(recoverMobileCode(`+86${phone}`));
    await page.getByRole("checkbox", { name: /30 天内免登录/ }).check();
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));

    // The replay context (cookie rides it): the login page's PROBE
    // answers the mask — the conditional banner (③) offers the
    // one-click continuation, and its click is the cookie-only verify
    // leg: no SMS, no code field, straight onto the home.
    const state = await page.context().storageState();
    const replay = await browser.newContext({ storageState: state });
    const second = await replay.newPage();
    second.goto(`${BASE_URL}/login`);
    const banner = second.getByText(`继续以 +86****${phone.slice(-4)} 登录`);
    await expect(banner).toBeVisible();
    await second.getByRole("button", { name: "一键继续" }).click();
    await expect(second).toHaveURL(new RegExp(`${BASE_URL}/$`), { timeout: 8_000 });
    await replay.close();
  });

  test("no cookie: the login page renders ZERO banner elements (③'s two-state pin)", async ({ page }) => {
    // A fresh context carries no cq_staff_device cookie: the probe's
    // uniform 204 must leave the login page byte-identical to the
    // pre-banner page — the banner, its copy, and its button are all
    // absent (无 cookie 时登录页零多余元素).
    await page.goto(`${BASE_URL}/login`);
    await expect(page.getByText(/继续以 .+ 登录/)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "一键继续" })).toHaveCount(0);
    await expect(page.locator(".login-fast-hint")).toHaveCount(0);
  });
});
