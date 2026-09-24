/**
 * Visual evidence capture — Plan 11 brief §17.0 (evidence matrix).
 *
 * Captures named-route screenshots under the fixed matrix so the
 * "before" baseline and every later "after" pass are comparable:
 * Chromium; viewport from CQ_E2E_VIEWPORT (desktop 1440x900 |
 * mobile 375x812); the same seeded Plan 10 browser world (fresh seed
 * per pass — deadlines are computed from claim time, so relative
 * urgency is equivalent across passes); one additional
 * reduced-motion pass with CQ_E2E_REDUCED_MOTION=1.
 *
 * Off by default: runs only when BOTH CQ_E2E=1 (the suite-wide
 * convention) and CQ_E2E_CAPTURE_DIR are set, so ordinary development
 * and the release gate (whose no-skip assertion watches
 * teacher/admin only) never depend on it.
 *
 * Output: ${CQ_E2E_CAPTURE_DIR}/${pass}/${name}.png plus a
 * manifest.json recording route / account / viewport / world run for
 * every shot — the artifact the PR evidence comment cites.
 *
 * Usage (from frontend/, ports chosen clear of any dev stack):
 *   CQ_E2E=1 CQ_E2E_CAPTURE_DIR=/tmp/cq-baseline CQ_E2E_VIEWPORT=desktop \
 *   CQ_E2E_BASE_URL=http://localhost:3100 \
 *   CQ_E2E_API_URL=http://localhost:8100/api/v1 \
 *   npx playwright test visual-capture.spec.ts
 */
import { createHmac } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, type Page } from "@playwright/test";

import { test, ensureStudentLogin } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const CAPTURE_DIR = process.env.CQ_E2E_CAPTURE_DIR;
const VIEWPORT =
  process.env.CQ_E2E_VIEWPORT === "mobile" ? "mobile" : "desktop";
const REDUCED_MOTION = process.env.CQ_E2E_REDUCED_MOTION === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const STAFF_LOGIN_URL =
  process.env.CQ_E2E_STAFF_LOGIN_URL ?? `${BASE_URL}/staff/login`;

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  mobile: { width: 375, height: 812 },
} as const;

const PASS = `${VIEWPORT}${REDUCED_MOTION ? "-reduced-motion" : ""}`;
const OUT_DIR = CAPTURE_DIR ? join(CAPTURE_DIR, PASS) : "";

test.use({
  viewport: VIEWPORTS[VIEWPORT],
  reducedMotion: REDUCED_MOTION ? "reduce" : "no-preference",
});

test.skip(
  !(E2E_ENABLED && CAPTURE_DIR),
  "Visual capture is opt-in: set CQ_E2E=1 and CQ_E2E_CAPTURE_DIR.",
);

// --- TOTP (RFC 6238, the teacher.spec.ts implementation) ----------------------

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
    (digest[offset + 3] & 0xff);
  return String(binary % 1_000_000).padStart(6, "0");
}

// --- capture helpers ----------------------------------------------------------

interface ManifestEntry {
  name: string;
  route: string;
  account: string;
  viewport: string;
  reducedMotion: boolean;
  worldRun: string;
  file: string;
}

function manifestPath(): string {
  return join(CAPTURE_DIR!, "manifest.json");
}

function recordManifest(entry: ManifestEntry): void {
  let entries: ManifestEntry[] = [];
  try {
    entries = JSON.parse(readFileSync(manifestPath(), "utf-8")) as ManifestEntry[];
  } catch {
    entries = [];
  }
  // One entry per (pass, name): a re-run of the same pass replaces its row.
  entries = entries.filter(
    (prior) => !(prior.file.startsWith(`${PASS}/`) && prior.name === entry.name),
  );
  entries.push(entry);
  writeFileSync(manifestPath(), JSON.stringify(entries, null, 2));
}

/**
 * Navigate to a route, wait for the page's own header block (falling
 * back to the main landmark) plus a settle delay, then capture the
 * full page. The settle delay absorbs dev-mode hydration and late
 * data paints without asserting on any business content.
 *
 * Wrong-artifact guard (post-incident hardening): on authenticated
 * pages the shot is only valid if THIS pass's navigation landmark
 * actually rendered (sidebar on desktop, bottom nav on mobile). A
 * stale/leaked dev server serving an older build would fail here
 * instead of silently producing "evidence" of the wrong layout.
 */
