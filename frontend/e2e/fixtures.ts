/**
 * CampusQuest Playwright fixtures skeleton — Plan 10 Task 1 (E1).
 *
 * The runner-side companion to the e2e specs: the shared CQ_E2E guard
 * values (the convention every spec header already documents), the
 * backend health wait, and the login helper. Specs keep their own
 * `test.skip(!E2E_ENABLED, ...)` guard line — that is the established
 * convention — and consume the rest from here so tasks 2/9 extend one
 * place.
 *
 * Environment contract (matches the spec headers; defaults target the
 * local dev servers orchestrated by playwright.config.ts):
 * - CQ_E2E=1            enable the suite (required);
 * - CQ_E2E_BASE_URL     frontend origin   (default https://localhost:3000);
 * - CQ_E2E_API_URL      backend API root  (default http://localhost:8000/api/v1);
 * - CQ_E2E_STUDENT / CQ_E2E_TEACHER / CQ_E2E_ADMIN — seeded account
 *   credentials in "username:password" form, the backend e2e
 *   factories' contract (backend/tests/e2e/factories.py; every seeded
 *   account shares its DEFAULT_PASSWORD there).
 *
 * Login semantics (the S4 memory-only ruling, evaluated): the helper
 * drives the REAL login page (学号 + 密码 -> 登录), so the access token
 * lands in the page's memory-only manager through the app's own code
 * path (recordLogin) and the HttpOnly refresh cookie is set by the real
 * Set-Cookie. A token minted OUTSIDE the page (an APIRequestContext
 * login) cannot be injected into that memory — the manager is
 * module-private by design — so out-of-page logins stay useful for
 * seeding calls only, never for authenticated page navigation.
 */
import { createHmac } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";

