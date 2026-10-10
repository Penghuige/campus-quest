/**
 * CampusQuest shell-refresh e2e — backlog UX item (owner QA
 * 2026-10-03, promoted 2026-10-10): the refresh shell jump.
 *
 * Defect: on reload the StudentShell loading branch rendered the
 * minimal topbar shell for the whole session-resolve window
 * (/me + /auth/refresh ≈ 0.6s), then jumped to the workspace sidebar
 * shell — a flash of "old UI" on every authenticated refresh.
 *
 * Contract under test (design §8: ONE navigation geometry, three
 * bands): while the session resolves, a visitor WITH session
 * evidence (the document request carried the readable csrf cookie —
 * the only session artifact a page request ever sees) gets the
 * workspace geometry ALREADY IN THE SERVER HTML: same rail, same
 * topbar strip, same bottom nav, same content column as the
 * authenticated shell, with the user-data slots as placeholders. A
 * visitor WITHOUT session evidence keeps the minimal topbar shell,
 * so the 401→anonymous exit never introduces a sidebar→card jump
 * direction.
 *
 * Mechanics: the /me response is held at the route layer (a gated
 * route, times:1 — the held counter proves interception before any
 * assertion runs) so the loading frame is deterministic: no race
 * against a ~0.6s window. The SSR half is pinned separately through
 * page.request (shares the context's cookies, bypasses page routes):
 * the optimistic rail must be IN the server-rendered HTML, not a
 * client-side second paint.
 *
 * Environment contract (same guard as every spec):
 * - CQ_E2E=1        enable the suite (required);
 * - CQ_E2E_STUDENT  seeded credentials for ensureStudentLogin.
 */
import type { Page } from "@playwright/test";

import { BASE_URL, ensureStudentLogin, expect, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* env) to run this suite.");

const SHELL = ".app-shell";
const ME_ROUTE = "**/api/v1/me";

/* Wide (≥64rem) sidebar band / medium top-nav band / narrow bottom-nav
 * band (design §8). 1280/1000/390 sit safely inside each. */
const BANDS = [
  { band: "wide", viewport: { width: 1280, height: 800 } },
  { band: "medium", viewport: { width: 1000, height: 800 } },
  { band: "narrow", viewport: { width: 390, height: 844 } },
] as const;

interface ShellGeometry {
  shellState: string | null;
  mainX: number | null;
  mainWidth: number | null;
  railWidth: number | null;
  railAttached: boolean;
  topNavDisplay: string | null;
  bottomNavVisible: boolean;
}

/** The observable geometry of the shell — everything that jumps when
 * the loading chrome disagrees with the authenticated chrome. */
async function shellGeometry(page: Page): Promise<ShellGeometry> {
  return page.evaluate(() => {
    const shell = document.querySelector(".app-shell");
    const main = document.querySelector(".app-main");
    const rail = document.querySelector(".app-sidebar");
    const nav = document.querySelector(".app-topbar .app-nav");
    const bottom = document.querySelector(".app-bottomnav");
    const mainRect = main?.getBoundingClientRect();
    const railRect = rail?.getBoundingClientRect();
    return {
      shellState: shell?.getAttribute("data-shell") ?? null,
      mainX: mainRect ? Math.round(mainRect.x) : null,
      mainWidth: mainRect ? Math.round(mainRect.width) : null,
      railWidth: railRect ? Math.round(railRect.width) : null,
      railAttached: rail !== null,
      topNavDisplay: nav ? getComputedStyle(nav).display : null,
      bottomNavVisible:
        bottom !== null && getComputedStyle(bottom).display !== "none",
    };
  });
}

interface MeGate {
  /** Resolves once the first /me request has actually been intercepted
   * — call after the navigation that fires it. Proves the session is
   * parked in the loading state before any frame assertion runs. */
  waitHeld: () => Promise<void>;
  /** Let the held request through. */
  release: () => void;
}

/** Hold the first /me response until released (times:1 — the 401
 * retry that follows a reload goes through untouched). */
async function holdFirstMe(page: Page): Promise<MeGate> {
  let held = 0;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    ME_ROUTE,
    (route) => {
      held += 1;
      return gate.then(() => route.continue());
    },
    { times: 1 },
  );
  return {
    waitHeld: () => expect.poll(() => held, { message: "the /me request is intercepted" }).toBe(1),
    release,
  };
}

