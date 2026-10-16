/**
 * Nickname rename nudge e2e (V1.1 §6; plan Task 10) — the render-path
 * proof over the shared island, mounted in all three shells:
 *
 * - the POSITIVE case rides a REAL account whose nickname is
 *   auto-shaped: registration requires a chosen nickname, but nothing
 *   forbids CHOOSING the student number itself (all digits — the
 *   predicate's first arm), so the register → login path produces a
 *   genuinely-nudged session without touching the shared world's
 *   staff accounts (whose 端到端* nicknames never trigger);
 * - dismissal persists across a full reload (the per-account
 *   localStorage key), and the chosen-nickname negative proves no
 *   false positives on the seeded student.
 *
 * The staff positive (email local-part nicknames) lands with the
 * Wave-1 email-code specs against the merged backend.
 */

import { ensureStudentLogin, expect, staffLogin, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;
// Run-unique number: this flow OWNS its registration (the shared
// CQ_E2E_REGISTER_NUMBER belongs to auth.spec's register test).
const NUDGE_NUMBER = process.env.CQ_E2E_NUDGE_NUMBER ?? `5${String(Date.now()).slice(-9)}`;

test.skip(
  !(E2E_ENABLED && RUN && ADMIN && ADMIN_TOTP_SECRET),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

test.describe("nickname rename nudge (V1.1 §6)", () => {
  test("an auto-shaped nickname nudges once; dismissal survives a reload", async ({ page }) => {
    // Auth extension II: registration needs no whitelist seeding (the
    // gate is gone); the account lands PENDING_APPROVAL — browsable,
    // so the nudge's presence/absence is observable either way.

    // Register with the NUMBER ITSELF as the chosen nickname (digits —
    // the predicate's first arm). The phone is TIME-derived: a
    // registered user is not part of the world file (teardown's clean
    // never sees it), so a RUN-derived phone collides with the previous
    // invocation's leftover row (该手机号已绑定其他账号).
    const phone = `138${String(Date.now()).slice(-8)}`;
    await page.goto(`${BASE_URL}/register`);
    await page.getByLabel("学号").fill(NUDGE_NUMBER);
    await page.getByLabel("昵称").fill(NUDGE_NUMBER);
    await page.getByLabel("手机号").fill(phone);
    await page.getByRole("button", { name: "获取验证码" }).click();
    await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
    const { recoverOtpCode } = await import("./fixtures");
    await page.getByLabel("短信验证码").fill(recoverOtpCode(`+86${phone}`));
    await page.getByLabel("密码（可选）").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "注册", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login\\?registered=1`));

    // Password login (the tab behind the default mobile channel) ->
    // the workspace greets the digit nickname with the nudge (pending
    // or not, the shell mounts the banner family for the logged-in).
    await page.getByRole("button", { name: "密码登录" }).click();
    await page.getByLabel("学号").fill(NUDGE_NUMBER);
    await page.getByLabel("密码").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
    await expect(page.getByText("把昵称改成好认的名字吧")).toBeVisible();
    await expect(page.getByRole("link", { name: "把昵称改成好认的名字吧" })).toHaveAttribute(
      "href",
      "/profile",
    );

    // Dismiss, then a full reload must NOT bring it back.
    await page.getByRole("button", { name: "暂不修改" }).click();
    await expect(page.getByText("把昵称改成好认的名字吧")).toHaveCount(0);
    await page.reload();
    await expect(page.getByText("把昵称改成好认的名字吧")).toHaveCount(0);
  });

  test("a chosen nickname never triggers (the seeded student)", async ({ page }) => {
    await ensureStudentLogin(page);
    await expect(page.getByText("把昵称改成好认的名字吧")).toHaveCount(0);
  });

  test("the seeded staff nickname never triggers (admin shell)", async ({ page }) => {
    await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
    await page.goto(`${BASE_URL}/admin/users`);
    await expect(page.getByText("把昵称改成好认的名字吧")).toHaveCount(0);
  });
});
