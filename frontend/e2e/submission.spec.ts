/**
 * CampusQuest submission e2e — Plan 09 Task 4 (claim detail + upload +
 * validation + revision UX); wired to the runner and extended with the
 * PR #2 browser-upload acceptance by Plan 10 Task 2.
 *
 * Skip guards: every test is skipped unless CQ_E2E=1 AND the seeded
 * world contract (e2e/global-setup.ts) is present, so importing the
 * file can never depend on a live backend during ordinary development.
 *
 * Environment contract (the seeded world's; defaults target the local
 * dev servers orchestrated by playwright.config.ts):
 * - CQ_E2E=1                 enable the suite (required);
 * - CQ_E2E_BASE_URL          frontend origin (default http://localhost:3000);
 * - CQ_E2E_STUDENT           seeded student credentials "number:password";
 * - CQ_E2E_CLAIM_PATH        deep link to the student's own submittable
 *                            claim A (consumed by the green-path test);
 * - CQ_E2E_CLAIM_PATH_2      deep link to submittable claim B (the
 *                            failed-validation test re-arms it, the
 *                            mobile test views it — A is terminal by then);
 * - CQ_E2E_TASK_OPEN_PATH    deep link to the fully-open task the
 *                            evidence test claims through the REAL UI;
 * - CQ_E2E_GOOD_CSV / CQ_E2E_BAD_CSV  fixture CSVs (schema-legal /
 *                            missing the required url column);
 * - CQ_E2E_STUDENT_ID / CQ_E2E_TEACHER_ID — ids for lazy token mints
 *                            (the review/approve sides ride the real API).
 *
 * PR #2 FINAL-REVIEW ACCEPTANCE (the four browser-side upload proofs,
 * asserted one by one in the evidence test below):
 *  1. the browser PUT with the VERBATIM echoed signing headers succeeds
 *     against the real MinIO (write-once If-None-Match arrives, CORS
 *     allows the frontend origin, the pinned byte count is framed by
 *     the browser itself);
 *  2. replaying the SAME URL from the browser side answers 412;
 *  3. after finalize, an overwrite attempt (same length, different
 *     content) is still rejected — the stored object cannot be swapped;
 *  4. the presigned download reads back EXACTLY the finalize-time bytes.
 *
 * Privacy pins under test (spec §40): no object key and no presigned URL
 * ever renders on the page — the upload URL is transient request state.
 */
import { readFile } from "node:fs/promises";

import { expect, test } from "./fixtures";

import { mintToken, ensureStudentLogin, runRankingJob, runValidationJob } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8000/api/v1";
const CLAIM_URL = process.env.CQ_E2E_CLAIM_PATH
  ? `${BASE_URL}${process.env.CQ_E2E_CLAIM_PATH}`
  : undefined;
const CLAIM_URL_2 = process.env.CQ_E2E_CLAIM_PATH_2
  ? `${BASE_URL}${process.env.CQ_E2E_CLAIM_PATH_2}`
  : undefined;
const TASK_OPEN_URL = process.env.CQ_E2E_TASK_OPEN_PATH
  ? `${BASE_URL}${process.env.CQ_E2E_TASK_OPEN_PATH}`
  : undefined;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240001:correct-horse"
const GOOD_CSV = process.env.CQ_E2E_GOOD_CSV;
const BAD_CSV = process.env.CQ_E2E_BAD_CSV;
const STUDENT_ID = process.env.CQ_E2E_STUDENT_ID;
const TEACHER_ID = process.env.CQ_E2E_TEACHER_ID;
const MOCK_STORAGE = process.env.CQ_E2E_MOCK_STORAGE === "1";

test.skip(
  !E2E_ENABLED,
  "set CQ_E2E=1 (and the CQ_E2E_* URLs) to run this suite.",
);

const flowReady = CLAIM_URL !== undefined && STUDENT !== undefined;
test.skip(
  !flowReady,
  "submission flow needs the seeded world (CQ_E2E_CLAIM_PATH + CQ_E2E_STUDENT); Plan 10's global setup provides both.",
);

