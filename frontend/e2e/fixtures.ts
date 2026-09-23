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
 * - CQ_E2E_BASE_URL     frontend origin   (default http://localhost:3000);
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
import { existsSync, readFileSync } from "node:fs";

import {
  expect,
  test as base,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

import { runWorldAction } from "./global-setup";

export const E2E_ENABLED = process.env.CQ_E2E === "1";
export const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
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
  await page.goto(`${BASE_URL}/login`);
  await page.getByLabel("学号").fill(account.username);
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
      await meOk;
      await page.context().storageState({ path: statePath });
      return;
    } catch {
      // Stale or corrupt state: fall through to the form login below.
    }
  }
  await loginThroughUi(page, account);
  await page.context().storageState({ path: statePath });
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
      const statePath = sessionStatePath();
      if (process.env.CQ_E2E_RUN !== undefined && page.context().pages().length > 0) {
        await page.context()
          .storageState({ path: statePath })
          .catch(() => {});
      }
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
export function recoverOtpCode(phoneE164: string): string {
  return runWorldAction<{ code: string }>("otp-code", phoneE164).code;
}
