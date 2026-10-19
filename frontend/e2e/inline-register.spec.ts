/**
 * The send-code-as-entry e2e (production experience batch 2026-10-19):
 * a FRESH phone rides the login page's mobile channel all the way —
 * code → the inline unregistered third step (学号 + optional
 * password) → registered AND logged in, the PENDING banner greeting
 * the landing. Zero register-page detours for the new user.
 *
 * The registered-phone arms stay covered where they live
 * (auth-extension-2's code-login family); this spec adds the
 * unregistered union arm only.
 */
import { expect, test, type Page } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;

test.skip(
  !(E2E_ENABLED && RUN),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

/** A time-unique phone (the account is a teardown-invisible orphan). */
function freshPhone(): string {
  return `155${String(Date.now()).slice(-8)}`;
}

/** Settle-verified phone fill (the hydration-wipe lesson). */
async function fillPhoneVerified(page: Page, phone: string): Promise<void> {
  const field = page.getByLabel("手机号或邮箱", { exact: true });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await field.fill(phone);
    await page.waitForTimeout(400);
    if ((await field.inputValue()) === phone) {
      return;
    }
  }
  await expect(field).toHaveValue(phone);
}

test("a fresh phone registers inline from the login page and lands logged in", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const phone = freshPhone();
  const studentNumber = `6${String(Date.now()).slice(-9)}`;

  await page.goto(`${BASE_URL}/login`);
  await fillPhoneVerified(page, phone);
  await page.getByRole("button", { name: "获取验证码", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  const { recoverMobileCode } = await import("./fixtures");
  await page.getByLabel("验证码", { exact: true }).fill(recoverMobileCode(`+86${phone}`));
  await page.getByRole("button", { name: "登录", exact: true }).click();

  // The third step renders INLINE — no register-page routing.
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));
  await expect(page.getByText("该手机号尚未注册")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // 学号 alone (password optional-empty) completes the registration
  // AND the login — straight onto the home with the PENDING banner.
  await page.getByLabel("学号").fill(studentNumber);
  await page.getByRole("button", { name: "完成注册并登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`), { timeout: 15_000 });
  await expect(page.getByText("账号待管理员审核。")).toBeVisible({ timeout: 15_000 });
});
