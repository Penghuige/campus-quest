/**
 * CampusQuest sidebar collapse/resize e2e — defect #3 (QA 2026-09-30).
 *
 * Covers the two affordances the QA report asks for on the shared rail
 * (WorkspaceSidebar serves Student/Teacher/Admin alike; the student
 * shell exercises the fullest variant with the account footer):
 * - the collapse toggle swaps the rail to an icon rail while every nav
 *   item KEEPS its accessible name (labels clip off-canvas, they do
 *   not leave the a11y tree) and toggles back;
 * - the right-edge resizer works by pointer drag AND by keyboard
 *   (role="separator" + Arrow/Home/End — the same pixel range);
 * - the preference survives reload through localStorage.
 *
 * Environment contract (same guard as every spec):
 * - CQ_E2E=1        enable the suite (required);
 * - CQ_E2E_STUDENT  seeded credentials for ensureStudentLogin.
 */
import { ensureStudentLogin, expect, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* env) to run this suite.");

/* The rail is a wide-band (>=64rem) affordance; 1280 keeps every test
 * safely inside it. The drag math below reads live bounding boxes, so
 * no width is hard-coded beyond the token pins the unit suite holds. */
test.use({ viewport: { width: 1280, height: 800 } });

const RAIL = "#app-sidebar-rail";
const TOGGLE = ".rail-toggle";
const RESIZER = ".rail-resizer";

