/**
 * CampusQuest admin workspace e2e — Plan 09 Task 10, wired to the
 * runner and seeded for real by Plan 10's world (PR #6 final review:
 * the CQ_E2E_TEACHER/CQ_E2E_ADMIN contracts below are the
 * browser_world exports, so every test RUNS under the gate — the
 * release gate asserts this suite at zero skips).
 *
 * Every test is skipped unless CQ_E2E=1 (the suite-wide convention),
 * so importing the file can never depend on a live backend during
 * ordinary development.
 *
 * Environment contract (the seeded world's; defaults work against the
 * orchestrated local servers):
 * - CQ_E2E=1                enable the suite (required);
 * - CQ_E2E_BASE_URL         frontend origin (default http://localhost:3000);
 * - CQ_E2E_STAFF_LOGIN_URL  staff login page (default $CQ_E2E_BASE_URL/staff/login);
 * - CQ_E2E_TEACHER          pre-seeded TEACHER credentials
 *                           "email:password" + CQ_E2E_TEACHER_TOTP_SECRET
 *                           (the step-1 privilege negative: a teacher
 *                           opening /admin/* gets guidance and ZERO
 *                           admin API calls);
 * - CQ_E2E_ADMIN            pre-seeded ADMIN credentials
 *                           "email:password" + CQ_E2E_ADMIN_TOTP_SECRET
 *                           (the operational flows: the world seeds
 *                           the pending redemption, the suspend-target
 *                           student, and the review-queue row).
 *
 * Brief flow: privilege navigation (teacher denied on /admin/whitelist,
 * no admin data request fires), whitelist import preview/confirm,
 * user suspend with mandatory reason, redemption decisions, and the
 * settings confirm dialog.
 */
import { createHmac } from "node:crypto";

import { expect, test, type Page } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const STAFF_LOGIN_URL = process.env.CQ_E2E_STAFF_LOGIN_URL ?? `${BASE_URL}/staff/login`;
const TEACHER = process.env.CQ_E2E_TEACHER;
const TEACHER_TOTP_SECRET = process.env.CQ_E2E_TEACHER_TOTP_SECRET;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;

test.skip(
  !E2E_ENABLED,
  "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

// The staff contract itself is a HARD requirement now (PR #6 final
// review): the world always provides it, and a missing export must
// fail the suite — never read as a skip the gate would have to catch.

/** RFC 4648 base32 (the TOTP secret alphabet) -> bytes. */
function base32Decode(input: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of input.toUpperCase().replace(/=+$/, "")) {
    const index = alphabet.indexOf(char);
    if (index === -1) {
      throw new Error(`non-base32 character in secret: ${char}`);
    }
    value = (value << 5) | index;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** The current RFC 6238 code for `secret` (SHA-1, 30s step, 6 digits). */
function totpCode(secret: string, atMs: number = Date.now()): string {
  const counter = Math.floor(atMs / 30_000);
  const block = Buffer.alloc(8);
  block.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac("sha1", base32Decode(secret)).update(block).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary =
    ((digest[offset] & 0x7f) << 24) |
    ((digest[offset + 1] & 0xff) << 16) |
    ((digest[offset + 2] & 0xff) << 8) |
    digest[offset + 3];
  return String(binary % 1_000_000).padStart(6, "0");
}

/** Staff login through the real form; retries across a 30s step rollover.
 * The success wait is the NEGATED login URL (community.spec's proven
 * shape): an unanchored `BASE_URL/` match would also match /staff/login
 * itself and let the test navigate away while the login POST is still
 * in flight — aborting it and landing the next page anonymous. */
async function loginAsStaff(
  page: Page,
  credentials: string,
  totpSecret: string,
): Promise<void> {
  const [email, password] = credentials.split(":");
  await page.goto(STAFF_LOGIN_URL);
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码").fill(password);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.getByLabel("动态验证码").fill(totpCode(totpSecret));
    await page.getByRole("button", { name: "登录", exact: true }).click();
    try {
      await expect(page).not.toHaveURL(/\/staff\/login/, { timeout: 5_000 });
      return;
    } catch {
      // A slow hop can carry the submit across the step boundary —
      // recompute the code exactly like a real user would.
    }
  }
  await expect(page).not.toHaveURL(/\/staff\/login/);
}

test.describe("admin workspace privilege navigation (plan step 1)", () => {
  test("a teacher opening /admin/whitelist gets guidance and ZERO admin API calls", async ({
    page,
  }) => {
    await loginAsStaff(page, TEACHER!, TEACHER_TOTP_SECRET!);

    // Arm the recorder BEFORE the first /admin navigation: the gate must
    // keep every admin data request from firing (the shell renders only
    // the guidance panel, never the pages).
    const adminRequests: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/v1/admin/")) {
        adminRequests.push(request.url());
      }
    });

    await page.goto(`${BASE_URL}/admin/whitelist`);
    await expect(page.getByText("管理后台仅对管理员开放，当前账号是教师账号")).toBeVisible();
    await expect(page.getByRole("link", { name: "返回教师工作台" })).toBeVisible();
    // No admin data rendered and no admin API call fired.
    await expect(page.getByRole("table")).toHaveCount(0);
    await page.waitForTimeout(500);
    expect(adminRequests, "no /admin API request may fire for a teacher session").toEqual([]);
  });
});

