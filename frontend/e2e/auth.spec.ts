/**
 * CampusQuest auth e2e — Plan 09 Task 2 (registration + login flows).
 *
 * STATUS: SPEC ONLY — NOT WIRED TO A RUNNER YET.
 *
 * Skip guard: Playwright itself is installed by Plan 10 (this stream's
 * frontend has no `@playwright/test` dependency and no `test:e2e` script
 * yet). Until then this file must stay invisible to the gates:
 * - `tsconfig.json` includes only `src/**` + `.next/**`, so `tsc` skips it;
 * - `eslint.config.mjs` lists `e2e/**` in globalIgnores for the same reason;
 * - `next build` never touches files outside `src/app`.
 * Once Plan 10 installs Playwright, remove the eslint ignore, add the
 * `test:e2e` script, and run with `CQ_E2E=1` — every test below is skipped
 * unless that flag is set, so importing the file can never depend on a live
 * backend during ordinary development.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1            enable the suite (required);
 * - CQ_E2E_BASE_URL     frontend origin   (default http://localhost:3000);
 * - CQ_E2E_API_URL      backend API root  (default http://localhost:8000/api/v1);
 * - CQ_E2E_OTP_CODE     the fixed dev SMS code the fake sender issues
 *                       (Plan 10's fixture contract; default 000000);
 * - CQ_E2E_SEED_URL     whitelist-seeding endpoint the fixture provides
 *                       (default $CQ_E2E_API_URL/admin/whitelist — the admin
 *                       surface lands with its own stream; adjust there).
 */
import { expect, test, type APIRequestContext } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8000/api/v1";
const OTP_CODE = process.env.CQ_E2E_OTP_CODE ?? "000000";
const SEED_URL = process.env.CQ_E2E_SEED_URL ?? `${API_URL}/admin/whitelist`;

test.skip(!E2E_ENABLED, "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* URLs) to run this suite.");

/** Unique-enough student number per run: 9 digits starting with the epoch minute. */
function freshStudentNumber(): string {
  return String(Date.now()).slice(-9);
}

/** Seed the registration whitelist for one student number (Plan 10 fixture). */
async function seedWhitelist(api: APIRequestContext, studentNumber: string): Promise<void> {
  const response = await api.post(SEED_URL, { data: { student_number: studentNumber } });
  expect(
    response.ok(),
    `whitelist seeding failed (${response.status()}); check CQ_E2E_SEED_URL and the Plan 10 fixture`,
  ).toBe(true);
}

test.describe("student registration and login", () => {
  let api: APIRequestContext;

  test.beforeAll(async () => {
    api = await test.request.newContext({ baseURL: API_URL });
  });

  test.afterAll(async () => {
    await api.dispose();
  });

  test("registers with phone OTP and lands on home after login", async ({ page }) => {
    const studentNumber = freshStudentNumber();
    await seedWhitelist(api, studentNumber);

    await page.goto(`${BASE_URL}/register`);
    await page.getByLabel("学号").fill(studentNumber);
    await page.getByLabel("昵称").fill("测试同学");
    await page.getByLabel("手机号").fill("13800138000");
    await page.getByRole("button", { name: "获取验证码" }).click();

    // The resend control enters its cooldown countdown (visible seconds).
    await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();

    await page.getByLabel("短信验证码").fill(OTP_CODE);
    await page.getByLabel("密码").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "注册", exact: true }).click();

    // Success redirects to the login page with the registered note.
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login\\?registered=1`));
    await expect(page.getByText("注册成功，请使用学号登录。")).toBeVisible();

    // Login with the fresh credentials -> student home.
    await page.getByLabel("学号").fill(studentNumber);
    await page.getByLabel("密码").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  });

  test("wrong password shows the stable failure copy, both fields resettable", async ({ page }) => {
    await page.goto(`${BASE_URL}/login`);
    await page.getByLabel("学号").fill(freshStudentNumber());
    await page.getByLabel("密码").fill("wrong-password");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expect(page.getByRole("alert")).toContainText("学号或密码不正确");
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
