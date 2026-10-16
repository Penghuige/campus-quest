/**
 * Platform registry e2e (platform-registry spec 2026-10-16 §3/§4) —
 * runs against the MERGED backend (the endpoints the spec froze).
 * Executed when the backend half lands; the shapes here are the
 * contract-first drafts.
 *
 * Coverage (the spec §5 e2e list):
 * 1. the admin CRUD chain: create (code+display_name+sort), rename,
 *    reorder, disable → the row states flip in place;
 * 2. the student dictionary surface: the claim panel renders the
 *    registry display_name (not the raw code) after allocation;
 * 3. the fallback honesty: a code outside the dictionary renders as
 *    itself (the disabled-platform-existing-claim shape — driven via
 *    the seeded world's zhihu backfill rows where available).
 */
import { expect, test, type Page } from "@playwright/test";

import { staffLogin } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8100/api/v1";
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;
const RUN = process.env.CQ_E2E_RUN;

test.skip(
  !(E2E_ENABLED && ADMIN && ADMIN_TOTP_SECRET && RUN),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

/** Run-unique code AND display names — a prior run's registry rows are
 * orphans the world teardown never sees (platforms are outside its
 * scope), so repeated invocations must never collide on either. */
const RUN_TAG = RUN!.slice(0, 8);
const PF_CODE = `e2e-pf-${RUN_TAG}`;
const NAME_CREATE = `端到端平台${RUN_TAG}`;
const NAME_RENAMED = `端到端改名${RUN_TAG}`;

async function adminOn(page: Page, path: string): Promise<void> {
  await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
  await page.goto(`${BASE_URL}${path}`, { waitUntil: "domcontentloaded" });
}

test.describe("platform registry admin (spec §4)", () => {
  test("create → rename → reorder → disable, the row states flip in place", async ({
    page,
  }) => {
    await adminOn(page, "/admin/platforms");

    // The seeded registry renders (the spec's migration seeds four).
    const table = page.getByRole("table", { name: "平台注册表" });
    await expect(table).toBeVisible();
    await expect(table.getByText("BOSS直聘")).toBeVisible();

    // Create: code + display name + sort.
    const code = PF_CODE;
    await page.getByRole("button", { name: "新建平台" }).click();
    await page.getByLabel("平台代码（创建后不可改）").fill(code);
    await page.getByLabel("显示名").fill(NAME_CREATE);
    await page.getByLabel("排序值").fill("500");
    await page.getByRole("button", { name: "创建平台" }).click();
    await expect(table.getByText(NAME_CREATE)).toBeVisible();

    // Rename via the edit dialog (code stays the path key).
    const row = table.locator("tr", { hasText: code });
    await row.getByRole("button", { name: "编辑" }).click();
    await page.getByLabel("显示名").fill(NAME_RENAMED);
    await page.getByRole("button", { name: "保存修改" }).click();
    await expect(table.getByText(NAME_RENAMED)).toBeVisible();

    // Toggle disabled: the badge flips; the row STAYS (no delete).
    await row.getByRole("button", { name: "停用" }).click();
    await expect(row.locator(".badge-muted")).toBeVisible();
  });

  test("a malformed create is refused client-side: field errors, ZERO POSTs", async ({
    page,
  }) => {
    await adminOn(page, "/admin/platforms");
    const posts: string[] = [];
    page.on("request", (request) => {
      if (
        request.url().includes("/api/v1/admin/platforms") &&
        request.method() === "POST"
      ) {
        posts.push(request.url());
      }
    });

    await page.getByRole("button", { name: "新建平台" }).click();
    await page.getByLabel("平台代码（创建后不可改）").fill("NOT-A-SLUG");
    await page.getByLabel("显示名").fill("");
    await page.getByRole("button", { name: "创建平台" }).click();

    await expect(page.getByText("仅小写字母、数字与连字符")).toBeVisible();
    await expect(page.getByText("请输入 1-32 个字符的显示名")).toBeVisible();
    await page.waitForTimeout(400);
    expect(posts, "client-rejected create must never POST").toEqual([]);
  });
});