import {
  expect,
  test as base,
  type APIRequestContext,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import { runWorldAction } from "./global-setup";

export const E2E_ENABLED = process.env.CQ_E2E === "1";
export const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
export const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8000/api/v1";

/** /health/ready (not under /api/v1): readiness proves PG/Redis answer. */
export const BACKEND_HEALTH_URL = `${new URL(API_URL).origin}/health/ready`;

/** One seeded backend account, parsed from its "username:password" env. */
export interface SeededAccount {
  username: string;
  password: string;
}

/** Parse the "username:password" env contract; throws a named, fixable error. */
export function parseSeededAccount(
  raw: string | undefined,
  envName: string,
): SeededAccount {
  const contract = `${envName} must be "username:password" (the backend e2e factory contract)`;
  if (raw === undefined || raw.length === 0) {
    throw new Error(`missing seeded account: ${contract}`);
  }
  const separator = raw.indexOf(":");
  if (separator <= 0 || separator === raw.length - 1) {
    throw new Error(`malformed seeded account: ${contract}`);
  }
  return {
    username: raw.slice(0, separator),
    password: raw.slice(separator + 1),
  };
}

/** Poll /health/ready until the backend answers 2xx, else throw. */
export async function waitForBackend(
  request: APIRequestContext,
  timeoutMs = 30_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const response = await request.get(BACKEND_HEALTH_URL).catch(() => null);
    if (response !== null && response.ok()) {
      return;
    }
    if (Date.now() >= deadline) {
      throw new Error(
        `backend not healthy at ${BACKEND_HEALTH_URL} within ${timeoutMs}ms`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

/**
 * Log one seeded account in through the REAL login page.
 *
 * The submit rides the app's own /auth/login call, so on success the
 * memory-only access-token manager holds the token (the page's API
 * requests authenticate) and the refresh cookie is in place; waiting
 * for the URL to leave /login is the observable success signal (a
 * failed login stays on the form with an error alert).
 */
export async function loginThroughUi(
  page: Page,
  account: SeededAccount,
): Promise<void> {
  await openLoginForm(page, `${BASE_URL}/login`);
  // The unified page (spec 2026-10-19 §4): the password form is the
  // weak link's expansion — the identifier field is THREE-state there
  // (手机号、邮箱或学号), and the seeded student rides its 学号.
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(account.username);
  await page.getByLabel("密码").fill(account.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

/**
 * Resume-or-login for the suite's SHARED seeded student.
 *
 * The backend rate-limits `auth:login` at 10 attempts / 5 min per
 * username — a real bound the whole-suite run would trip after a
 * handful of form logins. So the FIRST test logs in through the real
 * form (the login UX keeps its coverage) and persists the browser
 * context's cookies; every later test seeds its fresh context with
 * those cookies and lets the app's OWN session-resume path run (the
 * /auth/refresh rotation the memory-token manager drives on load),
 * then re-persists the rotated cookie for the next test. A stale or
 * missing state simply falls back to the form.
 *
 * Resume is only a resume when the identity is THE seeded student: a
 * parked staff session (the a11y admin shots, the community reveal)
 * also answers /me with 200 — /me accepts any live session — and the
 * student surfaces would then render the staff-guidance page instead
 * of the page under test (the battery-level flake this guard closes:
 * every ./fixtures spec runs after accessibility's admin-users shot,
 * whose end-of-test cookie persist used to park the admin session
 * here). The username equality is deliberately stricter than a role
 * check: a state file holding ANOTHER student must not resume either.
 */
export async function ensureStudentLogin(page: Page): Promise<void> {
  const account = parseSeededAccount(
    process.env.CQ_E2E_STUDENT,
    "CQ_E2E_STUDENT",
  );
  const statePath = sessionStatePath();
  if (existsSync(statePath)) {
    try {
      const { cookies } = JSON.parse(readFileSync(statePath, "utf-8")) as {
        cookies: StorageCookie[];
      };
      await page.context().addCookies(cookies);
      // The ONLY trustworthy resume signal is a successful /me: the
      // app's bootstrap fires it on every load, an anonymous load never
      // gets a 200 (and its redirect to /login races the navigation,
      // so the URL alone proves nothing). A 200 also guarantees the
      // /auth/refresh ROTATION already delivered the next cookie — the
      // moment the context is safe to re-persist.
      const meOk = page.waitForResponse(
        (response) =>
          response.url().includes("/api/v1/me") && response.status() === 200,
        { timeout: 15_000 },
      );
      await page.goto(`${BASE_URL}/`);
      const me = (await (await meOk).json().catch(() => null)) as {
        username?: unknown;
      } | null;
      if (me?.username !== account.username) {
        console.warn(
          `[ensureStudentLogin] state file holds ${String(me?.username)} — not ${account.username}; falling back to form login`,
        );
        throw new Error("state file is not the seeded student's session");
      }
      await page.context().storageState({ path: statePath });
      studentSessionContext = page.context();
      return;
    } catch {
      // Stale, corrupt, or wrong-identity state: fall through to the
      // form login below (whose Set-Cookie replaces whatever the
      // loaded cookies left in the jar).
    }
  }
  await loginThroughUi(page, account);
  await page.context().storageState({ path: statePath });
  studentSessionContext = page.context();
}

/**
 * The context the shared student's resumable session last landed in
 * (set by ensureStudentLogin, cleared by staffLogin). Playwright gives
 * every test a FRESH default context, so the marker only ever matches
 * the context of the test that itself logged the student in — exactly
 * the end state `persistSession` may hand to the next test. Staff
 * logins and never-logged-in default contexts never match, so their
 * cookies can no longer overwrite the student's state file.
 */
let studentSessionContext: BrowserContext | null = null;

/** Persist only the context explicitly verified as the shared student. */
export async function persistSharedStudentSession(page: Page): Promise<void> {
  if (
    process.env.CQ_E2E_RUN !== undefined &&
    studentSessionContext === page.context() &&
    page.context().pages().length > 0
  ) {
    await page.context().storageState({ path: sessionStatePath() });
  }
}

/**
 * Open a login page WITHOUT losing the shared student's session.
 *
 * Opening /login boots the app's session manager, which fires the
 * single-use /auth/refresh rotation against whatever refresh cookie the
 * context holds. In the shared student's context that rotation would
 * silently invalidate the cookie the state file still carries, so the
 * NEXT test's resume would fail and burn a form login (the rate-limited
 * resource this whole chain exists to protect). Watch the bootstrap to
 * completion, re-persist the rotated cookie when it still answers as
 * THE shared student, then clear the marker — the identity is about to
 * be replaced, and a later persist must never write the new account's
 * cookies into the student's state file (the switch test pins this).
 *
 * A context that is not the shared student's (staff logins, fresh
 * contexts, tests that never logged the student in) skips the wait
 * entirely and behaves exactly like a plain goto.
 */
async function openLoginForm(page: Page, url: string): Promise<void> {
  const shared = studentSessionContext === page.context();
  const hasRefresh =
    shared &&
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "refresh_token",
    );
  const bootstrap = hasRefresh
    ? page.waitForResponse(
        (response) =>
          (new URL(response.url()).pathname.endsWith("/api/v1/me") &&
            response.status() === 200) ||
          (new URL(response.url()).pathname.endsWith("/auth/refresh") &&
            [401, 403].includes(response.status())),
      )
    : null;
  await page.goto(url);
  if (bootstrap !== null) {
    const response = await bootstrap;
    if (response.status() === 200) {
      const me = (await response.json()) as { username?: unknown };
      const account = parseSeededAccount(
        process.env.CQ_E2E_STUDENT,
        "CQ_E2E_STUDENT",
      );
      if (me.username === account.username) await persistSharedStudentSession(page);
    }
  }
  if (shared) studentSessionContext = null;
}

/** The cookie slice of Playwright's storageState (addCookies' input). */
type StorageCookie = {
  name: string;
  value: string;
  domain: string;
  path: string;
  expires: number;
  httpOnly: boolean;
  secure: boolean;
  sameSite: "Strict" | "Lax" | "None";
};

/** Where this run's resumable student session lives. */
function sessionStatePath(): string {
  return `/tmp/cq-e2e-state-${process.env.CQ_E2E_RUN ?? "adhoc"}.json`;
}

// --- staff login (shared by visual-capture and visual-regression) -------------
//
// Moved here from visual-capture.spec.ts by plan-12 task 9 so the pixel
// suite logs staff in through the SAME code path the evidence harness
// uses — one TOTP implementation, one form-driving discipline.

export const STAFF_LOGIN_URL =
  // /staff/login now redirects to the unified page; go there directly.
  process.env.CQ_E2E_STAFF_LOGIN_URL ?? `${BASE_URL}/login`;

export async function staffLogin(
  page: Page,
  credentials: string,
  totpSecret: string,
): Promise<void> {
  const [email, password] = credentials.split(":");
  // openLoginForm clears the shared-student marker itself — after
  // saving the login-page boot's last rotation, so a staff login on a
  // student-seeded context cannot strand a stale cookie in the file.
  await openLoginForm(page, STAFF_LOGIN_URL);
  // The unified page's password expansion (spec §4 layer 1a): the
  // three-state identifier carries the staff email. An ADMIN's
  // password answers totp_required — the second leg completes on the
  // page's TOTP layer; a TEACHER password lands directly.
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(email);
  await page.getByLabel("密码").fill(password);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.getByRole("button", { name: "登录", exact: true }).click();
    const totpLeg = page.getByTestId("unified-login-totp-leg");
    const legAppeared = await totpLeg
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
  await expect(page).not.toHaveURL(/\/login/);
}

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

/** The current RFC 6238 code for `secret` (SHA-1, 30s step, 6 digits).
 * Exported since V1.1: the email-code login spec's ADMIN leg computes
 * the same code this module's staffLogin does — ONE implementation. */
export function totpCode(secret: string, atMs: number = Date.now()): string {
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

/** Staff login through the REAL form (the teacher.spec.ts retry loop).
 * A staff session on a context disqualifies that context from the
 * student resume chain — the marker clear keeps a later persist from
 * handing the NEXT test a staff session as "the student". */
/** The three-role account contract consumed by happy-path specs (each
 * leg optional: the e2e world exports only what its flows need). */
export interface SeededAccounts {
  student?: SeededAccount;
  teacher?: SeededAccount;
  admin?: SeededAccount;
}

type CqFixtures = {
  /** Auto: the backend answers /health/ready before the test body runs. */
  backendReady: void;
  /** The seeded three-role accounts (CQ_E2E_STUDENT/TEACHER/ADMIN). */
  accounts: SeededAccounts;
  /** Auto: persist this test's browser cookies after the body (the
   * session-resume chain — see ensureStudentLogin). */
  persistSession: void;
};

/**
 * The extended `test`: `import { test } from "./fixtures"` gives every
 * spec the backend-readiness wait (auto), the seeded-account contract,
 * and the post-test cookie persistence the shared student session's
 * resume chain needs (a full page load ROTATES the refresh cookie —
 * single-use by design — so only the context's END state is safe to
 * hand the next test), while `expect` re-exports unchanged. The CQ_E2E
 * skip guard stays in the specs (the existing convention), so fixtures
 * here never decide suite enablement themselves.
 *
 * (The second fixture parameter — the runtime callback Playwright
 * passes as `use` — is named `run` here: the react-hooks lint rules
 * shipped with the Next config flag any `use*` call in a non-hook
 * function, and Playwright cares about the parameter's position, not
 * its name.)
 */
export const test = base.extend<CqFixtures>({
  backendReady: [
    async ({ request }, run) => {
      await waitForBackend(request);
      await run();
    },
    { auto: true },
  ],
  persistSession: [
    async ({ page }, run) => {
      await run();
      await persistSharedStudentSession(page).catch(() => {});
    },
    { auto: true },
  ],
  accounts: async ({}, run) => {
    const parse = (
      raw: string | undefined,
      envName: string,
    ): SeededAccount | undefined =>
      raw === undefined ? undefined : parseSeededAccount(raw, envName);
    await run({
      student: parse(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"),
      teacher: parse(process.env.CQ_E2E_TEACHER, "CQ_E2E_TEACHER"),
      admin: parse(process.env.CQ_E2E_ADMIN, "CQ_E2E_ADMIN"),
    });
  },
});

export { expect };

// --- browser-world actions (backend/tests/e2e/browser_world.py) ----------------------
//
// The seeded world ids arrive as CQ_E2E_* env; these helpers shell the
// REAL backend job entries and the OTP probe on demand — the browser
// drives every product surface itself, the Python side only runs the
// worker code no browser can reach (the orchestrated stack runs no
// Celery worker by design, E1's two-server ruling).

/** Run the real validation worker entry for one submission. */
export function runValidationJob(submissionId: string): {
  validation_status: string;
  passed: boolean;
} {
  return runWorldAction("validate", submissionId);
}

/** Run the real ranking-projection worker entry for one user. */
export function runRankingJob(userId: string, effectiveAt?: string): void {
  runWorldAction("rank", userId, ...(effectiveAt ? [effectiveAt] : []));
}

/** A fresh bearer token for a seeded account (15-min TTL: mint per use). */
export function mintToken(userId: string): string {
  return runWorldAction<{ token: string }>("mint", userId).token;
}

/** Recover the current SMS OTP code for a phone (the real Redis hash). */
export function recoverOtpCode(phoneE164: string, purpose?: string): string {
  return runWorldAction<{ code: string }>(
    "otp-code",
    phoneE164,
    ...(purpose !== undefined ? [purpose] : []),
  ).code;
}

/** Recover the current staff login email code for an address (V1.1
 * Wave 1; world action `email-code` — the otp-code pattern). The FORM
 * value passes as typed: the action strips/lowercases internally. */
export function recoverEmailCode(email: string): string {
  return runWorldAction<{ code: string }>("email-code", email).code;
}

/** Recover the student login mobile code (auth extension II §3; world
 * action `mobile-code`, the email-code twin — login_mobile_challenges
 * newest row + closed-space brute force). The address passes E.164. */
export function recoverMobileCode(phoneE164: string): string {
  return runWorldAction<{ code: string }>("mobile-code", phoneE164).code;
}
