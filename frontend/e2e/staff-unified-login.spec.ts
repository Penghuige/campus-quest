/**
 * Staff UNIFIED code-login e2e (the two-channel convergence,
 * 2026-10-20) — rides the backend's unified identifier endpoints
 * (contract-frozen; implementation in its #74 slot): ONE identifier
 * field carries email OR phone, the backend routes the code stream.
 *
 * Coverage:
 * 1. teacher EMAIL chain: identifier → code → workspace, zero TOTP;
 * 2. teacher PHONE chain, fully self-contained (invite → accept →
 *    bind a phone through the live profile section → logout → login
 *    with the PHONE as the identifier);
 * 3. a wrong code renders the uniform invalid copy (derived, never
 *    guessed);
 * 4. ADMIN two-leg + the remembered device SKIPS the leg on replay
 *    (the checkbox's meaning lives on the totp leg — option a);
 * 5. lockout after five wrong codes (a DEDICATED invited teacher —
 *    locking the shared seeded teacher would poison the battery);
 * 6. the password channel survives one toggle away (the fixtures'
 *    staffLogin walks it everywhere; this pins the toggle itself).
 */
import { expect, test, type Page } from "@playwright/test";

import { mintToken, recoverEmailCode, recoverMobileCode, totpCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;
const ADMIN_ID = process.env.CQ_E2E_ADMIN_ID;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;

test.skip(
  !(E2E_ENABLED && RUN && ADMIN_ID && ADMIN && ADMIN_TOTP_SECRET),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

/** The obviously-wrong neighbor of the real code — deterministic. */
function wrongNeighborOf(code: string): string {
  return String((Number(code) + 1) % 1_000_000).padStart(6, "0");
}

/** Settle-verified identifier fill (the hydration-wipe lesson). */
async function fillIdentifierVerified(page: Page, identifier: string): Promise<void> {
  const field = page.getByLabel("邮箱或手机号", { exact: true });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await field.fill(identifier);
    await page.waitForTimeout(400);
    if ((await field.inputValue()) === identifier) {
      return;
    }
  }
  await expect(field).toHaveValue(identifier);
}

async function requestCode(page: Page, identifier: string): Promise<string> {
  await fillIdentifierVerified(page, identifier);
  await page.getByRole("button", { name: "重新发送", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await expect(page.getByText("验证码已发送", { exact: false })).toBeVisible();
  // The backend routes the stream by shape — recovery rides the
  // matching world action.
  return identifier.includes("@")
    ? recoverEmailCode(identifier)
    : recoverMobileCode(`+86${identifier}`);
}

test.describe("staff unified code login (two-channel convergence)", () => {
  test("teacher EMAIL chain: identifier → code → workspace, zero TOTP", async ({ page }) => {
    const [email] = (process.env.CQ_E2E_STAFF ?? "").split(":");
    await page.goto(`${BASE_URL}/staff/login`);
    const code = await requestCode(page, email);
    await page.getByLabel("验证码", { exact: true }).fill(code);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
    await expect(page.getByTestId("unified-login-totp-leg")).toHaveCount(0);
  });

  test("teacher PHONE chain: bind then login with the phone as the identifier", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    // A dedicated invited teacher, accepted through the real UI (no
    // password — teachers skip TOTP onboarding).
    const token = await mintToken(ADMIN_ID!);
    const response = await page.request.post(
      `${process.env.CQ_E2E_API_URL ?? "http://localhost:8100/api/v1"}/staff/invitations`,
      {
        headers: { Authorization: `Bearer ${token}` },
        data: { email: `e2e-unified-${RUN}@school.edu`, role: "TEACHER" },
      },
    );
    expect(response.ok(), await response.text()).toBe(true);
    const { token: inviteToken } = (await response.json()) as { token: string };
    await page.goto(`${BASE_URL}/staff/invite/${encodeURIComponent(inviteToken)}`);
    await page.getByRole("button", { name: "激活账号" }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));

    // Bind a phone through the live profile section (the ① surface).
    const phone = `156${String(Date.now()).slice(-8)}`;
    await page.goto(`${BASE_URL}/teacher/profile`);
    await page.getByLabel("绑定手机号").fill(phone);
    await page.getByRole("button", { name: "发送验证码", exact: true }).click();
    await page
      .getByLabel("短信验证码")
      .fill(recoverMobileCode(`+86${phone}`));
    await page.getByRole("button", { name: "绑定手机号", exact: true }).click();
    await expect(page.getByText("手机号已绑定")).toBeVisible();
    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/staff/login`));

    // The unified channel with the PHONE as the identifier.
    const code = await requestCode(page, phone);
    await page.getByLabel("验证码", { exact: true }).fill(code);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  });

  test("a wrong code renders the uniform invalid copy", async ({ page }) => {
    const [email] = (process.env.CQ_E2E_STAFF ?? "").split(":");
    await page.goto(`${BASE_URL}/staff/login`);
    const code = await requestCode(page, email);
    await page.getByLabel("验证码", { exact: true }).fill(wrongNeighborOf(code));
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page.getByText("验证码错误或已过期")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/staff/login`));
  });

  test("admin two-leg; the remembered device then skips the leg", async ({ page, browser }) => {
    // The replay leg waits out the 60s resend cooldown — 180s covers both.
    test.setTimeout(180_000);
    const [adminEmail] = ADMIN!.split(":");
    await page.goto(`${BASE_URL}/staff/login`);
    const code = await requestCode(page, adminEmail);
    await page.getByLabel("验证码", { exact: true }).fill(code);
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expect(page.getByTestId("unified-login-totp-leg")).toBeVisible();
    await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
    let landed = false;
    for (let attempt = 0; attempt < 3 && !landed; attempt += 1) {
      await page.getByLabel("动态验证码").fill(totpCode(ADMIN_TOTP_SECRET!));
      await page.getByRole("button", { name: "完成登录" }).click();
      try {
        await expect(page).toHaveURL(new RegExp(`${BASE_URL}/admin/`), { timeout: 5_000 });
        landed = true;
      } catch {
        /* step rolled over mid-submit */
      }
    }
    expect(landed, "the totp leg must land on the workspace").toBe(true);

    // Replay the same context (the 30-day cookie rides it): a fresh
    // code — the 60s per-mailbox cooldown is live — then the verify
    // completes WITHOUT the totp leg.
    await page.waitForTimeout(61_000);
    const state = await page.context().storageState();
    const replay = await browser.newContext({ storageState: state });
    const second = await replay.newPage();
    second.goto(`${BASE_URL}/staff/login`);
    const secondCode = await requestCode(second, adminEmail);
    await second.getByLabel("验证码", { exact: true }).fill(secondCode);
    await second.getByRole("button", { name: "登录", exact: true }).click();
    await expect(second).toHaveURL(new RegExp(`${BASE_URL}/admin/`));
    await expect(second.getByTestId("unified-login-totp-leg")).toHaveCount(0);
    await replay.close();
  });

  test("five wrong codes lock the account with the minutes copy (dedicated invited teacher)", async ({
    page,
  }) => {
    test.setTimeout(180_000);
    // (Kept from the email-code family: locking the shared seeded
    // teacher would poison the whole battery.)
    const token = await mintToken(ADMIN_ID!);
    const response = await page.request.post(
      `${process.env.CQ_E2E_API_URL ?? "http://localhost:8100/api/v1"}/staff/invitations`,
      {
        headers: { Authorization: `Bearer ${token}` },
        data: { email: `e2e-lock-${RUN}@school.edu`, role: "TEACHER" },
      },
    );
    const { token: inviteToken } = (await response.json()) as { token: string };
    await page.goto(`${BASE_URL}/staff/invite/${encodeURIComponent(inviteToken)}`);
    await page.getByRole("button", { name: "激活账号" }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
    await page.getByRole("button", { name: "退出登录" }).click();

    const email = `e2e-lock-${RUN}@school.edu`;
    await page.goto(`${BASE_URL}/staff/login`);
    const code = await requestCode(page, email);
    const wrong = wrongNeighborOf(code);
    for (let attempt = 0; attempt < 5; attempt += 1) {
      await page.getByLabel("验证码", { exact: true }).fill(wrong);
      await page.getByRole("button", { name: "登录", exact: true }).click();
      await expect(page.getByText("验证码错误或已过期")).toBeVisible();
    }
    // The sixth attempt — even with the RIGHT code — answers the
    // lockout envelope with whole minutes.
    await page.getByLabel("验证码", { exact: true }).fill(code);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page.getByText(/尝试次数过多，请 \d+ 分钟后再试/)).toBeVisible();
  });

  test("the password channel stays one toggle away", async ({ page }) => {
    await page.goto(`${BASE_URL}/staff/login`);
    await page.getByRole("button", { name: "密码登录" }).click();
    await expect(page.getByLabel("邮箱", { exact: true })).toBeVisible();
    await expect(page.getByLabel("动态验证码")).toBeVisible();
  });
});
