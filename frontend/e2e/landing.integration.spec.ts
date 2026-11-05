import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

const prefix = process.env.CQ_BASE_PATH ?? "";
const intro = (page: import("@playwright/test").Page) => page.frameLocator('iframe[title="智汇华师校园介绍"]');
for (const [role, destination, title] of [["STUDENT", "/", "STUDENT keeps its authenticated workspace; about remains public"], ["TEACHER", "/teacher/reviews", "TEACHER keeps its authenticated workspace; about remains public"], ["ADMIN", "/admin", "ADMIN keeps its authenticated workspace; about remains public"]] as const) {
  test(title, async ({ page, context }) => {
    await context.addCookies([
      { name: "csrf_token", value: "session-evidence", domain: "127.0.0.1", path: "/" },
      { name: "cq_shell", value: role === "STUDENT" ? "student" : "staff", domain: "127.0.0.1", path: "/" },
    ]);
    await page.route("**/api/v1/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/auth/refresh")) return route.fulfill({ json: { access_token: "test-session-token", csrf_token: "session-evidence" } });
      if (path === "/api/v1/me") return route.fulfill({ json: { id: "00000000-0000-4000-8000-000000000001", username: "12345678", nickname: "测试同学", role, status: "ACTIVE", has_avatar: false, phone_e164: null, email_normalized: null, email_verified_at: null, created_at: "2026-11-05T00:00:00Z" } });
      return route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
    });
    await page.goto(`${prefix}/`);
    await expect(page.locator(".app-sidebar")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${prefix}${destination === "/" ? "/?" : destination}$`));
    await expect(page.locator("iframe")).toHaveCount(0);
    await page.goto(`${prefix}/about`);
    await expect(intro(page).getByRole("heading", { name: "智汇华师", exact: true })).toBeVisible();
  });
}
test("anonymous root is public SSR; protected routes still require login", async ({ page, request }) => {
  const response = await request.get(`${prefix}/`);
  expect(response.status()).toBe(200);
  const html = await response.text();
  expect(html).toContain("智汇华师校园介绍");
  expect(html).not.toContain('data-shell="workspace"');
  await page.goto(`${prefix}/`);
  await expect(intro(page).getByRole("heading", { name: "智汇华师", exact: true })).toBeVisible();
  await page.goto(`${prefix}/tasks`);
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
});
test("public introduction works independently; both entries navigate the TOP login", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${prefix}/about`);
  await intro(page).getByRole("link", { name: "直接进入", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
  await expect(page.locator("iframe")).toHaveCount(0);
  await page.goto(`${prefix}/about`);
  await intro(page).getByRole("button", { name: "进入图文介绍" }).click();
  await expect(intro(page).getByText("参与校园任务", { exact: true })).toBeVisible();
  await intro(page).getByRole("button", { name: "进入聚树成印" }).click();
  await intro(page).getByRole("button", { name: "登录智汇华师", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
});
test("every declared runtime asset is served byte-for-byte with correct JavaScript MIME", async ({ request }) => {
  const manifest = JSON.parse(readFileSync("public/landing/runtime-manifest.json", "utf8"));
  for (const item of manifest.files) {
    const r = await request.get(`${prefix}/landing/${item.path}`);
    expect(r.status(), item.path).toBe(200);
    expect(createHash("sha256").update(await r.body()).digest("hex"), item.path).toBe(item.sha256);
    if (/\.[cm]?js$/.test(item.path)) expect(r.headers()["content-type"]).toContain("javascript");
  }
  expect((await request.get(`${prefix}/landing/no-such-module.mjs`)).status()).toBe(404);
});
test("stale cookie resolves anonymous to Landing; session outage remains retryable", async ({ page, context }) => {
  await context.addCookies([{ name: "csrf_token", value: "expired-evidence", domain: "127.0.0.1", path: "/" }]);
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ error: { code: "AUTHENTICATION_REQUIRED", message: "Login required", request_id: "landing-test" } }) }));
  await page.goto(`${prefix}/`);
  await expect(intro(page).getByRole("heading", { name: "智汇华师", exact: true })).toBeVisible();
  await page.unroute("**/api/v1/**");
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, contentType: "application/json", body: "{}" }));
  await page.reload();
  await expect(page.getByRole("button", { name: "重新加载" })).toBeVisible();
  await expect(page.locator("iframe")).toHaveCount(0);
});
test("no WebGL and missing stamp still expose real login recovery", async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (...args) {
      if (String(args[0]).includes("webgl")) return null;
      return Reflect.apply(original, this, args);
    } as typeof HTMLCanvasElement.prototype.getContext;
  });
  await page.route("**/seal/assets/imprint.png", (route) => route.abort());
  await page.goto(`${prefix}/about`);
  await intro(page).getByRole("button", { name: "进入图文介绍" }).click();
  await intro(page).getByRole("button", { name: "进入聚树成印" }).click();
  const recovery = intro(page).getByRole("link", { name: "前往登录", exact: true });
  await expect(recovery).toBeVisible();
  await recovery.click();
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
});
test("mobile static hero is readable and accessible", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${prefix}/about`);
  const frame = intro(page);
  await expect(frame.locator("#hero")).toHaveAttribute("data-mode", "static");
  await frame.locator(".campus-painting").evaluate((image: HTMLImageElement) => image.decode());
  await frame.locator("body").evaluate(() => document.fonts.ready);
  expect(await frame.locator("body").evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const accessibility = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(accessibility.violations).toEqual([]);
  await expect(page.locator("iframe")).toHaveScreenshot("landing-mobile.png", { animations: "disabled" });
});
test("desktop terminal keeps the approved stamp and keyboard login", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${prefix}/about`);
  await intro(page).getByRole("button", { name: "进入图文介绍" }).click();
  await intro(page).getByRole("button", { name: "进入聚树成印" }).click();
  const stamp = intro(page).getByRole("button", { name: "登录智汇华师", exact: true });
  await expect(stamp).toBeVisible();
  await expect(intro(page).locator("#stamp-image")).toHaveJSProperty("naturalWidth", 305);
  await intro(page).locator("body").evaluate(() => document.fonts.ready);
  await expect(page.locator("iframe")).toHaveScreenshot("stamp-desktop.png", { animations: "disabled" });
  await stamp.press("Enter");
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
});
test("desktop motion reaches DONE through the real composed document", async ({ page }) => {
  await page.goto(`${prefix}/about`);
  const frame = intro(page);
  await frame.getByRole("button", { name: "进入图文介绍" }).click();
  await frame.getByRole("button", { name: "进入聚树成印" }).click();
  await expect(frame.locator("html")).toHaveAttribute("data-scene-phase", "idle");
  await frame.locator("body").evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" }));
  await expect(frame.locator("#state")).toContainText("DONE", { timeout: 20_000 });
  const stamp = frame.getByRole("button", { name: "登录智汇华师", exact: true });
  await expect(stamp).toBeVisible();
  await expect(frame.locator("#hint")).toHaveCSS("opacity", "0");
  await stamp.click();
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
});
test("failed renderer module retains an independent actionable login", async ({ page }) => {
  await page.route("**/seal/render.mjs", (route) => route.abort());
  await page.goto(`${prefix}/about`);
  const frame = intro(page);
  await frame.getByRole("button", { name: "进入图文介绍" }).click();
  await frame.getByRole("button", { name: "进入聚树成印" }).click();
  const recovery = frame.getByRole("link", { name: "前往登录", exact: true });
  await expect(recovery).toBeVisible();
  await recovery.click();
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
});


test("failed renderer dependency restores keyboard login focus", async ({ page }) => {
  await page.route("**/seal/vendor/three/build/three.core.js", (route) => route.abort());
  await page.goto(`${prefix}/about`);
  const frame = intro(page);
  await frame.getByRole("button", { name: "进入图文介绍" }).click();
  await frame.getByRole("button", { name: "进入聚树成印" }).click();
  const recovery = frame.getByRole("link", { name: "前往登录", exact: true });
  await expect(recovery).toBeFocused();
  await recovery.press("Enter");
  await expect(page).toHaveURL(new RegExp(`${prefix}/login$`));
});
