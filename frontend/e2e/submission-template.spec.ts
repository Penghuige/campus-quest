/**
 * The submission-template download e2e (owner request 2026-10-16) —
 * runs against the MERGED backend endpoint; the download contract pins
 * (request shape + filename extraction) live in the unit suite. Here
 * we pin the STUDENT-visible behavior: the button on the upload panel
 * fetches a real CSV (BOM + headers) and hands the browser a download.
 */
import assert from "node:assert/strict";

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
  // The frozen disposition: attachment; filename="template.csv" (plain
  // quoted ASCII — the backend's confirmed form).
  assert.equal(download.suggestedFilename(), "template.csv");

  // The payload: BOM + the schema-ordered header line + trailing CRLF
  // (RFC 4180), no data rows — Excel-safe Chinese headers.
  const path = await download.path();
  assert.ok(path);
  const fs = await import("node:fs");
  const bytes = fs.readFileSync(path);
  assert.ok(
    bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf,
    "the template carries the UTF-8 BOM",
  );
  const text = bytes.subarray(3).toString("utf8");
  assert.ok(text.includes("\r\n"), "RFC 4180 CRLF line ending");
  const headerLine = text.trimEnd();
  assert.ok(headerLine.includes(","), "comma-separated schema columns");
  assert.equal(text.split("\r\n").filter((l) => l.length > 0).length, 1, "header row only, no data rows");
});
