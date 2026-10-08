/**
 * P3-B: cross-browser smoke — Firefox + WebKit ride a deliberately
 * NARROW subset (the trade-off is recorded in the PR: a full
 * three-browser matrix triples CI time for marginal signal — the
 * Chromium full suite remains the behavioral authority; the smoke
 * subset catches engine-level rendering/API breaks where they most
 * likely appear: auth → task discovery → the claim flow's client
 * island → one staff surface).
 *
 * Runs under --project=firefox / --project=webkit ONLY; the default
 * Chromium run collects nothing from this file, so the main suite and
 * its baselines are untouched.
 *
 * WEBKIT NOTE: Playwright's webkit fill() sets the DOM value without
 * the input event React's controlled components listen for — the
 * first post-hydration render rolls the field back to empty (probed:
 * fill reads 18 immediately, 0 within ~200ms; typing holds). The
 * keyboard path (pressSequentially) is the faithful user interaction
 * anyway, so every text entry in THIS spec types.
 */
import { createHmac } from "node:crypto";

import { expect, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* env) to run this suite.");

/** The teacher.spec TOTP generator, mirrored verbatim (importing the
 * spec module would drag its registry along — the P3-A lesson). */
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

function base32Decode(secret: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of secret.toUpperCase().replace(/=+$/, "")) {
    const index = alphabet.indexOf(char);
    if (index < 0) {
      throw new Error(`invalid base32 character: ${char}`);
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

/** Student login typed through the keyboard path (see the webkit
 * note). The post-login SESSION ANCHOR polls /me until it answers
 * authenticated — the negated /login URL alone can flip before the
 * memory-only bearer is recorded, so the next navigation lands
 * anonymous (the webkit snapshot evidence: /tasks showed the login
 * CTA shell). Same resume-signal discipline as ensureStudentLogin. */
async function studentLoginTyped(
  page: import("@playwright/test").Page,
  credentials: string,
): Promise<void> {
  const [username, password] = credentials.split(":");
  await page.goto(`${BASE}/login`);
  await page.getByLabel("学号").pressSequentially(username, { delay: 2 });
  await page.getByLabel("密码").pressSequentially(password, { delay: 2 });
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/);
  // Session anchor: the app shell must render the AUTHED layout (the
  // sidebar's collapse toggle only exists behind the session gate).
  // A bare /me fetch cannot see the memory-only bearer from the page's
  // own module state in webkit, so the DOM landmark is the honest
  // authenticated signal — same shape as the fence specs' chip checks.
  await expect(page.locator(".rail-toggle, .app-sidebar").first()).toBeVisible({
    timeout: 15_000,
  });
}

test.describe("cross-browser smoke (firefox + webkit subset)", () => {
  test("login → task square renders cards → one task detail loads", async ({ page }) => {
    await studentLoginTyped(page, process.env.CQ_E2E_STUDENT ?? "");
    await page.goto(`${BASE}/tasks`);
    const firstCard = page.locator(".task-card").first();
    await expect(firstCard).toBeVisible({ timeout: 20_000 });
    await firstCard.getByRole("link").first().click();
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible({
      timeout: 20_000,
    });
  });

  test("claim flow surface mounts (the client island)", async ({ page }) => {
    await studentLoginTyped(page, process.env.CQ_E2E_STUDENT ?? "");
    const taskUrl = process.env.CQ_E2E_TASK_URL;
    test.skip(
      taskUrl === undefined,
      "needs CQ_E2E_TASK_URL (the seeded open task deep link)",
    );
    await page.goto(`${BASE}${taskUrl}`);
    await expect(
      page.locator(".claim-panel, .task-detail-body section").first(),
    ).toBeVisible({ timeout: 20_000 });
  });

  test("staff surface renders behind its gate", async ({ browser }) => {
    const staff = (process.env.CQ_E2E_STAFF ?? "").split(":");
    const totpSecret = process.env.CQ_E2E_STAFF_TOTP_SECRET ?? "";
    test.skip(
      !(staff[0] && totpSecret),
      "needs CQ_E2E_STAFF + CQ_E2E_STAFF_TOTP_SECRET (the seeded staff contract)",
    );
    const page = await browser.newPage();
    await page.goto(`${BASE}/staff/login`);
    await page.getByLabel("邮箱").pressSequentially(staff[0], { delay: 2 });
    await page.getByLabel("密码").pressSequentially(staff[1] ?? "", { delay: 2 });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await page.getByLabel("动态验证码").pressSequentially(totpCode(totpSecret), { delay: 2 });
      await page.getByRole("button", { name: "登录", exact: true }).click();
      try {
        await expect(page).not.toHaveURL(/\/staff\/login/, { timeout: 5_000 });
        break;
      } catch {
        /* TOTP step rollover — recompute like a user would */
      }
    }
    await expect(page).not.toHaveURL(/\/staff\/login/);
    await page.goto(`${BASE}/teacher/reviews`);
    await expect(page.locator(".review-item, .section").first()).toBeVisible({
      timeout: 20_000,
    });
    await page.close();
  });
});
