/**
 * The staff root-redirect contract (owner feedback 2026-10-15; audit
 * item A, reviewer-approved plan): a STAFF session opening a student
 * route lands on ITS OWN workspace — the guidance card is retired for
 * this family. Pinned end-to-end:
 *
 * 1. the three-frame refresh timeline (teacher AND admin): the frozen
 *    pre-resolve frame is the minimal redirect placeholder (no student
 *    rail, no card), and releasing /me AUTO-replaces to the role's
 *    landing path — no manual click anywhere;
 * 2. students keep the optimistic workspace (no redirect, URL stays);
 * 3. anonymous keeps the auth card;
 * 4. a /me FAILURE under a staff hint renders the error frame inside
 *    the placeholder shell — retry AND a way out to the staff login,
 *    never an infinite 正在前往.
 */
import { expect, test, type Page } from "@playwright/test";

import { ensureStudentLogin, staffLogin } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const TEACHER = process.env.CQ_E2E_TEACHER;
const TEACHER_TOTP_SECRET = process.env.CQ_E2E_TEACHER_TOTP_SECRET;
const ADMIN = process.env.CQ_E2E_ADMIN;
const ADMIN_TOTP_SECRET = process.env.CQ_E2E_ADMIN_TOTP_SECRET;

test.skip(
  !(E2E_ENABLED && TEACHER && TEACHER_TOTP_SECRET && ADMIN && ADMIN_TOTP_SECRET),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

interface MeGate {
  waitHeld: () => Promise<void>;
  release: () => void;
}

async function holdFirstMe(page: Page): Promise<MeGate> {
  let held = 0;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/api/v1/me",
    (route) => {
      held += 1;
      return gate.then(() => route.continue());
    },
    { times: 1 },
  );
  return {
    waitHeld: () =>
      expect.poll(() => held, { message: "the /me request is intercepted" }).toBe(1),
    release,
  };
}

test.describe("staff on student routes: redirect, not a card (audit item A)", () => {
  for (const [role, creds, secret, landing] of [
    ["teacher", TEACHER!, TEACHER_TOTP_SECRET!, "/teacher/reviews"],
    ["admin", ADMIN!, ADMIN_TOTP_SECRET!, "/admin"],
  ] as const) {
    test(`${role}: root refresh = placeholder frame, then AUTO-redirect to ${landing}`, async ({
      page,
    }) => {
      await staffLogin(page, creds, secret);

      // Arm the gate BEFORE the first root visit: the redirect fires on
      // the very first resolve, so a pre-visit would already be gone.
      const gate = await holdFirstMe(page);
      await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
      await gate.waitHeld();

      // Frame 1 — the frozen pre-resolve frame: minimal chrome with the
      // redirect placeholder copy; NO student rail, NO rejection card.
      const shell = page.locator(".app-shell");
      await expect(shell).toHaveAttribute("data-shell", "loading");
      await expect(page.locator(".app-sidebar")).toHaveCount(0);
      await expect(page.getByText("正在前往工作台")).toBeVisible();

      // Frame 2 — release /me: the redirect fires BY ITSELF (no click).
      gate.release();
      await expect(page).toHaveURL(new RegExp(`${BASE_URL}${landing}`));
      await expect(page.getByText("学生工作区仅对学生账号开放")).toHaveCount(0);
    });

    test(`${role}: a deep student link (/rewards) also redirects to ${landing}`, async ({
      page,
    }) => {
      await staffLogin(page, creds, secret);
      await page.goto(`${BASE_URL}/rewards`, { waitUntil: "domcontentloaded" });
      await expect(page).toHaveURL(new RegExp(`${BASE_URL}${landing}`), { timeout: 8_000 });
    });
  }

  test("students keep the workspace — no redirect, URL stays", async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await expect(page.locator(".app-sidebar")).toBeVisible();
    await page.waitForTimeout(600);
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
    await expect(page.getByText("正在前往工作台")).toHaveCount(0);
  });

  test("anonymous keeps the auth card", async ({ page }) => {
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await expect(page.locator(".auth-card")).toBeVisible();
    await expect(page.getByText("正在前往工作台")).toHaveCount(0);
  });

  test("a /me failure under a staff hint renders the error frame with a way out", async ({
    page,
  }) => {
    await staffLogin(page, TEACHER!, TEACHER_TOTP_SECRET!);
    // The next /me fails hard (500) — the placeholder shell must surface
    // the error with BOTH a retry and an exit to the staff login.
    await page.route("**/api/v1/me", async (route) => {
      await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
    });
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("button", { name: "重新加载" })).toBeVisible({
      timeout: 8_000,
    });
    await expect(page.getByRole("link", { name: "前往员工登录" })).toBeVisible();
    await expect(page.getByText("正在前往工作台")).toHaveCount(0);
  });

  test.describe("staff workspace loading frames are shape-consistent (owner item 3)", () => {
    for (const [role, creds, secret, path] of [
      ["admin", ADMIN!, ADMIN_TOTP_SECRET!, "/admin"],
      ["teacher", TEACHER!, TEACHER_TOTP_SECRET!, "/teacher/reviews"],
    ] as const) {
      test(`${role}: refreshing ${path} paints the workspace skeleton, never the white bar`, async ({
        page,
      }) => {
        await staffLogin(page, creds, secret);

        const gate = await holdFirstMe(page);
        await page.goto(`${BASE_URL}${path}`, { waitUntil: "domcontentloaded" });
        await gate.waitHeld();

        // Frame 1 — the frozen loading frame carries the SAME shape as
        // the resolved workspace: the sidebar rail (with its nav — in
        // the WIDE band the topbar is an actions strip by design, the
        // same as the authenticated shell) and a placeholder account
        // cluster.
        await expect(page.locator(".app-sidebar")).toBeVisible();
        await expect(
          page.locator(".app-sidebar nav").first(),
        ).toBeVisible();
        await expect(page.locator(".app-topbar-actions .avatar-chip.skeleton")).toHaveCount(1);
        await expect(page.locator("main.app-main[aria-busy='true']")).toBeVisible();

        // Frame 2 — release /me: the real workspace mounts in place
        // (the sidebar never leaves the DOM — no cross-shape repaint).
        gate.release();
        await expect(page.locator(".app-sidebar")).toBeVisible();
        await expect(
          page.locator(".app-topbar-actions .avatar-chip.skeleton"),
        ).toHaveCount(0);
      });
    }
  });
});
