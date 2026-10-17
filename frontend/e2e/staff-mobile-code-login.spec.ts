/**
 * Staff mobile-code login + phone binding e2e (login-iteration item ①,
 * owner feedback 2026-10-17) — runs against the backend half (PR #101).
 * Contract-first: drafted against the frozen contract before the merge
 * (the ④②③ batch's red-confirmation pattern).
 *
 * One self-contained chain (order-free, no cross-test state):
 * 1. the seeded TEACHER binds a fresh phone through the profile's
 *    PhoneBindSection (first bind);
 * 2. the seeded ADMIN's bind attempt on the teacher's phone answers
 *    the 409 PHONE_ALREADY_BOUND copy — then binds its own fresh phone;
 * 3. the mobile-code channel: the teacher's phone logs in ONE step
 *    (TEACHER completes at verify — no second factor);
 * 4. the admin's phone answers the ADMIN totp_required leg (the
 *    seeded TOTP secret), which completes the login.
 *
 * The seeded accounts are re-created fresh by every battery seed, so
 * the in-run phone bindings never leak across runs.
 */
import { expect, test } from "@playwright/test";

import { staffLogin, totpCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const STAFF = process.env.CQ_E2E_STAFF;
const STAFF_TOTP_SECRET = process.env.CQ_E2E_STAFF_TOTP_SECRET;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;

test.skip(
  !(E2E_ENABLED && STAFF && STAFF_TOTP_SECRET && ADMIN && ADMIN_TOTP_SECRET),
  "set CQ_E2E=1 (and the CQ_E2E_STAFF/ADMIN + TOTP vars) to run this suite.",
);

/** A time-unique phone (bindings are teardown-invisible orphans). The
 * prefix separates the two phones minted back-to-back — Date.now() can
 * return the SAME millisecond for both, and a collision would hand the
 * admin the teacher's just-bound number (a 409, not a fresh bind). */
function freshPhone(prefix: "151" | "152" | "153" = "153"): string {
  return `${prefix}${String(Date.now()).slice(-8)}`;
}

/** Settle-verified phone fill (the hydration-wipe lesson: a fill that
 * lands before the client island hydrates is wiped by React's state
 * init; the request then goes out EMPTY and the 202-always decoy hides
 * it — verify after a beat, refill until it sticks). */
async function fillPhoneVerified(
  page: import("@playwright/test").Page,
  phone: string,
): Promise<void> {
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

/** The profile's bind flow: request the code, confirm it, land the note.
 * (StaffProfileView lives under /teacher/profile — the teacher
 * workspace's own route, open to ADMINs too. The bind challenge rides
 * the OTP table (OtpPurpose.PHONE_BIND — the register family), so its
 * code comes from recoverOtpCode; only the LOGIN channel's challenges
 * live in login_mobile_challenges.) */
async function bindPhoneThroughUi(page: import("@playwright/test").Page, phone: string): Promise<void> {
  await page.goto(`${BASE_URL}/teacher/profile`);
  await page.getByLabel("绑定手机号").fill(phone);
  await page.getByRole("button", { name: "发送验证码", exact: true }).click();
  await expect(page.getByLabel("短信验证码")).toBeVisible();
  const { recoverOtpCode } = await import("./fixtures");
  await page.getByLabel("短信验证码").fill(recoverOtpCode(`+86${phone}`, "PHONE_BIND"));
  await page.getByRole("button", { name: "绑定手机号", exact: true }).click();
  await expect(page.getByText("手机号已绑定。")).toBeVisible();
}

test("bind → the mobile-code channel: teacher one-step, admin two-leg, the 409 copy", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const teacherPhone = freshPhone("151");
  const adminPhone = freshPhone("152");

  // 1. The teacher's FIRST bind rides the profile section.
  await staffLogin(page, STAFF!, STAFF_TOTP_SECRET!);
  await bindPhoneThroughUi(page, teacherPhone);

  // 2. The admin (also unbound on the fresh seed — first-bind copy):
  //    a phone owned by the teacher answers the 409 family copy on the
  //    field; its own fresh phone binds cleanly.
  await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
  await page.goto(`${BASE_URL}/teacher/profile`);
  await page.getByLabel("绑定手机号").fill(teacherPhone);
  await page.getByRole("button", { name: "发送验证码", exact: true }).click();
  await expect(page.getByText("该手机号已绑定其他账号")).toBeVisible();
  await page.getByLabel("绑定手机号").fill(adminPhone);
  await page.getByRole("button", { name: "发送验证码", exact: true }).click();
  const { recoverMobileCode, recoverOtpCode } = await import("./fixtures");
  await page.getByLabel("短信验证码").fill(recoverOtpCode(`+86${adminPhone}`, "PHONE_BIND"));
  await page.getByRole("button", { name: "绑定手机号", exact: true }).click();
  await expect(page.getByText("手机号已绑定。")).toBeVisible();

  // Free the device for the channel tests below.
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/staff/login`));

  // 3. The TEACHER phone completes in ONE step: request → code → the
  //    teacher workspace (no second factor ever appears).
  await page.getByRole("button", { name: "手机验证码登录" }).click();
  await fillPhoneVerified(page, teacherPhone);
  await page.getByRole("button", { name: "重新发送", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page.getByLabel("短信验证码").fill(recoverMobileCode(`+86${teacherPhone}`));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  await expect(page.getByTestId("mobile-login-totp-leg")).toHaveCount(0);

  // 4. The ADMIN phone answers the totp_required leg — the seeded
  //    secret's current code completes it (a step rollover retried
  //    exactly like the family's loops).
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/staff/login`));
  await page.getByRole("button", { name: "手机验证码登录" }).click();
  await fillPhoneVerified(page, adminPhone);
  await page.getByRole("button", { name: "重新发送", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page.getByLabel("短信验证码").fill(recoverMobileCode(`+86${adminPhone}`));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByTestId("mobile-login-totp-leg")).toBeVisible();
  let landed = false;
  for (let attempt = 0; attempt < 3 && !landed; attempt += 1) {
    await page.getByLabel("动态验证码").fill(totpCode(ADMIN_TOTP_SECRET!));
    await page.getByRole("button", { name: "完成登录" }).click();
    try {
      // The #84 landing rule: the ADMIN lands on the ADMIN workspace.
      await expect(page).toHaveURL(new RegExp(`${BASE_URL}/admin/`), {
        timeout: 5_000,
      });
      landed = true;
    } catch {
      /* step rolled over mid-submit */
    }
  }
  expect(landed, "the totp leg must land on the workspace").toBe(true);
});
