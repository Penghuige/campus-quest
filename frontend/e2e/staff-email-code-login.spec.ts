/**
 * Staff email-code login e2e (V1.1 §2.1/§2.2; plan Tasks 8/12) — runs
 * against the MERGED Wave-1 backend (the endpoints the T5 contract
 * froze). Executed in the T12 round after the backend PR lands. Code
 * recovery rides the confirmed world action (`email-code`, exposed as
 * fixtures' recoverEmailCode — the otp-code pattern).
 *
 * Coverage (the plan's Task 12 list):
 * 1. teacher happy path: email → code → logged in, landing on the
 *    teacher workspace — zero TOTP anywhere;
 * 2. a wrong code renders the uniform EMAIL_CODE_INVALID copy (the
 *    wrong value is DERIVED from the real code, never a guess);
 * 3. ADMIN two-leg: verify answers totp_required, the 记住此设备
 *    checkbox rides the totp leg, and the 30-day cookie then SKIPS the
 *    leg on a replayed-context second login;
 * 4. the lockout envelope: five wrong codes lock the account and the
 *    copy names whole minutes (a dedicated invited teacher — locking
 *    the shared seeded teacher would poison the whole battery).
 */
import { expect, test, type Page } from "@playwright/test";

import { mintToken, recoverEmailCode, totpCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8100/api/v1";
const RUN = process.env.CQ_E2E_RUN;
const ADMIN_ID = process.env.CQ_E2E_ADMIN_ID;
const STAFF = process.env.CQ_E2E_STAFF;
const STAFF2 = process.env.CQ_E2E_STAFF2;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;

test.skip(
  !(E2E_ENABLED && RUN && ADMIN_ID && STAFF && ADMIN && ADMIN_TOTP_SECRET),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

/**
 * The emailed login code via the world action the backend confirmed
 * (`email-code <email>`, the otp-code pattern; the address passes the
 * form value — the action strips/lowercases internally).
 */

/** The obviously-wrong neighbor of the real code — deterministic, not
 * a guess (rolling +1 keeps it wrong with certainty). */
function wrongNeighborOf(code: string): string {
  return String((Number(code) + 1) % 1_000_000).padStart(6, "0");
}

async function requestCode(page: Page, email: string): Promise<string> {
  // exact: the 邮箱 label must not substring-match 邮箱验证码. The fill
  // is re-checked after a settle beat — a fill that lands before the
  // client island hydrates is wiped by React's state initialization
  // (the dev-server cold page), and a same-instant check would pass
  // before the wipe. Post-hydration refills stick.
  const emailField = page.getByLabel("邮箱", { exact: true });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await emailField.fill(email);
    await page.waitForTimeout(400);
    const value = await emailField.inputValue();
    if (value === email) {
      break;
    }
  }
  await expect(emailField).toHaveValue(email);
  await page.getByRole("button", { name: "重新发送", exact: true }).click();
  // The resend control enters its cooldown countdown (visible seconds).
  await expect(
    page.getByRole("button", { name: /重新发送（\d+ 秒）/ }),
  ).toBeVisible();
  await expect(page.getByText("验证码已发送", { exact: false })).toBeVisible();
  return recoverEmailCode(email);
}

test.describe("staff email-code login (V1.1 §2.1/§2.2)", () => {
  test("teacher: email → code → workspace, zero TOTP", async ({ page }) => {
    const [teacherEmail] = STAFF!.split(":");
    await page.goto(`${BASE_URL}/staff/login`);
    // The code channel is the default; the password channel one toggle away.
    await expect(page.getByRole("button", { name: "验证码登录" })).toBeVisible();
    await expect(page.getByRole("button", { name: "密码登录" })).toBeVisible();

    const code = await requestCode(page, teacherEmail);
    await page
      .getByLabel("邮箱验证码", { exact: false })
      .fill(code);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
    // No TOTP anywhere on this path.
    await expect(page.getByLabel("动态验证码")).toHaveCount(0);
  });

  test("a wrong code renders the uniform EMAIL_CODE_INVALID copy", async ({ page }) => {
    // A SEPARATE mailbox: the 60s resend cooldown is per-email, and the
    // teacher mailbox was just armed by the happy-path test above.
    const [teacher2Email] = (STAFF2 ?? "").split(":");
    test.skip(!teacher2Email, "needs CQ_E2E_STAFF2 (the seeded second teacher)");
    await page.goto(`${BASE_URL}/staff/login`);
    const code = await requestCode(page, teacher2Email);
    await page
      .getByLabel("邮箱验证码", { exact: false })
      .fill(wrongNeighborOf(code));
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page.getByText("验证码错误或已过期")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/staff/login`));
  });

  test("admin: totp_required two-leg; the remembered device then skips the leg", async ({ page, browser }) => {
    // The replay leg waits out the 60s resend cooldown — 180s covers
    // both legs plus the wait.
    test.setTimeout(180_000);
    const [adminEmail] = ADMIN!.split(":");
    await page.goto(`${BASE_URL}/staff/login`);
    const code = await requestCode(page, adminEmail);
    await page
      .getByLabel("邮箱验证码", { exact: false })
      .fill(code);
    await page.getByRole("button", { name: "登录", exact: true }).click();

    // The ADMIN second leg — the ONLY surface the checkbox lives on.
    await expect(page.getByTestId("email-login-totp-leg")).toBeVisible();
    const remember = page.getByRole("checkbox", {
      name: /记住此设备 30 天/,
    });
    await expect(remember).toBeVisible();
    await remember.check();
    // A slow hop can carry the totp submit across a 30s step boundary —
    // recompute the code on retry, exactly like the fixtures' loop.
    let landed = false;
    for (let attempt = 0; attempt < 3 && !landed; attempt += 1) {
      await page.getByLabel("动态验证码").fill(totpCode(ADMIN_TOTP_SECRET!));
      await page.getByRole("button", { name: "完成登录" }).click();
      try {
        // The #84 landing rule (hot-fix 2026-10-16): an ADMIN lands on
        // the ADMIN workspace, never the teacher queue.
        await expect(page).toHaveURL(new RegExp(`${BASE_URL}/admin/`), {
          timeout: 5_000,
        });
        landed = true;
      } catch {
        /* step rolled over mid-submit */
      }
    }
    expect(landed, "the totp leg must land on the workspace").toBe(true);

    // Replay the same browser context (the 30-day cookie rides it). A
    // successful verify CONSUMES the challenge row, so the replay needs
    // a fresh code — and the 60s per-mailbox resend cooldown is live in
    // the e2e stack (the login-rate relief covers the rate-LIMITER
    // family only, not this Redis cooldown key). Waiting the window out
    // is the honest path: this is the only end-to-end cookie-skip proof.
    await page.waitForTimeout(61_000);
    const state = await page.context().storageState();
    const replay = await browser.newContext({ storageState: state });
    const second = await replay.newPage();
    second.goto(`${BASE_URL}/staff/login`);
    const secondCode = await requestCode(second, adminEmail);
    await second.getByLabel("邮箱验证码", { exact: false }).fill(secondCode);
    await second.getByRole("button", { name: "登录", exact: true }).click();
    await expect(second).toHaveURL(new RegExp(`${BASE_URL}/admin/`));
    await expect(second.getByTestId("email-login-totp-leg")).toHaveCount(0);
    await replay.close();
  });

  test("five wrong codes lock the account with the minutes copy (dedicated invited teacher)", async ({ page }) => {
    // A DEDICATED account: locking the shared seeded teacher would
    // poison every later staff test for the lockout TTL.
    const adminToken = mintToken(ADMIN_ID!);
    const auth = { Authorization: `Bearer ${adminToken}` };
    const invitedEmail = `e2e.lockout.${RUN}@example.edu`;
    const invited = await page.request.post(`${API_URL}/staff/invitations`, {
      headers: auth,
      data: { email: invitedEmail, role: "TEACHER" },
    });
    expect(invited.ok(), `invitation failed: ${await invited.text()}`).toBe(true);
    const invitation = (await invited.json()) as { token: string };
    const accepted = await page.request.post(
      `${API_URL}/auth/staff/invitations/accept`,
      { data: { token: invitation.token, password: null } },
    );
    expect(accepted.ok(), `accept failed: ${await accepted.text()}`).toBe(true);

    await page.goto(`${BASE_URL}/staff/login`);
    const code = await requestCode(page, invitedEmail);
    const wrong = wrongNeighborOf(code);
    for (let attempt = 0; attempt < 5; attempt += 1) {
      await page
        .getByLabel("邮箱验证码", { exact: false })
        .fill(wrong);
      await page.getByRole("button", { name: "登录", exact: true }).click();
      await expect(page.getByText("验证码错误或已过期")).toBeVisible();
    }
    // The 6th attempt — with the CORRECT code — is refused by the lock.
    await page
      .getByLabel("邮箱验证码", { exact: false })
      .fill(code);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page.getByText(/尝试次数过多，请 \d+ 分钟后再试/)).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/staff/login`));
  });

});
