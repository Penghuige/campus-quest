/**
 * The submission-template download e2e (owner request 2026-10-16) —
 * runs against the MERGED backend endpoint; the download contract pins
 * (request shape + filename extraction) live in the unit suite. Here
 * we pin the STUDENT-visible behavior: the button on the upload panel
 * fetches a real CSV (BOM + headers) and hands the browser a download.
 */
import { expect, test } from "@playwright/test";

import { ensureStudentLogin } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const CLAIM_PATH = process.env.CQ_E2E_CLAIM_PATH;

test.skip(
  !(E2E_ENABLED && CLAIM_PATH),
  "set CQ_E2E=1 (and the seeded claim deep link) to run this suite.",
);

test("the upload panel's template button downloads the task's CSV", async ({ page }) => {
  await ensureStudentLogin(page);

  // Ride the claim deep link to the upload panel.
  await page.goto(`${BASE_URL}${CLAIM_PATH}`);

  const button = page.getByRole("button", { name: "下载本任务的提交模板（CSV）" });
  await expect(button).toBeVisible();

  // The click answers a real attachment: capture the download event.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    button.click(),
  ]);
  const suggested = download.suggestedFilename();
  expect(suggested.endsWith(".csv"), `filename=${suggested}`).toBe(true);

  // The payload is a BOM-prefixed UTF-8 CSV (Excel-safe Chinese).
  const path = await download.path();
  expect(path).toBeTruthy();
  const fs = await import("node:fs");
  const bytes = fs.readFileSync(path!);
  expect(bytes.length).toBeGreaterThan(3);
  expect(
    bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf,
    "the template carries the UTF-8 BOM",
  ).toBe(true);
});
