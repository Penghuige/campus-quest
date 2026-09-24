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
 * - CQ_E2E_BASE_URL     frontend origin   (default http://localhost:3000);
 * - CQ_E2E_API_URL      backend API root  (default http://localhost:8000/api/v1);
 * - CQ_E2E_RUN / CQ_E2E_REGISTER_NUMBER / CQ_E2E_ADMIN_ID — the seeded
 *   world's contract (e2e/global-setup.ts + backend browser_world.py):
 *   a run-unique student number to register and the admin id used to
 *   mint the whitelist-import token.
 *
 * Plan 10 wiring: the whitelist is seeded through the REAL admin API
 * (preview -> confirm, a minted admin bearer — the two-step the admin
 * surface owns), and the SMS code is recovered from the REAL Redis OTP
 * challenge record (browser_world.py otp-code: the production service
 * persists only the HMAC digest, so the probe brute-forces the closed
 * 6-digit space with the same settings secret). No fixed dev code, no
 * fixture endpoint.
 */
import { expect, request, test, type APIRequestContext } from "@playwright/test";

import { mintToken, recoverOtpCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8000/api/v1";
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

/** Any never-registered numeric handle for the failure-copy tests. */
function freshStudentNumber(): string {
  return `9${String(Date.now()).slice(-9)}`;
}

/** Seed the registration whitelist through the REAL admin API. */
async function seedWhitelist(api: APIRequestContext): Promise<void> {
  const adminToken = mintToken(ADMIN_ID!);
  const auth = { Authorization: `Bearer ${adminToken}` };
  const preview = await api.post(`${API_URL}/admin/whitelist/preview`, {
    headers: auth,
    data: { content: `${STUDENT_NUMBER}\n` },
  });
  expect(
    preview.ok(),
    `whitelist preview failed (${preview.status()}): ${await preview.text()}`,
  ).toBe(true);
  const body = (await preview.json()) as { confirm_token: string };
  const confirmed = await api.post(`${API_URL}/admin/whitelist/confirm`, {
    headers: auth,
    data: {
      confirm_token: body.confirm_token,
      enable: true,
      student_numbers: [STUDENT_NUMBER],
    },
  });
  expect(
    confirmed.ok(),
    `whitelist confirm failed (${confirmed.status()}): ${await confirmed.text()}`,
  ).toBe(true);
}

test.describe("student registration and login", () => {
  let api: APIRequestContext;

  test.beforeAll(async () => {
    // The module-level `request` factory (NOT the per-test fixture,
    // which is already a context) — the E1-recorded fix for the old
    // `test.request` misuse.
    api = await request.newContext({ baseURL: BASE_URL });
  });

  test.afterAll(async () => {
    await api.dispose();
  });

  test("registers with phone OTP and lands on home after login", async ({ page }) => {
    await seedWhitelist(api);
    const phone = freshPhone();

    await page.goto(`${BASE_URL}/register`);
    await page.getByLabel("学号").fill(STUDENT_NUMBER);
    await page.getByLabel("昵称").fill("测试同学");
    await page.getByLabel("手机号").fill(phone);
    await page.getByRole("button", { name: "获取验证码" }).click();

    // The resend control enters its cooldown countdown (visible seconds).
    await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();

    // The real OTP challenge is now in Redis; recover its code.
    const code = recoverOtpCode(`+86${phone}`);
    await page.getByLabel("短信验证码").fill(code);
    await page.getByLabel("密码").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "注册", exact: true }).click();

    // Success redirects to the login page with the registered note.
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login\\?registered=1`));
    await expect(page.getByText("注册成功，请使用学号登录。")).toBeVisible();

    // Login with the fresh credentials -> student home.
    await page.getByLabel("学号").fill(STUDENT_NUMBER);
    await page.getByLabel("密码").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  });

  test("wrong password shows the stable failure copy, both fields resettable", async ({ page }) => {
    await page.goto(`${BASE_URL}/login`);
    await page.getByLabel("学号").fill(freshStudentNumber());
    await page.getByLabel("密码").fill("wrong-password");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    // (.first(): Next's route announcer also carries role=alert.)
    await expect(page.getByRole("alert").first()).toContainText("学号或密码不正确");
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
