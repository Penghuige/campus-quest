/**
 * CampusQuest teacher workspace e2e — Plan 09 Task 9, wired to the
 * runner and seeded for real by Plan 10's world (PR #6 final review:
 * the CQ_E2E_STAFF* contract below is the browser_world export, so
 * every test RUNS under the gate — the release gate asserts this
 * suite at zero skips).
 *
 * Every test is skipped unless CQ_E2E=1 (the suite-wide convention),
 * so importing the file can never depend on a live backend during
 * ordinary development.
 *
 * Environment contract (the seeded world's; defaults work against the
 * orchestrated local servers):
 * - CQ_E2E=1                enable the suite (required);
 * - CQ_E2E_BASE_URL         frontend origin (default https://localhost:3000);
 * - CQ_E2E_STAFF_LOGIN_URL  staff login page (default $CQ_E2E_BASE_URL/staff/login);
 * - CQ_E2E_STAFF            pre-seeded TEACHER credentials
 *                           "email:password" + CQ_E2E_STAFF_TOTP_SECRET
 *                           (the run's answerable browser teacher — the
 *                           code is computed IN-TEST via node:crypto
 *                           RFC 6238, what a real authenticator shows
 *                           for the same secret);
 * - CQ_E2E_STAFF2 / CQ_E2E_STAFF2_TOTP_SECRET  an UNRELATED second
 *                           teacher for the access-denied negative (F6);
 * - CQ_E2E_STUDENT          a seeded student for the role-negative.
 *
 * Brief flow: task-create -> import (preview/confirm) -> publish, plus
 * the review-decision surfaces' gating copy (mandatory note/reason, the
 * invalidate warning) against the VALIDATED queue row the fixture
 * seeds on the browser teacher's own task.
 */
import { createHmac } from "node:crypto";

import { expect, test } from "@playwright/test";

import { mintToken } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const STAFF_LOGIN_URL = process.env.CQ_E2E_STAFF_LOGIN_URL ?? `${BASE_URL}/login`;
const STAFF = process.env.CQ_E2E_STAFF; // "e2e-teacher-{run}@school.edu:password" (factories' DEFAULT_PASSWORD)
const STAFF_TOTP_SECRET = process.env.CQ_E2E_STAFF_TOTP_SECRET;
// An UNRELATED teacher for the access-denied negative (F6): a second
// seeded account with no collaboration on the first teacher's task.
const STAFF2 = process.env.CQ_E2E_STAFF2;
const STAFF2_TOTP_SECRET = process.env.CQ_E2E_STAFF2_TOTP_SECRET;