/** Open the session (resume via cookie, first run through the real
 * form — see ensureStudentLogin) and land on the student home. */
async function loginAsStudent(page: import("@playwright/test").Page): Promise<void> {
  await ensureStudentLogin(page);
}

/**
 * Mocked storage PUT endpoint: fulfill every CROSS-ORIGIN PUT locally.
 * Same-origin API PUTs do not exist in this flow, so method+origin is a
 * sufficient predicate. Returns the intercepted request headers so the
 * caller can pin the wire shape (pinned Content-Type, no Authorization,
 * no cookies).
 */
async function mockStoragePut(page: import("@playwright/test").Page): Promise<
  Array<Record<string, string>>
> {
  const seen: Array<Record<string, string>> = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const isCrossOriginPut =
      request.method() === "PUT" && url.origin !== new URL(BASE_URL).origin;
    if (!isCrossOriginPut) {
      await route.continue();
      return;
    }
    seen.push(request.headers());
    await route.fulfill({ status: 200, body: "" });
  });
  return seen;
}

/** One browser-side cross-origin PUT through fetch (the evidence probes). */
async function browserPut(
  page: import("@playwright/test").Page,
  url: string,
  headers: Record<string, string>,
  body: Buffer,
): Promise<number> {
  const base64 = body.toString("base64");
  return page.evaluate(
    async ({ url, headers, base64 }) => {
      const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
      const response = await fetch(url, {
        method: "PUT",
        headers,
        body: bytes,
      });
      return response.status;
    },
    { url, headers, base64 },
  );
}

/** Read a presigned URL's bytes back inside the browser context. */
async function browserGetBytes(
  page: import("@playwright/test").Page,
  url: string,
): Promise<Buffer> {
  const base64 = await page.evaluate(async (url) => {
    const response = await fetch(url);
    const bytes = new Uint8Array(await response.arrayBuffer());
    let binary = "";
    for (const byte of bytes) {
      binary += String.fromCharCode(byte);
    }
    return btoa(binary);
  }, url);
  return Buffer.from(base64, "base64");
}

