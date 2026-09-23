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
import {
  expect,
  test as base,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

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

/** The three-role account contract consumed by happy-path specs. */
export interface SeededAccounts {
  student: SeededAccount;
  teacher: SeededAccount;
  admin: SeededAccount;
}

type CqFixtures = {
  /** Auto: the backend answers /health/ready before the test body runs. */
  backendReady: void;
  /** The seeded three-role accounts (CQ_E2E_STUDENT/TEACHER/ADMIN). */
  accounts: SeededAccounts;
};

/**
 * The extended `test`: `import { test } from "./fixtures"` gives every
 * spec the backend-readiness wait (auto) and the seeded-account
 * contract, while `expect` re-exports unchanged. The CQ_E2E skip guard
 * stays in the specs (the existing convention), so fixtures here never
 * decide suite enablement themselves.
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
  accounts: async ({}, run) => {
    await run({
      student: parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT"),
      teacher: parseSeededAccount(process.env.CQ_E2E_TEACHER, "CQ_E2E_TEACHER"),
      admin: parseSeededAccount(process.env.CQ_E2E_ADMIN, "CQ_E2E_ADMIN"),
    });
  },
});

export { expect };