test.describe("sidebar collapse + resize (defect #3)", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
    await expect(page.locator(RAIL)).toBeVisible();
  });

  test("fresh session starts expanded with the toggle announcing collapse", async ({ page }) => {
    const rail = page.locator(RAIL);
    await expect(rail).toHaveAttribute("data-collapsed", "false");
    const toggle = page.locator(TOGGLE);
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(toggle).toHaveAccessibleName("收起侧边栏");
    // The account footer's nickname chip renders on the student rail.
    await expect(page.locator(".rail-account-name")).toBeVisible();
  });

  test("toggle collapses to an icon rail that keeps every accessible name", async ({ page }) => {
    const rail = page.locator(RAIL);
    const expandedWidth = (await rail.boundingBox())!.width;
    await page.locator(TOGGLE).click();

    await expect(rail).toHaveAttribute("data-collapsed", "true");
    const toggle = page.locator(TOGGLE);
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(toggle).toHaveAccessibleName("展开侧边栏");

    // The rail visibly narrows to the icon-rail width…
    const collapsedWidth = (await rail.boundingBox())!.width;
    expect(collapsedWidth).toBeLessThan(expandedWidth - 100);
    // …labels leave the visual canvas via the clip pattern (1px +
    // clip-path — deliberately NOT display:none, see below)…
    await expect(page.locator(".rail-account-name")).toHaveCSS("clip-path", "inset(50%)");
    await expect(page.locator(".app-sidebar-brand-name")).toHaveCSS("clip-path", "inset(50%)");
    // …but the links keep their accessible names (clip-path, not
    // display:none) — the icon rail is not an unnamed icon cluster.
    // Scoped to the rail + exact: page content (claim rows, task
    // links) also substring-matches "任务".
    await expect(rail.getByRole("link", { name: "任务", exact: true })).toBeAttached();
    await expect(rail.getByRole("link", { name: "通知", exact: true })).toBeAttached();

    // Toggle back: the rail returns to its expanded geometry.
    await page.locator(TOGGLE).click();
    await expect(rail).toHaveAttribute("data-collapsed", "false");
    await expect(page.locator(".rail-account-name")).toBeVisible();
    const restoredWidth = (await rail.boundingBox())!.width;
    expect(restoredWidth).toBeGreaterThan(collapsedWidth + 100);
  });

  test("resizer drag widens and narrows the rail within the pixel range", async ({ page }) => {
    const rail = page.locator(RAIL);
    const resizer = page.locator(RESIZER);
    const before = (await rail.boundingBox())!;
    const handle = (await resizer.boundingBox())!;
    const startY = before.y + Math.min(300, before.height / 2);

    await page.mouse.move(handle.x + handle.width / 2, startY);
    await page.mouse.down();
    await page.mouse.move(handle.x + handle.width / 2 + 60, startY, { steps: 4 });
    await page.mouse.up();

    const widened = (await rail.boundingBox())!;
    expect(Math.round(widened.width - before.width)).toBe(60);
    // aria-valuenow tracks the settled width for assistive tech.
    await expect(resizer).toHaveAttribute("aria-valuenow", String(Math.round(widened.width)));

    // Drag back below the minimum clamps at RAIL_WIDTH_MIN_PX (208).
    const handle2 = (await resizer.boundingBox())!;
    await page.mouse.move(handle2.x + handle2.width / 2, startY);
    await page.mouse.down();
    await page.mouse.move(handle2.x + handle2.width / 2 - 400, startY, { steps: 8 });
    await page.mouse.up();
    const clamped = (await rail.boundingBox())!;
    expect(Math.round(clamped.width)).toBe(208);
  });

  test("resizer keyboard stepping is the pointer drag's equal", async ({ page }) => {
    const resizer = page.locator(RESIZER);
    await resizer.focus();
    await expect(resizer).toBeFocused();
    await expect(resizer).toHaveAttribute("aria-valuenow", "232");

    await page.keyboard.press("ArrowRight");
    await expect(resizer).toHaveAttribute("aria-valuenow", "248");
    const rail = page.locator(RAIL);
    expect(Math.round((await rail.boundingBox())!.width)).toBe(248);

    // The NON-clamped narrowing step (r5 backlog pin): ArrowLeft from
    // a mid-range value steps back by the same 16px — the clamped
    // Left-at-min case below can never catch a broken narrow branch.
    await page.keyboard.press("ArrowLeft");
    await expect(resizer).toHaveAttribute("aria-valuenow", "232");
    expect(Math.round((await rail.boundingBox())!.width)).toBe(232);

    // Home/End jump to the range bounds; clamped keys hold the bound.
    await page.keyboard.press("End");
    await expect(resizer).toHaveAttribute("aria-valuenow", "400");
    await page.keyboard.press("Home");
    await expect(resizer).toHaveAttribute("aria-valuenow", "208");
    await page.keyboard.press("ArrowLeft");
    await expect(resizer).toHaveAttribute("aria-valuenow", "208");
  });

  test("the preference survives reload (localStorage, non-secret key)", async ({ page }) => {
    await page.locator(TOGGLE).click();
    await expect(page.locator(RAIL)).toHaveAttribute("data-collapsed", "true");

    await page.reload();
    // Applied post-hydration by the mount effect — poll for it.
    await expect(page.locator(RAIL)).toHaveAttribute("data-collapsed", "true", {
      timeout: 10_000,
    });
    const stored = await page.evaluate(() =>
      window.localStorage.getItem("cq:sidebar-preference"),
    );
    expect(JSON.parse(stored!)).toEqual({ collapsed: true, widthPx: 232 });
  });

  test("a DRAGGED width survives reload too (endResizeDrag's write)", async ({ page }) => {
    // S2 (r4): the persistence test above pins only the collapsed
    // flag at the untouched default width — this one drags to a
    // non-default width and asserts the settled write restores it.
    const rail = page.locator(RAIL);
    const resizer = page.locator(RESIZER);
    const before = (await rail.boundingBox())!;
    const handle = (await resizer.boundingBox())!;
    const y = before.y + 200;
    await page.mouse.move(handle.x + handle.width / 2, y);
    await page.mouse.down();
    await page.mouse.move(handle.x + handle.width / 2 + 60, y, { steps: 4 });
    await page.mouse.up();
    await expect
      .poll(() => rail.evaluate((node) => Math.round(node.getBoundingClientRect().width)))
      .toBe(292);

    await page.reload();
    // The mount effect re-applies the stored 292 post-hydration.
    await expect
      .poll(() => rail.evaluate((node) => Math.round(node.getBoundingClientRect().width)), {
        timeout: 10_000,
      })
      .toBe(292);
    const stored = await page.evaluate(() =>
      window.localStorage.getItem("cq:sidebar-preference"),
    );
    expect(JSON.parse(stored!)).toEqual({ collapsed: false, widthPx: 292 });
  });

  test("no stored preference keeps the rem-based default (root-font scaling)", async ({ page }) => {
    // Fresh context = no cq:sidebar-preference, so the rail must ride
    // the CSS 14.5rem fallback and scale with the root font — NOT a
    // JS-written 232px, which would freeze the width for users whose
    // root font differs from 16px (reviewer finding A).
    const rail = page.locator(RAIL);
    const at16 = (await rail.boundingBox())!.width;
    expect(Math.round(at16)).toBeGreaterThanOrEqual(230);
    expect(Math.round(at16)).toBeLessThanOrEqual(234);
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "20px";
    });
    // 14.5rem at a 20px root = 290px (rem units recompute live).
    const at20 = (await rail.boundingBox())!.width;
    expect(Math.round(at20)).toBeGreaterThanOrEqual(286);
    expect(Math.round(at20)).toBeLessThanOrEqual(294);
  });

  test("below the wide band the rail (and its affordances) stay hidden", async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 800 });
    await expect(page.locator(RAIL)).toBeHidden();
    await expect(page.locator(TOGGLE)).toBeHidden();
    await expect(page.locator(RESIZER)).toBeHidden();
    // The medium band's top nav owns navigation instead.
    await expect(page.locator(".app-nav")).toBeVisible();
  });
});