test.describe("student submission", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsStudent(page);
  });

  test("claim detail shows the assigned unit and never another student's", async ({ page }) => {
    await page.goto(CLAIM_URL!);

    // The assignment panel: exactly one 平台 + one 关键词 (spec §42).
    const panel = page.locator(".claim-panel");
    await expect(panel).toBeVisible();
    await expect(panel.getByText("平台", { exact: true })).toHaveCount(1);
    await expect(panel.getByText("关键词", { exact: true })).toHaveCount(1);
    // No assignment list / assignment id input anywhere (§42 pin).
    await expect(page.locator("[data-assignments-list]")).toHaveCount(0);
    await expect(page.locator("input[name='assignment_id']")).toHaveCount(0);
    // Server-authoritative deadline copy rides the panel (two lines
    // match on this page — the panel's own and the progress note).
    await expect(panel.getByText(/截止/).first()).toBeVisible();
    // Reward line is a SERVER figure verbatim (patterns §3).
    await expect(page.getByText(/当前可获得 \d+ 积分/)).toBeVisible();
  });

  test("valid upload finalizes and reaches 待审核 with a report", async ({
    page,
    request,
  }) => {
    test.skip(
      MOCK_STORAGE,
      "green path needs the fixture's real object storage (the backend HEADs the object at finalize); unset CQ_E2E_MOCK_STORAGE.",
    );
    test.skip(
      GOOD_CSV === undefined || STUDENT_ID === undefined,
      "needs CQ_E2E_GOOD_CSV + CQ_E2E_STUDENT_ID (the world's CSV fixture and account id); the seeded world provides both.",
    );
    await page.goto(CLAIM_URL!);

    const submissionReady = page.waitForResponse(
      (response) =>
        response.url().includes("/api/v1/submissions/upload-complete") &&
        response.status() === 200,
    );
    await page.locator("#submission-file").setInputFiles(GOOD_CSV!);
    await page.getByRole("button", { name: "开始上传" }).click();

    // No worker consumes the broker in the orchestrated stack (E1's
    // two-server ruling): run the REAL validation job entry, then the
    // page's own polling picks up the terminal status.
    const submission = (await (await submissionReady).json()) as {
      id: string;
    };
    runValidationJob(submission.id);

    // Polling ends in the terminal state: 待审核 (the phase line and
    // the claim progress note both carry the phrase). The bounded
    // boundary refetch swaps the live upload panel (and its 校验通过
    // report) for the claim view on its own schedule, so the
    // AUTHORITATIVE machine verdict is asserted through the real
    // validation view API instead of racing the remount.
    await expect(page.getByText("已提交，等待老师审核").first()).toBeVisible({
      timeout: 120_000,
    });
    const verdict = await request.get(
      `${API_URL}/submissions/${submission.id}/validation`,
      { headers: { Authorization: `Bearer ${mintToken(STUDENT_ID!)}` } },
    );
    expect(verdict.ok(), await verdict.text()).toBe(true);
    const report = (await verdict.json()) as {
      validation_status: string;
      report: { row_count: number; errors: unknown[] };
    };
    expect(report.validation_status).toBe("VALIDATED");
    expect(report.report.errors).toEqual([]);
    // The presigned URL never RENDERS (spec §40): dev-mode flight
    // payloads legitimately carry route strings, so the pin reads the
    // page's visible text, not its full HTML source.
    const visibleText = await page.locator("body").innerText();
    expect(visibleText, "no presigned signature material rendered").not.toContain(
      "Signature",
    );
    expect(visibleText).not.toContain("/submissions/");
  });

  test("failed validation shows the structured report and a re-upload path", async ({ page }) => {
    test.skip(
      MOCK_STORAGE,
      "validation runs server-side; the mocked PUT never delivers an object to validate.",
    );
    test.skip(
      BAD_CSV === undefined || CLAIM_URL_2 === undefined,
      "needs CQ_E2E_BAD_CSV (a file failing the task's schema) on the world's second submittable claim; Plan 10's global setup provides both.",
    );
    await page.goto(CLAIM_URL_2!);

    const submissionReady = page.waitForResponse(
      (response) =>
        response.url().includes("/api/v1/submissions/upload-complete") &&
        response.status() === 200,
    );
    await page.locator("#submission-file").setInputFiles(BAD_CSV!);
    await page.getByRole("button", { name: "开始上传" }).click();

    const submission = (await (await submissionReady).json()) as {
      id: string;
    };
    runValidationJob(submission.id);

    // Structured failure: typed summary + report + actionable retry. The
    // claim was rolled back server-side, so the panel re-arms.
    await expect(page.getByText("提交未通过校验，请修正")).toBeVisible({ timeout: 120_000 });
    await expect(page.locator(".validation-report")).toBeVisible();
    await expect(page.locator(".report-errors")).toBeVisible();
    const retryButton = page.getByRole("button", { name: "重新上传文件" });
    await expect(retryButton).toBeEnabled();
    // v1 failed -> v2 reachable (D-flow): reset re-arms the picker.
    await retryButton.click();
    await expect(page.locator("#submission-file")).toBeEnabled();
  });

  test("mocked storage PUT: pinned wire shape + retry-finalize path", async ({ page }) => {
    test.skip(
      !MOCK_STORAGE,
      "mock-mode test; set CQ_E2E_MOCK_STORAGE=1 (no real object storage in the fixture).",
    );
    const putHeaders = await mockStoragePut(page);
    await page.goto(CLAIM_URL!);

    await page.locator("#submission-file").setInputFiles({
      name: "upload.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("platform,date\n小红书,2026-09-21\n"),
    });
    await page.getByRole("button", { name: "开始上传" }).click();

    // The PUT fired exactly once with the pinned shape: CSV MIME, no
    // Authorization, no CSRF header, no cookie forwarding.
    await expect.poll(() => putHeaders.length).toBeGreaterThanOrEqual(1);
    const headers = putHeaders[0]!;
    expect(headers["content-type"]).toBe("text/csv");
    expect(headers["authorization"]).toBeUndefined();
    expect(headers["x-csrf-token"]).toBeUndefined();
    expect(headers["cookie"]).toBeUndefined();

    // The backend cannot see the object, so finalize fails -> the
    // patterns-§11 retry-finalize path (PUT landed; retry finalize only).
    await expect(page.getByText("重试完成提交")).toBeVisible({ timeout: 60_000 });
    // The presigned URL stays transient (spec §40).
    const content = await page.content();
    expect(content).not.toContain("Signature");
  });

  test.describe("mobile 375x812", () => {
    test.use({ viewport: { width: 375, height: 812 } });

    test("upload picker and phase line are reachable without scroll-hunt", async ({ page }) => {
      // Claim A is terminal by now (the green-path test consumed it);
      // claim B re-armed after its failed validation.
      await page.goto(CLAIM_URL_2 ?? CLAIM_URL!);

      const picker = page.locator("#submission-file");
      await expect(picker).toBeVisible();
      // Reachable without scroll-hunt: the panel scrolls into view on
      // demand (a hard inViewport assert would pin this world's exact
      // content height, not the affordance).
      const panel = page.locator(".upload-panel");
      await panel.scrollIntoViewIfNeeded();
      await expect(panel).toBeInViewport({ timeout: 10_000 });
    });
  });
});

