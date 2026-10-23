/**
 * CampusQuest auth e2e — Plan 09 Task 2 (registration + login flows);
 * wired to the runner by Plan 10 Task 2.
 *
 * Skip guard: every test below is skipped unless CQ_E2E=1, so importing
 * the file can never depend on a live backend during ordinary
 * development.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1            enable the suite (required);
 * - CQ_E2E_BASE_URL     frontend origin   (default https://localhost:3000);
 * - CQ_E2E_API_URL      backend API root  (default http://localhost:8000/api/v1);
 * - CQ_E2E_RUN / CQ_E2E_REGISTER_NUMBER / CQ_E2E_ADMIN_ID — the seeded
 *   world's contract (e2e/global-setup.ts + backend browser_world.py):
 *   a run-unique student number to register and the admin id used to
 *   recover the register-time SMS OTP.
 *
 * Auth extension II wiring: registration needs no seeding (the
 * whitelist gate is retired).
 * (preview -> confirm, a minted admin bearer — the two-step the admin
 * surface owns), and the SMS code is recovered from the REAL Redis OTP
 * challenge record (browser_world.py otp-code: the production service
 * persists only the HMAC digest, so the probe brute-forces the closed
 * 6-digit space with the same settings secret). No fixed dev code, no
 * fixture endpoint.
 */
import { expect, test } from "@playwright/test";

import { recoverOtpCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;
const ADMIN_ID = process.env.CQ_E2E_ADMIN_ID;

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* URLs) to run this suite.");

const worldReady = RUN !== undefined && ADMIN_ID !== undefined;
test.skip(
  !worldReady,
  "registration flow needs the seeded world (CQ_E2E_RUN + CQ_E2E_ADMIN_ID); Plan 10's global setup provides both.",
);

/** The run-unique student number the world reserved for this flow. */
const STUDENT_NUMBER =
  process.env.CQ_E2E_REGISTER_NUMBER ??
  `3${String(Date.now()).slice(-9)}`;

/** Run-unique CN mobile number (the shared-stack OTP/rate keys are
 * phone-scoped, so a stable phone would collide across reruns). */
function freshPhone(): string {
  const digits = String(
    Number.parseInt((RUN ?? "0").slice(0, 8), 16) % 10 ** 8,
  ).padStart(8, "0");
  return `139${digits}`;
}

/** Any never-registered numeric handle for the failure-copy tests.
 *
 * TWELVE digits behind a 6: libphonenumber parses MOST ten-digit CN
 * numerals as valid numbers (9/7/6-led samples collide 10-60% of the
 * time), and the backend's identifier resolver classifies a valid CN
 * number as PHONE before it ever considers the student-number arm — a
 * colliding handle flips the copy under test to 该手机号未注册. The
 * 6-led 12-digit shape samples clean (0/300); the student-number band
 * accepts 6-20 digits. */
function freshStudentNumber(): string {
  return `6${String(Date.now()).slice(-11)}`;
}

// Auth extension II (2026-10-16): the whitelist gate is GONE — the
// register API needs no seeding, and the seeded account lands in
// PENDING_APPROVAL (browsable, frozen) instead of ACTIVE.

test.describe("student registration and login", () => {
  test("registers (no password) and lands on home with the pending banner (auth II §1/§2)", async ({
    page,
  }) => {
    const phone = freshPhone();

    await page.goto(`${BASE_URL}/register`);
    await page.getByLabel("学号").fill(STUDENT_NUMBER);
    await page.getByLabel("昵称").fill("测试同学");
    await page.getByLabel("手机号").fill(phone);
    await page.getByRole("button", { name: "获取验证码" }).click();

    // The resend control enters its cooldown countdown (visible seconds).
    await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();

    // The real OTP challenge is now in Redis; recover its code. The
    // password field stays EMPTY — the mobile-code-only account shape.
    const code = recoverOtpCode(`+86${phone}`);
    await page.getByLabel("短信验证码").fill(code);
    await page.getByRole("button", { name: "注册", exact: true }).click();

    // ④ straight in (owner feedback 2026-10-17): the register
    // response's {user, tokens} envelope IS the login — the landing is
    // the home itself with the PENDING_APPROVAL banner (browsable,
    // frozen — auth II §2), never a login-page round trip.
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`), { timeout: 15_000 });
    await expect(page.getByText("账号待管理员审核。")).toBeVisible();
  });

  test("wrong password shows the stable failure copy, both fields resettable", async ({ page }) => {
    await page.goto(`${BASE_URL}/login`);
    // The unified page's password expansion (the weak link); the
    // student number rides the three-state identifier.
    await page.getByRole("button", { name: "使用密码登录" }).click();
    await page.getByLabel("账号", { exact: true }).fill(freshStudentNumber());
    await page.getByLabel("密码").fill("wrong-password");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    // (.first(): Next's route announcer also carries role=alert.)
    await expect(page.getByRole("alert").first()).toContainText("账号或密码不正确");
  });

  test("client convenience validation blocks obviously invalid input without a request", async ({ page }) => {
    await page.goto(`${BASE_URL}/register`);
    await page.getByLabel("学号").fill("123"); // too short, ASCII digits only 6-20
    await page.getByLabel("昵称").fill("测");
    await page.getByLabel("手机号").fill("13800138000");
    await page.getByLabel("密码").fill("short");
    await page.getByRole("button", { name: "注册", exact: true }).click();

    await expect(page.getByText(/学号需为 6-20 位数字/)).toBeVisible();
    await expect(page.getByText(/密码长度需为 10-128 个字符/)).toBeVisible();
  });

  test("nickname counter counts graphemes (emoji = 1) and caps at 16", async ({ page }) => {
    await page.goto(`${BASE_URL}/register`);
    const nickname = page.getByLabel("昵称");
    // One ZWJ family emoji = 1 user-perceived character.
    await nickname.fill("\u{1F468}‍\u{1F469}‍\u{1F467}‍\u{1F466}");
    await expect(page.getByText("1/16")).toBeVisible();
  });

  test("password recovery keeps enumeration-free copy", async ({ page }) => {
    await page.goto(`${BASE_URL}/forgot-password`);
    await page.getByLabel("学号").fill(freshStudentNumber()); // unknown username
    await page.getByRole("button", { name: "获取验证码", exact: true }).click();

    // Uniform copy for every identifier class — never "user not found".
    await expect(page.getByText("如果该用户名对应的账号存在且已绑定手机号")).toBeVisible();
  });
});
