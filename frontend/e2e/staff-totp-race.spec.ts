/**
 * CampusQuest staff TOTP begin single-flight regression — Plan 10 E6
 * (the E5 flake watch, reproduced deterministically).
 *
 * Every test below is skipped unless CQ_E2E=1, so importing the file can
 * never depend on a live backend during ordinary development.
 *
 * Environment contract: identical to staff-auth.spec.ts (CQ_E2E_*).
 *
 * The race (E5 run 5, 16.8s failure after five rejected codes):
 * TotpSetup's mount effect fires `/staff/totp/begin`, and the backend
 * ROTATES the unconfirmed secret on every begin (staff_service.
 * begin_totp_setup — the last COMMITTED begin owns what confirm will
 * verify). React StrictMode's dev double-effect fires TWO begins; the
 * component's cancelled-flag keeps only the LAST-DISPATCHED begin's
 * response as state. When the server commits the two begins in the
 * OPPOSITE order to their dispatch (the first request stuck on a slow
 * hop — exactly a cold `next dev`), the displayed secret is the second
 * begin's while the stored one is the first's: every confirm code
 * computed from the page is then rejected, with no recovery via retry.
 *
 * This spec pins the owning-domain invariant: ONE begin per mount
 * generation. The route below holds the FIRST begin's forwarding until
 * the duplicate has committed (replaying the reordered interleave
 * legally); with the single-flight guard the duplicate never exists,
 * the displayed secret is the stored one, and confirm succeeds.
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

test.describe("TOTP begin single-flight (E5 flake watch)", () => {
  let api: APIRequestContext;

  test.beforeAll(async () => {
    api = await request.newContext({ baseURL: BASE_URL });
  });

  test.afterAll(async () => {
    await api.dispose();
  });

  /** Mint one single-use invitation through the REAL admin API. */
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

  test("a duplicated begin must not desync the displayed secret from the stored one", async ({ page }) => {
    const token = await freshInvitationToken(`e2e-totp-race-${RUN}@school.edu`);
    await page.goto(`${BASE_URL}/staff/invite/${encodeURIComponent(token)}`);

    // Password step first; the route must be armed before the setup
    // island mounts and fires its begin(s).
    await page.getByLabel("设置密码").fill("e2e-correct-horse");
    await page.getByLabel("确认密码").fill("e2e-correct-horse");

    // The reordered interleave, legally replayed: the FIRST begin's
    // request is held back from the server until the duplicate has
    // committed (a slow first hop — the natural cold-start shape). With
    // a second begin in flight that makes the server's commit order the
    // reverse of the dispatch order; with the single-flight guard the
    // hold simply times out and the one begin proceeds normally.
    let beginCount = 0;
    let markDuplicateCommitted: () => void = () => {};
    const duplicateCommitted = new Promise<void>((resolve) => {
      markDuplicateCommitted = resolve;
    });
    const holdFirstHop = new Promise<void>((resolve) => {
      duplicateCommitted.then(() => resolve());
      setTimeout(resolve, 1_500); // no duplicate ever comes: proceed anyway
    });
    await page.route("**/api/v1/staff/totp/begin", async (route) => {
      const index = beginCount;
      beginCount += 1;
      if (index === 0) {
        await holdFirstHop;
      }
      const response = await route.fetch();
      if (index === 1) {
        markDuplicateCommitted();
      }
      await route.fulfill({ response });
    });

    await page.getByRole("button", { name: "设置密码并继续" }).click();

    // Setup step renders the secret the page will confirm against.
    const secretLocator = page.getByTestId("totp-secret");
    await expect(secretLocator).toBeVisible({ timeout: 15_000 });
    const secret = ((await secretLocator.textContent()) ?? "").trim();
    expect(secret.length, "a base32 secret is displayed").toBeGreaterThan(0);

    // Confirm exactly like a user (and the sibling spec): codes from the
    // DISPLAYED secret, a fresh code per attempt across step rollovers.
    const codeField = page.getByRole("textbox", { name: /动态验证码/ });
    const codesList = page.getByRole("list", { name: "恢复代码" });
    let confirmed = false;
    for (let attempt = 0; attempt < 3 && !confirmed; attempt += 1) {
      const confirmResponse = page.waitForResponse(
        (response) =>
          response.url().includes("/api/v1/staff/totp/confirm") &&
          response.request().method() === "POST",
        { timeout: 15_000 },
      );
      await codeField.fill(totpCode(secret));
      await page.getByRole("button", { name: "确认绑定" }).click();
      const response = await confirmResponse;
      confirmed = response.ok();
      if (!confirmed) {
        await page.waitForTimeout(1_000);
      }
    }

    // The one-flight verdict: with a desynced secret every code above is
    // rejected (the stored secret is the other begin's), so the recovery
    // codes never appear — the exact E5 failure signature.
    expect(confirmed, "a code computed from the displayed secret must confirm").toBe(
      true,
    );
    await expect(codesList).toBeVisible({ timeout: 10_000 });
    expect(
      beginCount,
      "exactly one begin per mount generation (the StrictMode duplicate is suppressed)",
    ).toBe(1);
  });
});
