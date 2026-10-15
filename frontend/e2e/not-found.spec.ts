/**
 * The custom 404 (audit item B): the product speaks Chinese everywhere
 * except the router's default English not-found page. Pins the Chinese
 * copy, the minimal shell, and the way home.
 */
import { expect, test } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.");

test("a missing route renders the Chinese not-found page with a way home", async ({
  page,
}) => {
  await page.goto(`${BASE_URL}/definitely-not-a-page`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "页面不存在" })).toBeVisible();
  await expect(page.getByText("你访问的页面不存在或已被移动。")).toBeVisible();
  // No English default leakage.
  await expect(page.getByText("This page could not be found.")).toHaveCount(0);

  await page.getByRole("link", { name: "返回首页" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/`));
});