test.describe("refresh shell geometry (backlog: 刷新外壳跳变)", () => {
  test("a session-bearing refresh renders the workspace geometry in the loading frame, SSR included", async ({ page }) => {
    await ensureStudentLogin(page);

    // The optimistic rail must be IN the server HTML — the fix is a
    // first-paint fix, not a client-side second paint. page.request
    // shares the context cookies (and bypasses page routes).
    const ssr = await (await page.request.get(`${BASE_URL}/tasks`)).text();
    expect(ssr).toContain('data-shell="loading"');
    expect(ssr).toContain("app-sidebar-rail");

    const gate = await holdFirstMe(page);
    await page.reload();
    await gate.waitHeld();

    const shell = page.locator(SHELL);
    await expect(shell).toHaveAttribute("data-shell", "loading");

    // Per-band loading geometry: the workspace chrome in every band.
    const loading: Record<string, ShellGeometry> = {};
    for (const { band, viewport } of BANDS) {
      await page.setViewportSize(viewport);
      loading[band] = await shellGeometry(page);
      expect(loading[band].shellState).toBe("loading");
    }
    // Wide: the rail owns primary navigation; the topbar degrades to
    // the actions strip (brand + nav hidden by the :has rule).
    expect(loading.wide.railAttached).toBe(true);
    expect(loading.wide.railWidth).toBeGreaterThanOrEqual(230);
    expect(loading.wide.topNavDisplay).toBe("none");
    // Medium: the horizontal top nav keeps every destination.
    expect(loading.medium.topNavDisplay).toBe("flex");
    expect(loading.medium.railWidth).toBe(0);
    // Narrow: the 5-slot bottom nav; no rail, no top nav.
    expect(loading.narrow.bottomNavVisible).toBe(true);
    expect(loading.narrow.topNavDisplay).toBe("none");

    // Session resolves in place: same page, no reload.
    gate.release();
    await expect(shell).toHaveAttribute("data-shell", "workspace");

    // The settled shell must sit in EXACTLY the captured geometry —
    // the anti-jump verdict, band by band.
    for (const { band, viewport } of [...BANDS].reverse()) {
      await page.setViewportSize(viewport);
      const settled = await shellGeometry(page);
      expect(settled.shellState).toBe("workspace");
      expect(settled.mainX).toBe(loading[band].mainX);
      expect(settled.mainWidth).toBe(loading[band].mainWidth);
      expect(settled.railWidth).toBe(loading[band].railWidth);
      expect(settled.topNavDisplay).toBe(loading[band].topNavDisplay);
      expect(settled.bottomNavVisible).toBe(loading[band].bottomNavVisible);
    }
  });

  test("a cookie-less visitor never sees the workspace chrome (no new jump direction)", async ({ page }) => {
    // Fresh context: no session cookies at all. The server HTML must
    // keep the minimal shell — no rail for the anonymous resolve to
    // take away again.
    const ssr = await (await page.request.get(`${BASE_URL}/tasks`)).text();
    expect(ssr).toContain('data-shell="loading"');
    expect(ssr).not.toContain("app-sidebar-rail");

    const gate = await holdFirstMe(page);
    await page.goto(`${BASE_URL}/tasks`);
    await gate.waitHeld();

    const shell = page.locator(SHELL);
    await expect(shell).toHaveAttribute("data-shell", "loading");
    // The minimal topbar shell: brand visible, NO rail, NO bottom nav.
    await expect(page.locator(".app-sidebar")).toHaveCount(0);
    await expect(page.locator(".app-bottomnav")).toHaveCount(0);
    await expect(page.locator(".app-topbar .app-brand")).toBeVisible();

    // 401 lands: minimal → auth card, the transition that has always
    // existed — never sidebar → card.
    gate.release();
    await expect(shell).toHaveAttribute("data-shell", "anonymous");
    await expect(page.getByRole("heading", { name: "登录 CampusQuest" })).toBeVisible();
  });

  test("a session-bearing network failure keeps the workspace chrome around the retryable error", async ({ page }) => {
    await ensureStudentLogin(page);

    // Abort every /me: the resolve ends in `error`, not anonymous.
    await page.route(ME_ROUTE, (route) => route.abort());
    await page.reload();

    const shell = page.locator(SHELL);
    await expect(shell).toHaveAttribute("data-shell", "error");
    // The optimistic verdict holds for errors too (shellChromeFor):
    // the rail frames the retryable error card.
    await expect(page.locator(".app-sidebar")).toBeVisible();
    const retry = page.getByRole("button", { name: "重新加载" });
    await expect(retry).toBeVisible();

    // Retry after the network recovers: the shell settles in place.
    await page.unroute(ME_ROUTE);
    await retry.click();
    await expect(shell).toHaveAttribute("data-shell", "workspace");
  });
});
