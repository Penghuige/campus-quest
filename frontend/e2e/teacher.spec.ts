/**
 * CampusQuest teacher workspace e2e — Plan 09 Task 9.
 *
 * STATUS: SPEC ONLY — NOT WIRED TO A RUNNER YET (the T2-T8 guard
 * pattern). Playwright is installed by Plan 10; until then this file
 * stays invisible to the gates (`tsconfig.json` includes only `src/**`,
 * ESLint globally ignores `e2e/**`, `next build` never touches it).
 * Every test is skipped unless CQ_E2E=1.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1                enable the suite (required);
 * - CQ_E2E_BASE_URL         frontend origin (default http://localhost:3000);
 * - CQ_E2E_STAFF_LOGIN_URL  staff login page (default $CQ_E2E_BASE_URL/staff/login);
 * - CQ_E2E_STAFF            pre-seeded TEACHER credentials
 *                           "email:password" (required; Plan 10's fixture
 *                           seeds the account);
 * - CQ_E2E_STAFF_TOTP_SECRET  base32 TOTP secret of that account
 *                           (required: staff login demands the second
 *                           factor; the code is computed IN-TEST via
 *                           node:crypto RFC 6238 — the T8 staff-auth
 *                           spec's helper, what a real authenticator
 *                           shows for the same secret).
 *
 * Brief flow: task-create -> import (preview/confirm) -> publish, plus
 * the review-decision surfaces' gating copy (mandatory note/reason, the
 * invalidate warning) against a queue the fixture seeds.
 */
import { createHmac } from "node:crypto";

import { expect, test } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const STAFF_LOGIN_URL = process.env.CQ_E2E_STAFF_LOGIN_URL ?? `${BASE_URL}/staff/login`;
const STAFF = process.env.CQ_E2E_STAFF; // "teacher@school.edu:correct-horse"
const STAFF_TOTP_SECRET = process.env.CQ_E2E_STAFF_TOTP_SECRET;

test.skip(
  !E2E_ENABLED,
  "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

const flowReady = STAFF !== undefined && STAFF_TOTP_SECRET !== undefined;
test.skip(
  !flowReady,
  "teacher flow needs CQ_E2E_STAFF (seeded teacher credentials) and CQ_E2E_STAFF_TOTP_SECRET (the account's base32 TOTP secret); Plan 10's fixture provides both.",
);

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
    (digest[offset + 1] << 16) |
    (digest[offset + 2] << 8) |
    digest[offset + 3];
  return String(binary % 1_000_000).padStart(6, "0");
}

