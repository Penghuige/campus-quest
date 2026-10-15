/**
 * Admin staff-invitation issuance e2e (spec §5.8 step 1; the §8 Admin
 * surface, owner request 2026-10-14). Two pinned flows:
 *
 * 1. the happy path: issue an invitation, read the ONE-TIME link, and
 *    FOLLOW it — the link must route to the real accept page
 *    (/staff/invite/[token]), which is the end-to-end proof that link
 *    building joins the page origin and the deployment mount prefix
 *    correctly (the /campus production mount's 404 class of bug);
 * 2. the client validation branch (reviewer ruling 2026-10-15): a
 *    malformed email is refused client-side with the field error and
 *    ZERO POSTs — the backend's 20/hour admin rate bucket counts
 *    requests, not successes, so a client-rejected value must never
 *    reach the wire. (The 409 already-registered branch is
 *    accept-side today; its create-side arrival rides the V1.1
 *    invitation-lifecycle scope — until then unit tests pin the copy.)
 *
 * Staff login rides the SHARED fixtures helper (shot-surfaces'
 * precedent) — per-spec TOTP copies are how this file's first draft
 * shipped a broken base32 decoder and earned three 401 rounds before
 * the probe caught it (2026-10-15); no local crypto here.
 *
 * Environment contract: identical to admin.spec.ts (CQ_E2E=1 plus the
 * world-exported CQ_E2E_ADMIN / CQ_E2E_ADMIN_TOTP_SECRET — a missing
 * export FAILS, never skips).
 */
import { expect, staffLogin, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;
const RUN = process.env.CQ_E2E_RUN;

test.skip(
  !E2E_ENABLED,
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

// The staff contract itself is a HARD requirement (admin.spec's
// convention): the world always provides it; a missing export must fail
// the suite — never read as a skip the gate would have to catch.

test.describe("admin staff invitations (spec §5.8 step 1)", () => {
  test.beforeEach(async ({ page }) => {
    await staffLogin(page, ADMIN!, ADMIN_TOTP_SECRET!);
    await page.goto(`${BASE_URL}/admin/invitations`);
    // The shell mounted the page (not the permission guidance).
    await expect(page.getByRole("heading", { name: "员工邀请" })).toBeVisible();
  });

  test("nav carries the invitation entry; issuing renders the one-time link and the link routes to the accept page", async ({
    page,
  }) => {
    await expect(page.getByRole("link", { name: "员工邀请" })).toBeVisible();

    // A run-unique address: this row is dead weight after the run (the
    // token is never accepted), and re-runs must never collide with a
    // previously invited address in a reused world.
    const invitedEmail = `e2e.invited.${RUN ?? Date.now()}@example.edu`;
    await page.getByLabel("受邀人邮箱").fill(invitedEmail);
    await expect(page.getByLabel("角色")).toHaveValue("TEACHER");
    await page.getByRole("button", { name: "创建邀请" }).click();

    // The one-time window: link, notices, copy affordance.
    const link = page.getByTestId("invitation-link");
    await expect(link).toBeVisible();
    const linkText = await link.textContent();
    expect(linkText).toBeTruthy();
    // Same origin as the page itself (never a hardcoded host) and the
    // accept-route segment with a URL-safe token tail.
    expect(linkText!.startsWith(page.url().split("/admin")[0])).toBe(true);
    expect(linkText!).toMatch(/\/staff\/invite\/[A-Za-z0-9_-]{20,}$/);
    await expect(page.getByText("邀请链接只显示这一次", { exact: false })).toBeVisible();
    // Scoped to the one-time alert: the page subtitle carries the same
    // "48 小时" copy (strict-mode would see both).
    await expect(
      page.getByRole("alert").filter({ hasText: "48 小时内有效" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "复制邀请链接" }),
    ).toBeVisible();
    await expect(page.getByText(invitedEmail)).toBeVisible();

    // FOLLOW the link: it must land on the real accept page with the
    // password step armed (the mount-prefix + route-join proof).
    await page.goto(linkText!);
    await expect(
      page.getByRole("heading", { name: "接受员工邀请" }),
    ).toBeVisible();
    await expect(page.getByLabel("设置密码")).toBeVisible();
  });

  test("a malformed email is refused client-side: field error, ZERO issuance POSTs", async ({
    page,
  }) => {
    const issuancePosts: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/v1/staff/invitations")) {
        issuancePosts.push(request.url());
      }
    });

    await page.getByLabel("受邀人邮箱").fill("not-an-email");
    await page.getByRole("button", { name: "创建邀请" }).click();

    await expect(page.getByText("请输入有效的邮箱地址")).toBeVisible();
    await page.waitForTimeout(500);
    expect(
      issuancePosts,
      "a client-rejected email must never reach the wire (the 20/hour bucket counts requests)",
    ).toEqual([]);
    // The one-time window never opened.
    await expect(page.getByTestId("invitation-link")).toHaveCount(0);
  });
});
