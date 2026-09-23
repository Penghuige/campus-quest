/**
 * CampusQuest staff auth e2e — Plan 09 Task 8 (invitation acceptance + 2FA);
 * wired to the runner by Plan 10 Task 2.
 *
 * Every test below is skipped unless CQ_E2E=1, so importing the file can
 * never depend on a live backend during ordinary development.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1                 enable the suite (required);
 * - CQ_E2E_BASE_URL          frontend origin   (default http://localhost:3000);
 * - CQ_E2E_API_URL           backend API root  (default http://localhost:8000/api/v1);
 * - CQ_E2E_RUN / CQ_E2E_ADMIN_ID — the seeded world's contract
 *                            (e2e/global-setup.ts): the invitation is
 *                            minted through the REAL admin API with a
 *                            freshly minted admin bearer (the 15-min token
 *                            TTL), and the invited email is run-prefixed so
 *                            teardown can sweep the accepted account.
 *
 * This file covers the brief's STEP 1 (invitation -> password -> TOTP setup
 * -> recovery codes once -> done pointing at staff login). Step 2 (2FA
 * enforcement at login: password-without-TOTP must not yield a session;
 * reused recovery code fails) needs a seeded staff fixture contract from
 * Plan 10 and lands with the runner.
 *
 * The confirm code is computed IN-TEST from the displayed secret
 * (RFC 6238, SHA-1, 30s step, 6 digits — node:crypto, no test dependency),
 * mirroring what a real authenticator app does with the same secret.
 */
import { createHmac } from "node:crypto";

import { expect, request, test, type APIRequestContext } from "@playwright/test";

import { mintToken } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8000/api/v1";
const RUN = process.env.CQ_E2E_RUN;
const ADMIN_ID = process.env.CQ_E2E_ADMIN_ID;

test.skip(
  !E2E_ENABLED,
  "set CQ_E2E=1 (and the CQ_E2E_* URLs) to run this suite.",
);

const worldReady = RUN !== undefined && ADMIN_ID !== undefined;
test.skip(
  !worldReady,
  "invitation flow needs the seeded world (CQ_E2E_RUN + CQ_E2E_ADMIN_ID); Plan 10's global setup provides both.",
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

/** The current RFC 6238 code for `secret` — what the authenticator shows. */
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

test.describe("staff invitation acceptance (brief step 1)", () => {
  let api: APIRequestContext;

  test.beforeAll(async () => {
    // The module-level `request` factory (the E1-recorded fix for the
    // old `test.request` misuse).
    api = await request.newContext({ baseURL: BASE_URL });
  });

  test.afterAll(async () => {
    await api.dispose();
  });

  /** Mint one single-use invitation through the REAL admin API (the
   * route lives at /api/v1/staff/invitations — the identity admin
   * router's full-path convention). */
  async function freshInvitationToken(email: string): Promise<string> {
    const response = await api.post(`${API_URL}/staff/invitations`, {
      headers: { Authorization: `Bearer ${mintToken(ADMIN_ID!)}` },
      data: { email, role: "TEACHER" },
    });
    expect(
      response.ok(),
      `invitation minting failed (${response.status()}): ${await response.text()}`,
    ).toBe(true);
    const body = (await response.json()) as { token: string };
    expect(typeof body.token, "the API returns the one-time {token}").toBe("string");
    return body.token;
  }

  test("invite -> password -> TOTP confirm -> recovery codes once -> done", async ({ page }) => {
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);

    const token = await freshInvitationToken(`e2e-staff-${RUN}@school.edu`);
    await page.goto(`${BASE_URL}/staff/invite/${encodeURIComponent(token)}`);

    // Password step: band mirror + repeat field.
    await page.getByLabel("设置密码").fill("e2e-correct-horse");
    await page.getByLabel("确认密码").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "设置密码并继续" }).click();

    // Setup step: NO QR — the secret + otpauth URI render as copyable text.
    const secretLocator = page.getByTestId("totp-secret");
    await expect(secretLocator).toBeVisible();
    await expect(page.getByTestId("totp-uri")).toContainText("otpauth://totp/");
    const secret = (await secretLocator.textContent())?.trim() ?? "";

    // Confirm with the code a real authenticator would show for `secret`.
    // A slow dev-server hop can carry the request across a 30s step
    // boundary (the server window is current ±1 step), so a typed
    // wrong-code RESPONSE is retried with the CURRENT code — exactly
    // what a real user does when the step rolls over mid-submit. The
    // verdict is the confirm REQUEST's own response (the wrong-code
    // alert node persists between attempts, so waiting on it would
    // match the stale copy and spin the loop).
    const codeField = page.getByRole("textbox", { name: /动态验证码/ });
    const codesList = page.getByRole("list", { name: "恢复代码" });
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const confirmResponse = page.waitForResponse(
        (response) =>
          response.url().includes("/api/v1/staff/totp/confirm") &&
          response.request().method() === "POST",
        { timeout: 15_000 },
      );
      await codeField.fill(totpCode(secret));
      await page.getByRole("button", { name: "确认绑定" }).click();
      const response = await confirmResponse;
      if (response.ok()) {
        break;
      }
      // Let the rejected attempt's alert settle before the next code.
      await page.waitForTimeout(1_000);
    }

    // Recovery codes: shown EXACTLY ONCE with the prominent warning.
    await expect(codesList).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("恢复代码只显示这一次")).toBeVisible();
    const codeCount = await codesList.getByRole("listitem").count();
    expect(codeCount, "the backend issues 8 recovery codes").toBe(8);

    // Copy-all places one code per line on the clipboard.
    await page.getByRole("button", { name: "复制全部恢复代码" }).click();
    await expect(page.getByText("已复制")).toBeVisible();

    // Confirm-seen exits the one-time window; the codes never come back.
    await page.getByRole("button", { name: "我已妥善保存，完成绑定" }).click();
    await expect(page.getByText("动态口令绑定完成，员工账号已激活。")).toBeVisible();
    await expect(codesList).toHaveCount(0);
    await expect(secretLocator).toHaveCount(0);

    // Done points at the staff login (the pending session is setup-only).
    await expect(page.getByRole("link", { name: "前往员工登录" })).toHaveAttribute(
      "href",
      "/staff/login",
    );
  });

  test("client validation: a too-short password never sends the one-time token", async ({ page }) => {
    await page.goto(`${BASE_URL}/staff/invite/whatever-token`);
    await page.getByLabel("设置密码").fill("short");
    await page.getByLabel("确认密码").fill("short");
    await page.getByRole("button", { name: "设置密码并继续" }).click();
    await expect(page.getByText(/密码长度需为 10-128 个字符/)).toBeVisible();
    // Still on the password step — no request left the browser.
    await expect(page.getByLabel("设置密码")).toBeVisible();
  });

  test("a dead invitation token gets the uniform rejection copy", async ({ page }) => {
    await page.goto(`${BASE_URL}/staff/invite/definitely-not-a-real-token`);
    await page.getByLabel("设置密码").fill("e2e-correct-horse");
    await page.getByLabel("确认密码").fill("e2e-correct-horse");
    await page.getByRole("button", { name: "设置密码并继续" }).click();
    // Uniform for unknown/expired/used — the branch reason never leaks.
    // (.first(): Next's route announcer also carries role=alert.)
    await expect(page.getByRole("alert").first()).toContainText("邀请链接无效或已被使用");
  });
});