test.describe("browser upload evidence (PR #2 final acceptance)", () => {
  test.skip(
    TASK_OPEN_URL === undefined ||
      GOOD_CSV === undefined ||
      STUDENT_ID === undefined ||
      TEACHER_ID === undefined,
    "needs the seeded world's open task, CSV fixture, and account ids; Plan 10's global setup provides them.",
  );

  test(
    "UI claim -> verbatim-echo PUT -> 412 replay -> post-finalize overwrite rejected -> readback == pinned bytes -> approve",
    { timeout: 240_000 },
    async ({ page, request }) => {
      await loginAsStudent(page);

      // --- claim through the REAL task page --------------------------
      const claimResponse = page.waitForResponse(
        (response) =>
          response.url().includes("/api/v1/tasks/") &&
          response.url().includes("/claim") &&
          response.status() === 201,
      );
      await page.goto(TASK_OPEN_URL!);
      await page.getByRole("button", { name: "领取任务" }).click();
      const claim = (await (await claimResponse).json()) as { claim_id: string };
      await page.goto(`${BASE_URL}/claims/${claim.claim_id}`);
      await expect(page.locator(".upload-panel")).toBeVisible();

      // --- the page's own upload: capture the signing contract -------
      // (the URL itself never renders — spec §40; the test reads it from
      // the API response only).
      const intentResponse = page.waitForResponse(
        (response) =>
          response.url().includes("/api/v1/submissions/upload-intent") &&
          response.status() === 201,
      );
      const pinned = await readFile(GOOD_CSV!);
      await page.locator("#submission-file").setInputFiles({
        name: "浏览器实证.csv",
        mimeType: "text/csv",
        buffer: pinned,
      });
      await page.getByRole("button", { name: "开始上传" }).click();
      const intent = (await (await intentResponse).json()) as {
        intent_id: string;
        upload_url: string;
        headers: Record<string, string>;
        pinned_content_length: number;
      };

      // The adapter-owned contract the browser must echo VERBATIM.
      expect(intent.headers).toEqual({
        "If-None-Match": "*",
        "Content-Type": "text/csv",
      });
      expect("Content-Length" in intent.headers).toBe(false);
      expect(intent.pinned_content_length).toBe(pinned.length);

      const finalizeResponse = page.waitForResponse(
        (response) =>
          response.url().includes("/api/v1/submissions/upload-complete") &&
          response.status() === 200,
      );
      const submission = (await (await finalizeResponse).json()) as {
        id: string;
        validation_status: string;
      };

      // (1) BROWSER PUT SUCCEEDED: the page's own XHR carried the echoed
      // headers cross-origin to the REAL MinIO (CORS allowed, write-once
      // condition honored, browser-framed Content-Length matched the
      // pin) — a failed PUT or a refused preflight would have stopped
      // the flow in the upload-failed phase long before finalize.
      expect(submission.validation_status).toBe("UPLOADED");

      runValidationJob(submission.id);
      await expect(page.getByText("已提交，等待老师审核").first()).toBeVisible({
        timeout: 120_000,
      });
      // (1) continued: the machine verdict through the real validation
      // view (the live report remounts on the boundary refetch, so the
      // API is the non-racy authority — and it proves the VALIDATED
      // report reflects THIS run's pinned bytes).
      const verdict = await request.get(
        `${API_URL}/submissions/${submission.id}/validation`,
        { headers: { Authorization: `Bearer ${mintToken(STUDENT_ID!)}` } },
      );
      expect(verdict.ok(), await verdict.text()).toBe(true);
      const validated = (await verdict.json()) as {
        validation_status: string;
        report: { row_count: number; errors: unknown[] };
      };
      expect(validated.validation_status).toBe("VALIDATED");
      expect(validated.report.row_count).toBe(2);

      // (2) SAME-URL REPLAY from the browser: 412, write-once.
      const replayStatus = await browserPut(
        page,
        intent.upload_url,
        intent.headers,
        pinned,
      );
      expect(replayStatus).toBe(412);

      // (3) POST-FINALIZE OVERWRITE: same length, DIFFERENT content —
      // still rejected; the stored object cannot be swapped.
      const overwrite = Buffer.concat([
        pinned.subarray(0, pinned.length - 1),
        Buffer.from("X"),
      ]);
      expect(overwrite).not.toEqual(pinned);
      expect(overwrite.length).toBe(pinned.length);
      const overwriteStatus = await browserPut(
        page,
        intent.upload_url,
        intent.headers,
        overwrite,
      );
      expect(overwriteStatus).toBe(412);

      // (4) READBACK: the presigned download (owner token, real API)
      // read INSIDE the browser returns EXACTLY the finalize-time bytes.
      const download = await request.get(
        `${API_URL}/submissions/${submission.id}/download`,
        { headers: { Authorization: `Bearer ${mintToken(STUDENT_ID!)}` } },
      );
      expect(download.ok(), await download.text()).toBe(true);
      const { url: downloadUrl } = (await download.json()) as { url: string };
      const readBack = await browserGetBytes(page, downloadUrl);
      expect(readBack.equals(pinned)).toBe(true);

      // --- teacher approve (real API) -> student-visible state -------
      const approved = await request.post(
        `${API_URL}/teacher/submissions/${submission.id}/approve`,
        { headers: { Authorization: `Bearer ${mintToken(TEACHER_ID!)}` } },
      );
      expect(approved.ok(), await approved.text()).toBe(true);
      const decision = (await approved.json()) as {
        claim_status: string;
        points_granted: number;
      };
      expect(decision.claim_status).toBe("COMPLETED");
      expect(decision.points_granted).toBe(100);

      // The claim page reflects the server verdict (student-visible
      // labels match backend state).
      await page.goto(`${BASE_URL}/claims/${claim.claim_id}`);
      await expect(page.getByText("任务已完成，积分已发放。")).toBeVisible({
        timeout: 30_000,
      });

      // The wallet strip shows the earned delta (seeded 100 + 100).
      await page.goto(`${BASE_URL}/rewards`);
      await expect(page.getByText("累计获得").locator("..")).toContainText("200");

      // The boards: run the REAL ranking projection at the new ledger
      // entry, then the student's own anchor reflects it.
      runRankingJob(STUDENT_ID!);
      await page.goto(`${BASE_URL}/rankings?period=all`);
      const around = page.getByLabel("我的附近");
      await expect(around).toBeVisible();
      await expect(around.locator(".board-me-tag")).toHaveCount(1);
      await expect(around).toContainText("200");
    },
  );
});