/** Log in through the staff login page (T8 surface) with password + TOTP. */
async function loginAsTeacher(page: import("@playwright/test").Page): Promise<void> {
  const [email, password] = (STAFF ?? "").split(":");
  await page.goto(STAFF_LOGIN_URL);
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码").fill(password);
  await page
    .getByLabel("动态验证码")
    .fill(totpCode(STAFF_TOTP_SECRET!));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/`));
}

/** The §7.1 CSV payload: a valid row plus a duplicate-in-file error row. */
function assignmentsCsv(): string {
  return "platform,keyword\nweibo,图书馆\nweibo,图书馆\n";
}

test.describe("teacher workspace (brief: create -> import -> publish)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsTeacher(page);
  });

  test("create draft -> import preview/confirm -> publish -> status flips", async ({ page }) => {
    await page.goto(`${BASE_URL}/teacher/tasks`);

    // Create dialog: the publish-validation fields; obvious mistakes stay
    // client-side (a too-short reward never sends).
    await page.getByRole("button", { name: "新建任务" }).first().click();
    const dialog = page.locator("dialog[aria-labelledby='create-task-title']");
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("基础奖励积分").fill("0");
    await dialog.getByRole("button", { name: "创建草稿" }).click();
    await expect(dialog.getByText("基础奖励积分必须大于 0")).toBeVisible();

    await dialog.getByLabel("任务标题").fill(`e2e 教师任务 ${Date.now()}`);
    await dialog.getByLabel("任务描述").fill("e2e 创建的采集任务");
    await dialog.getByLabel("基础奖励积分").fill("120");
    await dialog.getByLabel("固定截止时间").fill("2030-09-30T18:00");
    await dialog.locator("#task-schema").fill('{"columns":["platform","keyword"]}');
    await dialog.locator("#task-schema-version").fill("1");
    await dialog.getByRole("button", { name: "创建草稿" }).click();

    // The draft row appears in the workbench list with the 草稿 badge.
    const row = page.locator("tr", { hasText: "e2e 教师任务" }).first();
    await expect(row).toBeVisible();
    await expect(row.getByText("草稿")).toBeVisible();

    // Detail: import flow (spec §7.1 preview -> explicit confirm -> summary).
    await row.getByRole("link").click();
    await page.getByLabel("任务单元导入").scrollIntoViewIfNeeded();
    await page.locator("#assignment-import-file").setInputFiles({
      name: "assignments.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(assignmentsCsv(), "utf8"),
    });

    const preview = page.locator(".import-preview");
    await expect(preview).toBeVisible();
    // Counts line: 2 rows total, 1 importable, 1 problematic (in-file dup).
    await expect(preview.getByText(/共 2 行：可导入 1 行，存在问题 1 行/)).toBeVisible();
    await expect(preview.getByText("第 2 行")).toBeVisible();
    await expect(
      preview.getByText("文件内重复的 platform + keyword 组合"),
    ).toBeVisible();

    await preview.getByRole("button", { name: /确认导入 1 行/ }).click();
    await expect(page.getByText("已成功导入 1 个任务单元")).toBeVisible();

    // Publish from the detail head: explicit confirm, then the badge flips.
    await page.getByRole("button", { name: "发布", exact: true }).click();
    const confirm = page.locator("dialog[aria-labelledby='lifecycle-confirm-title']");
    await expect(confirm).toBeVisible();
    await expect(confirm.getByText(/发布后任务立即对学生可见/)).toBeVisible();
    await confirm.getByRole("button", { name: "确认发布" }).click();
    await expect(page.locator(".page-head").getByText("已发布")).toBeVisible();
  });

  test("student session gets the permission-denied panel, not broken controls", async ({
    page,
    browser,
  }) => {
    test.skip(
      process.env.CQ_E2E_STUDENT === undefined,
      "needs CQ_E2E_STUDENT (seeded student credentials) for the negative check",
    );
    // The teacher session from beforeEach navigated; open a student context.
    const context = await browser.newContext();
    const studentPage = await context.newPage();
    const [username, password] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
    await studentPage.goto(`${BASE_URL}/login`);
    await studentPage.getByLabel("学号").fill(username);
    await studentPage.getByLabel("密码").fill(password);
    await studentPage.getByRole("button", { name: "登录", exact: true }).click();
    await studentPage.goto(`${BASE_URL}/teacher/tasks`);
    await expect(
      studentPage.getByText("教师工作台仅对教师与管理员开放"),
    ).toBeVisible();
    await context.close();
  });

  test("review decisions: mandatory note/reason and the invalidate warning", async ({
    page,
  }) => {
    await page.goto(`${BASE_URL}/teacher/reviews`);
    const firstRow = page.locator(".review-item").first();
    await expect(firstRow).toBeVisible();
    // §12.4 UI list fields on the row: platform/keyword pair + locked tier.
    await expect(firstRow.locator(".review-pair")).toBeVisible();
    await expect(firstRow.locator(".review-tier")).toContainText(/积分|未锁定/);

    await firstRow.click();
    await expect(page.getByRole("heading", { name: "审核提交" })).toBeVisible();

    // 退回修改: a blank note is refused client-side (transport mirror).
    await page.getByRole("button", { name: "退回修改" }).click();
    await page.getByRole("button", { name: "确认退回" }).click();
    await expect(page.getByText("退回说明不能为空")).toBeVisible();
    await page.getByRole("button", { name: "取消" }).click();

    // 判无效: the prominent warning renders BEFORE the mandatory reason.
    await page.getByRole("button", { name: "判无效", exact: true }).click();
    await expect(page.getByText("判无效将取消该学生当前锁定的奖励档位与积分")).toBeVisible();
    await page.getByRole("button", { name: "确认判无效" }).click();
    await expect(page.getByText("判无效原因不能为空")).toBeVisible();
    await page.getByRole("button", { name: "取消" }).click();

    // 通过 shows the explicit grant confirm.
    await page.getByRole("button", { name: "通过并发放奖励" }).click();
    await expect(page.getByText(/确认后该领取将标记为已完成/)).toBeVisible();
    await page.getByRole("button", { name: "取消" }).click();
  });
});
