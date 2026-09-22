/**
 * CampusQuest submission e2e — Plan 09 Task 4 (claim detail + upload +
 * validation + revision UX).
 *
 * STATUS: SPEC ONLY — NOT WIRED TO A RUNNER YET.
 *
 * Same guard pattern as `e2e/auth.spec.ts` / `e2e/task-claim.spec.ts`:
 * Playwright itself is installed by Plan 10 (no `@playwright/test`
 * dependency and no `test:e2e` script yet). Until then this file stays
 * invisible to the gates:
 * - `tsconfig.json` includes only `src/**` + `.next/**`, so `tsc` skips it;
 * - `eslint.config.mjs` lists `e2e/**` in globalIgnores for the same reason;
 * - `next build` never touches files outside `src/app`.
 * Once Plan 10 installs Playwright, remove the eslint ignore, add the
 * `test:e2e` script, and run with `CQ_E2E=1` — every test below is
 * skipped unless that flag is set, so importing the file can never
 * depend on a live backend during ordinary development.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1                enable the suite (required);
 * - CQ_E2E_BASE_URL         frontend origin (default http://localhost:3000);
 * - CQ_E2E_LOGIN_URL        login page (default $CQ_E2E_BASE_URL/login);
 * - CQ_E2E_CLAIM_URL        claim DETAIL deep link (/claims/{claimId}) to
 *                           the seeded student's own claim in a submittable
 *                           state (CLAIMED or REVISION_REQUIRED) — required
 *                           for the flow tests; Plan 10's fixture prepares
 *                           it, until then these tests skip;
 * - CQ_E2E_STUDENT          seeded student credentials "student-number:password";
 * - CQ_E2E_GOOD_CSV         path to a fixture CSV that passes the task's
 *                           submission schema (default: a minimal inline
 *                           header+rows buffer — works only if the fixture
 *                           task's schema accepts it);
 * - CQ_E2E_BAD_CSV          path to a fixture CSV that FAILS validation
 *                           (e.g. missing a required column);
 * - CQ_E2E_MOCK_STORAGE     "1" = intercept the browser's cross-origin
 *                           storage PUT and fulfill it locally (the MOCKED
 *                           STORAGE PUT ENDPOINT). The backend never
 *                           receives the object in this mode, so finalize
 *                           cannot succeed; the mock-mode test therefore
 *                           pins the PUT wire shape and the retry-finalize
 *                           UI instead of the green path. Unset (default)
 *                           passes the PUT through to the fixture's real
 *                           object storage.
 *
 * Privacy pins under test (spec §40): no object key and no presigned URL
 * ever renders on the page — the upload URL is transient request state.
 */
import { expect, test } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const LOGIN_URL = process.env.CQ_E2E_LOGIN_URL ?? `${BASE_URL}/login`;
const CLAIM_URL = process.env.CQ_E2E_CLAIM_URL;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240001:correct-horse"
const GOOD_CSV = process.env.CQ_E2E_GOOD_CSV;
const BAD_CSV = process.env.CQ_E2E_BAD_CSV;
const MOCK_STORAGE = process.env.CQ_E2E_MOCK_STORAGE === "1";

test.skip(
  !E2E_ENABLED,
  "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* URLs) to run this suite.",
);

const flowReady = CLAIM_URL !== undefined && STUDENT !== undefined;
test.skip(
  !flowReady,
  "submission flow needs CQ_E2E_CLAIM_URL (the seeded student's own submittable claim) and CQ_E2E_STUDENT; Plan 10's fixture provides both.",
);

/** Log in through the student login page (T2 flow). */
async function loginAsStudent(page: import("@playwright/test").Page): Promise<void> {
  const [username, password] = (STUDENT ?? "").split(":");
  await page.goto(LOGIN_URL);
  await page.getByLabel("学号").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
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
    // Server-authoritative deadline copy rides the panel.
    await expect(panel.getByText(/截止/)).toBeVisible();
    // Reward line is a SERVER figure verbatim (patterns §3).
    await expect(page.getByText(/当前可获得 \d+ 积分/)).toBeVisible();
  });

  test("valid upload finalizes and reaches 待审核 with a report", async ({ page }) => {
    test.skip(
      MOCK_STORAGE,
      "green path needs the fixture's real object storage (the backend HEADs the object at finalize); unset CQ_E2E_MOCK_STORAGE.",
    );
    test.skip(
      GOOD_CSV === undefined,
      "needs CQ_E2E_GOOD_CSV pointing at a CSV the task's schema accepts (Plan 10 fixture).",
    );
    await page.goto(CLAIM_URL!);

    await page.locator("#submission-file").setInputFiles(GOOD_CSV!);
    await page.getByRole("button", { name: "开始上传" }).click();

    // Polling ends in the terminal state: 待审核 + passed report.
    await expect(page.getByText("已提交，等待老师审核")).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText("校验通过")).toBeVisible();
    // The presigned URL never renders (spec §40).
    const content = await page.content();
    expect(content, "no presigned signature material in the DOM").not.toContain("Signature");
    expect(content).not.toContain("/submissions/");
  });

  test("failed validation shows the structured report and a re-upload path", async ({ page }) => {
    test.skip(
      MOCK_STORAGE,
      "validation runs server-side; the mocked PUT never delivers an object to validate.",
    );
    test.skip(
      BAD_CSV === undefined,
      "needs CQ_E2E_BAD_CSV (a file failing the task's schema, e.g. missing a required column); Plan 10 fixture.",
    );
    await page.goto(CLAIM_URL!);

    await page.locator("#submission-file").setInputFiles(BAD_CSV!);
    await page.getByRole("button", { name: "开始上传" }).click();

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
      await page.goto(CLAIM_URL!);

      const picker = page.locator("#submission-file");
      await expect(picker).toBeVisible();
      await expect(page.locator(".upload-panel")).toBeInViewport({ timeout: 10_000 });
    });
  });
});