test.describe("admin workspace operations (brief: whitelist / users / redemptions / settings)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsStaff(page, ADMIN!, ADMIN_TOTP_SECRET!);
  });

  test("whitelist import: preview classifies rows, confirm writes the importable set", async ({
    page,
  }) => {
    await page.goto(`${BASE_URL}/admin/whitelist`);

    const stamp = String(Date.now()).slice(-8);
    const content = `${stamp}1\n${stamp}1\n${stamp}２\n${stamp}223456789012345`;
    await page.getByLabel("导入内容").fill(content);
    await page.getByRole("button", { name: "预览导入" }).click();

    // Preview: counts line + per-row decisions (in-file duplicate,
    // full-width digits, invalid length).
    await expect(page.getByText(/共 4 行：可导入 1 行/)).toBeVisible();
    await expect(page.getByText("文件内重复").first()).toBeVisible();
    await expect(page.getByText("全角数字").first()).toBeVisible();
    await expect(page.getByText("长度不合法").first()).toBeVisible();

    await page.getByRole("button", { name: /确认导入 1 行/ }).click();
    await expect(page.getByText(/已成功导入 1 个学号/)).toBeVisible();

    // The import section and the entries listing are independent: the
    // listing loads once on mount and does not auto-refresh on confirm
    // (the toggle path's own refresh contract) — reload to read the
    // durable row back.
    await page.reload();
    // The new entry appears in the listing with the 启用 badge.
    await expect(page.locator("tr", { hasText: `${stamp}1` }).getByText("启用")).toBeVisible();
  });

  test("user suspend blocks on a blank reason, then applies with one", async ({ page }) => {
    await page.goto(`${BASE_URL}/admin/users`);

    const row = page.locator("tr", { hasText: "e2e-suspended-student" });
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "停用" }).click();

    const dialog = page.locator("dialog[aria-labelledby='account-status-title']");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "确认停用" }).click();
    await expect(dialog.getByText("操作原因必填（将记入审计日志）")).toBeVisible();

    await dialog.getByLabel("操作原因").fill("e2e：违规行为核查期间临时停用");
    await dialog.getByRole("button", { name: "确认停用" }).click();
    await expect(dialog).not.toBeVisible();
    await expect(row.getByText("已停用")).toBeVisible();

    // Restore for reruns: reactivate with a reason.
    await row.getByRole("button", { name: "恢复" }).click();
    await dialog.getByLabel("操作原因").fill("e2e：恢复账号");
    await dialog.getByRole("button", { name: "确认恢复" }).click();
    await expect(row.getByText("正常")).toBeVisible();
  });

  test("redemption decisions: reject demands a reason; approve then fulfill", async ({ page }) => {
    await page.goto(`${BASE_URL}/admin/redemptions`);

    const firstRow = page.locator(".review-item").first();
    await expect(firstRow).toBeVisible();
    await firstRow.click();

    // Reject: blank reason is refused client-side (the transport mirror).
    await page.getByRole("button", { name: "拒绝申请" }).click();
    const reject = page.locator("dialog[aria-labelledby='redemption-reject-title']");
    await expect(reject).toBeVisible();
    await reject.getByRole("button", { name: "确认拒绝" }).click();
    await expect(reject.getByText("拒绝原因必填（将通过通知送达申请者）")).toBeVisible();
    await reject.getByRole("button", { name: "取消" }).click();

    // Approve: explicit confirm, then the row lands in 待发放 and fulfill
    // becomes available on the server verdict.
    await page.getByRole("button", { name: "通过并扣减积分" }).click();
    const approve = page.locator("dialog[aria-labelledby='redemption-approve-title']");
    await expect(approve.getByText(/将扣减积分并进入待发放状态/)).toBeVisible();
    await approve.getByRole("button", { name: "确认通过" }).click();
    // .first(): after approval the badge renders on BOTH the queue row
    // and the selected detail — the strict-mode ambiguity would fail
    // the otherwise-correct wait.
    await expect(page.getByText("已批准 · 待发放").first()).toBeVisible();

    await page.getByRole("button", { name: "标记已发放" }).click();
    const fulfill = page.locator("dialog[aria-labelledby='redemption-fulfill-title']");
    await fulfill.getByRole("button", { name: "确认发放" }).click();
    await expect(page.getByText("已发放").first()).toBeVisible();
  });

  test("settings: term edit confirms the old -> new diff before the PUT", async ({ page }) => {
    await page.goto(`${BASE_URL}/admin/system`);

    const termCard = page.locator(".panel", { hasText: "当前学期" }).first();
    const termInput = termCard.getByLabel("新学期值");
    await termInput.fill("e2e-term-2026-2");
    await termCard.getByRole("button", { name: "保存修改" }).click();

    const confirm = page.locator("dialog[aria-labelledby='setting-confirm-title']");
    await expect(confirm).toBeVisible();
    await expect(confirm.getByText(/切换学期后，新创建的兑换将快照新学期/)).toBeVisible();
    await confirm.getByRole("button", { name: "确认修改" }).click();
    await expect(confirm).not.toBeVisible();
    await expect(termCard.getByText("e2e-term-2026-2").first()).toBeVisible();
  });
});