async function capture(
  page: Page,
  name: string,
  route: string,
  account: string,
  authenticated = true,
): Promise<void> {
  await page.goto(`${BASE_URL}${route}`);
  const anchor = page.locator(".page-head, main").first();
  await expect(anchor).toBeVisible({ timeout: 15_000 });
  if (authenticated) {
    // Desktop: every shell renders the sidebar. Narrow: the student
    // shell renders the bottom nav, the staff shells the menu button.
    const nav =
      VIEWPORT === "desktop"
        ? page.locator(".app-sidebar")
        : page.locator(".app-bottomnav, .app-menubtn");
    await expect(nav.first()).toBeVisible({ timeout: 5_000 });
  }
  await page.waitForTimeout(800);
  const file = `${PASS}/${name}.png`;
  await page.screenshot({ path: join(CAPTURE_DIR!, file), fullPage: true });
  recordManifest({
    name,
    route,
    account,
    viewport: VIEWPORT,
    reducedMotion: REDUCED_MOTION,
    worldRun: process.env.CQ_E2E_RUN ?? "adhoc",
    file,
  });
}

/** Staff login through the REAL form (the teacher.spec.ts retry loop). */
async function staffLogin(
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

// --- passes -------------------------------------------------------------------

test.describe("visual capture — anonymous auth surfaces", () => {
  test("auth screens", async ({ page }) => {
    await capture(page, "auth-login", "/login", "anonymous", false);
    await capture(page, "auth-register", "/register", "anonymous", false);
    await capture(page, "auth-staff-login", "/staff/login", "anonymous", false);
  });
});

test.describe("visual capture — student surfaces", () => {
  test("student core routes", async ({ page }) => {
    test.skip(!process.env.CQ_E2E_STUDENT, "world did not export CQ_E2E_STUDENT");
    const account = process.env.CQ_E2E_STUDENT!.split(":")[0];
    await ensureStudentLogin(page);
    await capture(page, "student-dashboard", "/", account);
    await capture(page, "student-tasks", "/tasks", account);
    if (process.env.CQ_E2E_TASK_OPEN_PATH) {
      await capture(page, "student-task-detail", process.env.CQ_E2E_TASK_OPEN_PATH, account);
    }
    if (process.env.CQ_E2E_CLAIM_PATH) {
      await capture(page, "student-claim", process.env.CQ_E2E_CLAIM_PATH, account);
    }
    await capture(page, "student-rankings", "/rankings", account);
    await capture(page, "student-rewards", "/rewards", account);
    await capture(page, "student-notifications", "/notifications", account);
    await capture(page, "student-profile", "/profile", account);
  });
});

test.describe("visual capture — teacher workstation", () => {
  test("teacher routes", async ({ page }) => {
    test.skip(
      !(process.env.CQ_E2E_STAFF && process.env.CQ_E2E_STAFF_TOTP_SECRET),
      "world did not export the staff contract",
    );
    const account = process.env.CQ_E2E_STAFF!.split(":")[0];
    await staffLogin(page, process.env.CQ_E2E_STAFF!, process.env.CQ_E2E_STAFF_TOTP_SECRET!);
    await capture(page, "teacher-reviews", "/teacher/reviews", account);
    await capture(page, "teacher-tasks", "/teacher/tasks", account);
  });
});

test.describe("visual capture — admin workstation", () => {
  test("admin routes", async ({ page }) => {
    test.skip(
      !(process.env.CQ_E2E_ADMIN && process.env.CQ_E2E_ADMIN_TOTP_SECRET),
      "world did not export the admin contract",
    );
    const account = process.env.CQ_E2E_ADMIN!.split(":")[0];
    await staffLogin(page, process.env.CQ_E2E_ADMIN!, process.env.CQ_E2E_ADMIN_TOTP_SECRET!);
    await capture(page, "admin-users", "/admin/users", account);
    await capture(page, "admin-rewards", "/admin/rewards", account);
    await capture(page, "admin-redemptions", "/admin/redemptions", account);
    await capture(page, "admin-audit", "/admin/audit", account);
    await capture(page, "admin-system", "/admin/system", account);
    await capture(page, "admin-whitelist", "/admin/whitelist", account);
  });
});

// The output directory must exist before the first shot (Playwright
// creates parents for screenshot paths, but the manifest writer does
// not — create both up front so a failed first test cannot lose the
// manifest convention).
test.beforeAll(() => {
  mkdirSync(OUT_DIR, { recursive: true });
});