test.skip(
  !E2E_ENABLED,
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
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
export function totpCode(secret: string, atMs: number = Date.now()): string {
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

/** Staff login through the real form; retries across a 30s step rollover.
 * The success wait is the NEGATED login URL (community.spec's proven
 * shape): an unanchored `BASE_URL/` match would also match /staff/login
 * itself and let the test navigate away while the login POST is still
 * in flight — aborting it and landing the next page anonymous. */
async function loginAsStaff(
  page: import("@playwright/test").Page,
  credentials: string,
  totpSecret: string,
): Promise<void> {
  const [email, password] = credentials.split(":");
  await page.goto(STAFF_LOGIN_URL);
  // The unified page: the weak link expands the password form; an
  // ADMIN's password answers totp_required (the leg completes it).
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(email);
  await page.getByLabel("密码").fill(password);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.getByRole("button", { name: "登录", exact: true }).click();
    const legAppeared = await page
      .getByTestId("unified-login-totp-leg")
      .waitFor({ state: "visible", timeout: 5_000 })
      .then(() => true)
      .catch(() => false);
    if (legAppeared) {
      await page.getByLabel("动态验证码").fill(totpCode(totpSecret));
      await page.getByRole("button", { name: "完成登录" }).click();
    }
    try {
      await expect(page).not.toHaveURL(/\/login/, { timeout: 5_000 });
      return;
    } catch {
      // A slow hop can carry the submit across the step boundary —
      // recompute the code exactly like a real user would.
    }
  }
  await expect(page).not.toHaveURL(/\/staff\/login/);
}

/** The §7.1 CSV payload: a valid row plus a duplicate-in-file error row.
 * The platform must be one of the closed import vocabulary
 * (xiaohongshu/douyin/zhihu) — the historical "weibo" row hit
 * UNSUPPORTED_PLATFORM on its first real run. */
function assignmentsCsv(): string {
  return "platform,keyword\nxiaohongshu,图书馆\nxiaohongshu,图书馆\n";
}

test.describe("teacher workspace (brief: create -> import -> publish)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsStaff(page, STAFF!, STAFF_TOTP_SECRET!);
  });

  test("create draft -> edit-in schema -> import preview/confirm -> publish; unrelated teacher denied", async ({
    page,
    browser,
  }) => {
    const apiBase =
      process.env.CQ_E2E_API_URL ?? "http://localhost:8100/api/v1";
    const teacherToken = mintToken(process.env.CQ_E2E_TEACHER_ID!);

    // The 2026-10-23 simplification: the UI's one-click create fills
    // ONLY the business questions; every advanced field arrives with a
    // working default. The edit surface gets exercised below.
    await page.goto(`${BASE_URL}/teacher/tasks`);
    await page.getByRole("button", { name: "新建任务" }).first().click();
    const dialog = page.getByRole("dialog", { name: "新建任务" });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("任务标题").fill(`e2e 教师任务 ${Date.now()}`);
    await dialog.getByLabel("任务描述").fill("e2e 创建的采集任务");
    // target_count is the one required number (spec §3.1).
    await dialog.getByLabel("需要多少人完成").fill("3");
    // The schema is OPTIONAL now and starts EMPTY; 使用模板 offers the
    // strict DSL's starting point on demand.
    await dialog.getByText("高级（可选）：奖励、文件类型、校验规则等").click();
    const schemaField = dialog.locator("#task-schema");
    await expect(schemaField).toHaveValue("");
    // Spec §3.1's 通知关闭: the advanced fold carries the policy OFF by
    // default — the wire check after creation proves this is what lands.
    await expect(
      dialog.getByRole("checkbox", { name: "提前 24 小时提醒" }),
    ).not.toBeChecked();
    await expect(
      dialog.getByRole("checkbox", { name: "提前 4 小时提醒" }),
    ).not.toBeChecked();
    await expect(dialog.getByRole("checkbox", { name: "短信" })).not.toBeChecked();
    await dialog.getByRole("button", { name: "使用模板" }).click();
    await expect(schemaField).toHaveValue(/"platform"/);
    await schemaField.fill('{"columns":["platform","keyword"]}');
    await dialog.locator("#task-schema-version").fill("1");
    await dialog.getByRole("button", { name: "创建草稿" }).click();

    // The draft row appears in the workbench list with the 草稿 badge.
    const row = page.locator("tr", { hasText: "e2e 教师任务" }).first();
    await expect(row).toBeVisible();
    await expect(row.getByText("草稿")).toBeVisible();

    // Detail: the one-click create minted three 通用 units in the same
    // transaction (spec §3.2) — the statistics show them up front.
    await row.getByRole("link").click();
    await expect(page).toHaveURL(/\/teacher\/tasks\/.+/);
    const detailUrl = page.url();
    await expect(page.getByText("可领取单元")).toBeVisible();
    await expect(page.getByText("总单元")).toBeVisible();
    await expect(
      page.locator(".metric-row").getByText("3", { exact: true }).first(),
    ).toBeVisible();

    // The one-click form's OFF notification policy is what the server
    // stored (the §3.1 default is not a client-side illusion).
    const createdId = detailUrl.split("/").pop();
    // The student-facing detail lives at /tasks/{id} — the teacher URL
    // is staff-guarded and would never show the claim panel.
    const studentTaskUrl = `${BASE_URL}/tasks/${createdId}`;
    const createdResp = await page.request.get(
      `${apiBase}/teacher/tasks/${createdId}`,
      { headers: { Authorization: `Bearer ${teacherToken}` } },
    );
    expect(createdResp.ok(), await createdResp.text()).toBe(true);
    const createdJson = (await createdResp.json()) as {
      notify_24h: boolean;
      notify_4h: boolean;
      notification_channels: string[];
    };
    expect(createdJson.notify_24h).toBe(false);
    expect(createdJson.notify_4h).toBe(false);
    expect(createdJson.notification_channels).toEqual([]);

    // Edit-in the schema exactly as the create dialog left it; the
    // detail refetch renders the stored schema in the facts.
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    const editDialog = page.getByRole("dialog", { name: "编辑任务" });
    await expect(editDialog).toBeVisible();
    await editDialog.getByText("高级（可选）：奖励、文件类型、校验规则等").click();
    await editDialog.locator("#task-schema").fill('{"columns":["platform","keyword"]}');
    await editDialog.locator("#task-schema-version").fill("1");
    await editDialog.getByRole("button", { name: "保存修改" }).click();
    await expect(editDialog).not.toBeVisible();
    await expect(page.getByText(/v1.*columns/)).toBeVisible();

    // Publish straight off the minted units: explicit confirm, badge flips.
    await page.getByRole("button", { name: "发布", exact: true }).click();
    const confirm = page.getByRole("dialog", { name: "发布任务" });
    await expect(confirm).toBeVisible();
    await expect(confirm.getByText(/发布后任务立即对学生可见/)).toBeVisible();
    await expect(confirm.getByText(/不可修改/)).toBeVisible();
    await confirm.getByRole("button", { name: "确认发布" }).click();
    await expect(page.locator(".page-head").getByText("已发布")).toBeVisible();

    // The published task is claimable: a student session walks its
    // detail and lands the claim panel (the one-click loop closed).
    const studentCtx = await browser.newContext();
    const studentPage = await studentCtx.newPage();
    const [studentNumber, studentPassword] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
    await studentPage.goto(`${BASE_URL}/login`);
    await studentPage.getByRole("button", { name: "使用密码登录" }).click();
    await studentPage.getByLabel("账号", { exact: true }).fill(studentNumber);
    await studentPage.getByLabel("密码").fill(studentPassword);
    await studentPage.getByRole("button", { name: "登录", exact: true }).click();
    await expect(studentPage).not.toHaveURL(/\/login/, { timeout: 10_000 });
    await studentPage.goto(studentTaskUrl);
    await studentPage.getByRole("button", { name: "领取任务" }).click();
    await expect(studentPage.locator(".claim-panel")).toBeVisible();
    await studentCtx.close();

    // F6: an UNRELATED teacher (no ownership/collaboration) opening this
    // task's detail gets the access-denied panel, not broken sections.
    const context = await browser.newContext();
    const other = await context.newPage();
    await loginAsStaff(other, STAFF2!, STAFF2_TOTP_SECRET!);
    await other.goto(detailUrl);
    await expect(
      other.getByText("任务不存在，或您不是该任务的所有者 / 协作者，无法访问。"),
    ).toBeVisible();
    await expect(other.getByRole("link", { name: "返回任务管理" })).toBeVisible();
    await context.close();

    // The import path keeps its coverage on a UNITS-LESS draft (minted
    // through the API — the UI requires target_count): publish is
    // GATED with the friendly copy, then the import unlocks it.
    const bareDraft = await page.request.post(`${apiBase}/teacher/tasks`, {
      headers: { Authorization: `Bearer ${teacherToken}` },
      data: {
        title: `e2e 导入任务 ${Date.now()}`,
        description: "手动导入单元的任务",
        base_reward_points: 10,
        deadline_mode: "RELATIVE",
        allowed_file_types: ["CSV"],
        max_file_size_bytes: 10 * 1024 * 1024,
        task_type: "DATA_CRAWL",
        rarity: "NORMAL",
        fixed_deadline_at: null,
        duration_minutes: 10080,
        claim_cutoff_minutes: 60,
        submission_schema: null,
        submission_schema_version: null,
        notify_24h: true,
        notify_4h: true,
        notification_channels: ["SMS", "EMAIL", "IN_APP"],
      },
    });
    expect(bareDraft.ok(), await bareDraft.text()).toBe(true);
    const bare = (await bareDraft.json()) as { id: string };
    await page.goto(`${BASE_URL}/teacher/tasks/${bare.id}`);

    // Zero units: the precondition is VISIBLE before the call (task
    // brief P1.3) — the publish verb renders disabled with its reason,
    // and the lenient-schema fact says so. The server-side gate's copy
    // mapping stays pinned by the unit test (the UI cannot open the
    // dialog while the precondition holds).
    await expect(page.getByText("未配置（宽松校验）")).toBeVisible();
    const zeroPublish = page.getByRole("button", { name: "发布", exact: true });
    await expect(zeroPublish).toBeDisabled();
    await expect(
      page.getByText("请先设置完成人数（任务单元）再发布"),
    ).toBeVisible();

    // The import flow (spec §7.1 preview -> explicit confirm -> summary).
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

    // The landed import refetches the statistics; the precondition
    // clears and the same verb publishes.
    await expect(zeroPublish).toBeEnabled();
    await zeroPublish.click();
    const secondConfirm = page.getByRole("dialog", { name: "发布任务" });
    await secondConfirm.getByRole("button", { name: "确认发布" }).click();
    await expect(page.locator(".page-head").getByText("已发布")).toBeVisible();

    // The LENIENT validation (spec 2026-10-23 §2.1): the bare task has
    // NO schema — any parseable CSV with a header and one data row
    // passes the machine check and waits for the teacher's review.
    const lenientCtx = await browser.newContext();
    const lenientPage = await lenientCtx.newPage();
    const [lenientNumber, lenientPassword] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
    await lenientPage.goto(`${BASE_URL}/login`);
    await lenientPage.getByRole("button", { name: "使用密码登录" }).click();
    await lenientPage.getByLabel("账号", { exact: true }).fill(lenientNumber);
    await lenientPage.getByLabel("密码").fill(lenientPassword);
    await lenientPage.getByRole("button", { name: "登录", exact: true }).click();
    await expect(lenientPage).not.toHaveURL(/\/login/, { timeout: 10_000 });
    await lenientPage.goto(`${BASE_URL}/tasks/${bare.id}`);
    await lenientPage.getByRole("button", { name: "领取任务" }).click();
    await expect(lenientPage.locator(".claim-panel")).toBeVisible();
    // A CSV the strict DSL would refuse (no schema is configured, so
    // the columns are free): header + one data row.
    await lenientPage.locator("#submission-file").setInputFiles({
      name: "lenient.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("anything,goes\nhello,world\n", "utf8"),
    });
    await lenientPage.getByRole("button", { name: "开始上传" }).click();
    await expect(
      lenientPage.getByText("已提交，等待老师审核").first(),
    ).toBeVisible({ timeout: 120_000 });
    await lenientCtx.close();
  });
  test("student session gets the permission-denied panel, not broken controls", async ({
    browser,
  }) => {
    // The teacher session from beforeEach navigated; open a student context.
    const context = await browser.newContext();
    const studentPage = await context.newPage();
    const [username, password] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
    await studentPage.goto(`${BASE_URL}/login`);
    await studentPage.getByRole("button", { name: "使用密码登录" }).click();
    await studentPage.getByLabel("账号", { exact: true }).fill(username);
    await studentPage.getByLabel("密码").fill(password);
    await studentPage.getByRole("button", { name: "登录", exact: true }).click();
    // Wait for the login to actually LAND before navigating (the same
    // in-flight-abort race the staff helper above guards against).
    await expect(studentPage).not.toHaveURL(/\/login/, { timeout: 10_000 });
    await studentPage.goto(`${BASE_URL}/teacher/tasks`);
    await expect(
      studentPage.getByText("教师工作台仅对教师与管理员开放"),
    ).toBeVisible();
    await context.close();
  });

  test("the workspace carries its own profile page (defect #17)", async ({
    page,
  }) => {
    // 个人信息 is a first-class nav destination post-login.
    await page.goto(`${BASE_URL}/teacher/reviews`);
    const nav = page.getByRole("link", { name: "个人信息", exact: true });
    await expect(nav).toBeVisible();
    await nav.click();
    await expect(page).toHaveURL(/\/teacher\/profile/);

    // The /me facts render as definition rows (the owner's own account
    // view, spec §40). 注册日期 joined with PR #46's created_at —
    // date-only, year-inclusive, so the row must read 年.
    const section = page.getByRole("region", { name: "个人信息" });
    await expect(section).toBeVisible();
    for (const label of ["昵称", "账号", "角色", "账号状态", "注册日期"]) {
      await expect(section.getByText(label, { exact: true })).toBeVisible();
    }
    await expect(section.getByText("教师", { exact: true })).toBeVisible();
    await expect(section.getByText("正常", { exact: true })).toBeVisible();
    const registration = section
      .locator(".fact-row", { hasText: "注册日期" })
      .locator(".fact-value");
    await expect(registration).toContainText(/年/);
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
    await expect(page.getByText(/判无效会取消锁定的奖励与积分/)).toBeVisible();
    await page.getByRole("button", { name: "确认判无效" }).click();
    await expect(page.getByText("判无效原因不能为空")).toBeVisible();
    await page.getByRole("button", { name: "取消" }).click();

    // 通过 shows the explicit grant confirm.
    await page.getByRole("button", { name: "通过并发放奖励" }).click();
    await expect(page.getByText(/该领取将标记完成/)).toBeVisible();
    await page.getByRole("button", { name: "取消" }).click();
  });
});
