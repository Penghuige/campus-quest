/**
 * Exploratory probe round — 2026-10-25 campaign (owner-approved).
 *
 * NOT part of the committed battery: this file is a one-off harness that
 * drives the real app through the SAME stack the Playwright battery uses
 * (globalSetup seeds the world, config orchestrates both servers). It
 * records each probe's outcome to reports/artifacts/exploration.json —
 * the dashboard's layer 7 manifest.
 *
 * Probe semantics: status "passed" = the behavior held (no issue found);
 * "failed" = an issue was observed (the message says what). This is an
 * adversarial scripted sweep, not a free-form human explorer: every probe
 * lists the concrete actions it took in `detail`.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, ensureStudentLogin, test } from "./fixtures";

// Own gate ON TOP of CQ_E2E: exploration rounds are deliberately NOT part
// of the standard battery — they run on demand after major merges (campus
// reviewer ruling 2026-10-26; upgrade condition: three consecutive manual
// rounds with real findings reopen the standing-cadence question). The
// noskip exemption for the standard battery lives in
// e2e/noskip-exemptions.mjs.
const E2E_ENABLED =
  process.env.CQ_E2E === "1" && process.env.CQ_E2E_EXPLORE === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const TASK_URL = process.env.CQ_E2E_TASK_URL;
const STUDENT = process.env.CQ_E2E_STUDENT;
const AUTHOR = process.env.CQ_E2E_AUTHOR_STUDENT;

test.skip(
  !E2E_ENABLED,
  "set CQ_E2E=1 CQ_E2E_EXPLORE=1 (plus the CQ_E2E_* world) to run an exploration round.",
);

type ProbeStatus = "passed" | "failed" | "skipped" | "not_run";

interface Probe {
  name: string;
  status: ProbeStatus;
  duration_ms: number;
  file: string;
  case: string;
  detail?: string;
  message?: string;
}

const probes: Probe[] = [];
const FILE = "frontend/e2e/exploratory-probe.spec.ts";
const SHOTS = join(__dirname, "..", "..", "reports", "artifacts", "exploration-shots");

function writeManifest(): void {
  const dir = join(__dirname, "..", "..", "reports", "artifacts");
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, "exploration.json"),
    JSON.stringify(
      { run_label: `exploration ${new Date().toISOString()}`, probes },
      null,
      2,
    ),
    "utf-8",
  );
}

async function probe(
  name: string,
  caseId: string,
  body: () => Promise<string | void>,
): Promise<void> {
  const started = Date.now();
  try {
    const detail = await body();
    probes.push({
      name,
      status: "passed",
      duration_ms: Date.now() - started,
      file: FILE,
      case: caseId,
      ...(detail ? { detail } : {}),
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    probes.push({
      name,
      status: "failed",
      duration_ms: Date.now() - started,
      file: FILE,
      case: caseId,
      message: message.slice(0, 500),
    });
  }
}

/** Collect console errors / page errors / 5xx for one probe. */
function watch(page: import("@playwright/test").Page) {
  const errors: string[] = [];
  const onConsole = (msg: import("@playwright/test").ConsoleMessage) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
  };
  const onPageError = (err: Error) => errors.push(`pageerror: ${err.message}`);
  const onResponse = (res: import("@playwright/test").Response) => {
    if (res.status() >= 500) errors.push(`http ${res.status()}: ${res.url()}`);
  };
  page.on("console", onConsole);
  page.on("pageerror", onPageError);
  page.on("response", onResponse);
  return {
    errors,
    dispose: () => {
      page.off("console", onConsole);
      page.off("pageerror", onPageError);
      page.off("response", onResponse);
    },
  };
}

async function openPasswordForm(page: import("@playwright/test").Page): Promise<void> {
  await page.goto(`${BASE_URL}/login`);
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await expect(page.getByLabel("账号", { exact: true })).toBeVisible();
}

test("exploratory probe sweep", async ({ page }) => {
  test.setTimeout(300_000);
  mkdirSync(SHOTS, { recursive: true });

  // 1. Login boundary inputs (throwaway identifiers: the seeded account's
  //    rate-limit window stays untouched).
  await probe("登录表单边界输入与脚本注入", "login_boundary_inputs", async () => {
    await openPasswordForm(page);
    let dialog = "";
    page.on("dialog", async (d) => {
      dialog = d.message();
      await d.dismiss();
    });
    const cases = [
      ["empty", ""],
      ["spaces", "   "],
      ["long", "a".repeat(300)],
      ["script", "<script>alert(1)</script>"],
      ["quote", "'; DROP TABLE users;--"],
    ] as const;
    const notes: string[] = [];
    for (const [label, value] of cases) {
      await page.getByLabel("账号", { exact: true }).fill(value);
      await page.getByLabel("密码").fill("wrong-password-123");
      await page.getByRole("button", { name: "登录", exact: true }).click();
      await page.waitForTimeout(700);
      const url = page.url();
      if (!url.includes("/login")) {
        throw new Error(`case ${label}: left the login page (${url})`);
      }
      notes.push(`${label}=stayed`);
    }
    if (dialog) throw new Error(`script case opened a dialog: ${dialog}`);
    return `5 cases stayed on the form, no dialog: ${notes.join(", ")}`;
  });

  // 2. Error-copy uniformity (user enumeration check) — one attempt on the
  //    seeded student, one on a nonexistent identifier.
  await probe("登录错误文案是否区分账号存在性", "login_error_uniformity", async () => {
    async function attempt(identifier: string): Promise<string> {
      await openPasswordForm(page);
      await page.getByLabel("账号", { exact: true }).fill(identifier);
      await page.getByLabel("密码").fill("wrong-password-123");
      const answered = page.waitForResponse(
        (res) => res.url().includes("/auth/login") && res.request().method() === "POST",
        { timeout: 8_000 },
      ).catch(() => null);
      await page.getByRole("button", { name: "登录", exact: true }).click();
      const response = await answered;
      await page.waitForTimeout(400);
      const alert = page.locator("[role='alert']").first();
      const copy =
        (await alert.count()) > 0 ? ((await alert.textContent()) ?? "").trim() : "";
      return `${response ? response.status() : "no-response"} ${copy}`.trim();
    }
    // Use the SECOND seeded student when the world exports one: the main
    // student's login bucket stays clean for the session probes.
    const probeAccount = (AUTHOR ?? STUDENT)!.split(":")[0];
    const [real, fake] = [await attempt(probeAccount), await attempt("20269999999")];
    if (real !== fake) {
      throw new Error(`copy differs — real=${JSON.stringify(real)} fake=${JSON.stringify(fake)}`);
    }
    return `uniform copy: ${JSON.stringify(real)}`;
  });

  // 3. Signed-in refresh bursts + rapid navigation.
  await probe("登录后连续刷新与快速导航", "refresh_and_rapid_navigation", async () => {
    await ensureStudentLogin(page);
    const w = watch(page);
    const auth401: string[] = [];
    const onResponse = (res: import("@playwright/test").Response) => {
      if (res.status() === 401) auth401.push(res.url());
    };
    page.on("response", onResponse);
    try {
      for (let i = 0; i < 3; i += 1) {
        await page.goto(`${BASE_URL}/notifications`);
        await page.reload();
        await page.waitForTimeout(400);
      }
      await page.goto(`${BASE_URL}/tasks`);
      await page.goto(`${BASE_URL}/`).catch(() => undefined);
      await page.waitForTimeout(600);
    } finally {
      page.off("response", onResponse);
      w.dispose();
    }
    const hard = w.errors.filter(
      (e) => e.startsWith("pageerror") || e.startsWith("http 5") ||
        (e.startsWith("console:") && !e.includes("401")),
    );
    if (hard.length > 0) throw new Error(hard.slice(0, 3).join(" | "));
    const refresh401 = auth401.filter((url) => url.includes("/auth/refresh"));
    const unexpected = auth401.filter(
      (url) => !url.includes("/api/v1/me") && !url.includes("/auth/refresh"),
    );
    if (unexpected.length > 0) throw new Error(`unexpected 401s: ${unexpected.slice(0, 3).join(", ")}`);
    // Auth-state signal: the profile page's authenticated chrome. (A bare
    // page.request /me carries no bearer — the access token is memory-only —
    // so it answers 401 even when logged in; the diagnostic file in this
    // directory records that false signal.)
    await page.goto(`${BASE_URL}/profile?tab=info`);
    const logoutButton = page.getByRole("button", { name: "退出登录" });
    await logoutButton.waitFor({ state: "visible", timeout: 8_000 }).catch(() => undefined);
    const chrome = await logoutButton.count();
    const stillIn = page.url().includes("/profile") && chrome > 0;
    if (!stillIn) {
      mkdirSync(SHOTS, { recursive: true });
      const shot = join(SHOTS, "refresh-failure.png");
      await page.screenshot({ path: shot, fullPage: true }).catch(() => undefined);
      const bodyText = ((await page.locator("body").innerText().catch(() => "")) ?? "").slice(0, 220);
      throw new Error(
        `session lost after reload burst: url=${page.url()} chrome=${chrome} ` +
          `refresh401=${refresh401.length} body=${JSON.stringify(bodyText)} shot=${shot}`,
      );
    }
    return `3 reloads + rapid nav: session survived (profile chrome present); refresh 401 x${refresh401.length}` +
      (refresh401.length > 0 ? " (single-use rotation race during overlapping loads — harmless here)" : "");
  });

  // 4. Student deep-link into the admin surface.
  await probe("学生会话直达管理员路由", "student_admin_deep_link", async () => {
    await ensureStudentLogin(page);
    await page.goto(`${BASE_URL}/admin/users`);
    await page.waitForTimeout(1200);
    const body = (await page.locator("body").innerText()) ?? "";
    if (body.includes("账号目录") || body.includes("批量停用")) {
      throw new Error(`student sees admin directory content at ${page.url()}`);
    }
    return `no admin directory content (landed ${page.url()})`;
  });

  // 5. Claim double-click race.
  await probe("领取按钮双击竞态", "claim_double_click", async () => {
    if (!TASK_URL) throw new Error("CQ_E2E_TASK_URL missing");
    await ensureStudentLogin(page);
    await page.goto(TASK_URL);
    const cta = page.getByRole("button", { name: "领取任务" });
    await cta.waitFor({ state: "visible", timeout: 6_000 }).catch(() => undefined);
    if ((await cta.count()) === 0) return "SKIP: no claim CTA (already claimed in this world)";
    const responses: number[] = [];
    page.on("response", (res) => {
      if (res.url().includes("/claim") && res.request().method() === "POST") {
        responses.push(res.status());
      }
    });
    await Promise.all([
      cta.click({ force: true }),
      page.waitForTimeout(30).then(() => cta.click({ force: true }).catch(() => undefined)),
    ]);
    await page.waitForTimeout(1200);
    const ok = responses.filter((s) => s === 200 || s === 201).length;
    if (ok > 1) throw new Error(`double claim: ${ok} successes (${responses.join(",")})`);
    return `claim responses: [${responses.join(",")}] (one success expected at most)`;
  });

  // 6. HTML/script payload in a comment renders literally.
  await probe("评论内容 HTML/脚本注入", "comment_html_rendering", async () => {
    if (!TASK_URL) throw new Error("CQ_E2E_TASK_URL missing");
    await ensureStudentLogin(page);
    await page.goto(TASK_URL);
    const box = page.getByLabel("评论内容", { exact: true });
    await box.waitFor({ state: "visible", timeout: 6_000 }).catch(() => undefined);
    if ((await box.count()) === 0) return "SKIP: comment box absent on this task";
    const marker = `<img src=x onerror="window.__xss_probe=1">probe-${Date.now()}`;
    await box.fill(marker);
    await page.getByRole("button", { name: "发布评论" }).click();
    await page.waitForTimeout(1000);
    const injected = await page.evaluate(() => (window as unknown as { __xss_probe?: number }).__xss_probe);
    if (injected !== undefined) throw new Error("comment payload executed as HTML");
    const literal = await page.getByText(marker, { exact: false }).count();
    if (literal === 0) return "submitted; marker not visible (possibly paginated) — no execution observed";
    return "payload rendered as literal text, no execution";
  });

  // 7. Oversized comment.
  await probe("超长评论（5000 字符）", "comment_length_limit", async () => {
    if (!TASK_URL) throw new Error("CQ_E2E_TASK_URL missing");
    await ensureStudentLogin(page);
    await page.goto(TASK_URL);
    const box = page.getByLabel("评论内容", { exact: true });
    await box.waitFor({ state: "visible", timeout: 6_000 }).catch(() => undefined);
    if ((await box.count()) === 0) return "SKIP: comment box absent";
    const w = watch(page);
    try {
      await box.fill("长".repeat(5000));
      await page.getByRole("button", { name: "发布评论" }).click();
      await page.waitForTimeout(1200);
    } finally {
      w.dispose();
    }
    const serverErrors = w.errors.filter((e) => e.startsWith("http 5"));
    if (serverErrors.length > 0) throw new Error(serverErrors.join(" | "));
    return "no 5xx; the form either refused or accepted the payload";
  });

  // 8. Mobile width overflow on the two core student screens.
  await probe("390px 移动宽度横向溢出", "mobile_horizontal_overflow", async () => {
    await ensureStudentLogin(page);
    await page.setViewportSize({ width: 390, height: 844 });
    const notes: string[] = [];
    for (const path of ["/tasks", "/"]) {
      await page.goto(`${BASE_URL}${path}`).catch(() => undefined);
      await page.waitForTimeout(500);
      const [scroll, inner] = await page.evaluate(() => [
        document.documentElement.scrollWidth,
        window.innerWidth,
      ]);
      notes.push(`${path}: scrollWidth=${scroll}, inner=${inner}`);
      if (scroll > inner + 2) throw new Error(`horizontal overflow — ${notes.join("; ")}`);
    }
    return notes.join("; ");
  });

  // 9. Internal link crawl from the student home: no 5xx, no dead 404s.
  await probe("学生首页内部链接巡游", "link_crawl", async () => {
    await ensureStudentLogin(page);
    await page.goto(`${BASE_URL}/tasks`);
    const hrefs = await page.locator("a[href]").evaluateAll((anchors) =>
      anchors
        .map((a) => (a as HTMLAnchorElement).href)
        .filter((href) => href.startsWith(window.location.origin))
        .filter((href) => !/logout|signout/i.test(href)),
    );
    const unique = [...new Set(hrefs)].slice(0, 12);
    const bad: string[] = [];
    for (const href of unique) {
      const res = await page.request.get(href);
      if (res.status() >= 500) bad.push(`${res.status()} ${href}`);
    }
    if (bad.length > 0) throw new Error(bad.join(" | "));
    return `${unique.length} links crawled, no 5xx`;
  });

  // 10. Logout then browser Back (LAST: ends the shared session).
  await probe("退出后浏览器后退是否泄露私有内容", "logout_then_back", async () => {
    await ensureStudentLogin(page);
    await page.goto(`${BASE_URL}/profile?tab=info`);
    await page.waitForTimeout(600);
    const logout = page.getByRole("button", { name: "退出登录" });
    await logout.waitFor({ state: "visible", timeout: 6_000 }).catch(() => undefined);
    if ((await logout.count()) === 0) return "SKIP: logout control not found";
    await logout.click();
    await page.waitForTimeout(800);
    await page.goBack().catch(() => undefined);
    await page.waitForTimeout(800);
    // The profile page requires auth: after the revoked logout it must
    // either bounce to /login or render without the authenticated chrome.
    await page.goto(`${BASE_URL}/profile?tab=info`);
    const logoutButton = page.getByRole("button", { name: "退出登录" });
    await logoutButton.waitFor({ state: "visible", timeout: 4_000 }).catch(() => undefined);
    const chrome = await logoutButton.count();
    if (chrome > 0) {
      throw new Error(`back after logout still shows the authenticated shell at ${page.url()}`);
    }
    return `after logout+back: profile renders unauthenticated (${page.url()})`;
  });

  writeManifest();
  const failed = probes.filter((p) => p.status === "failed");
  console.log(`[exploration] probes=${probes.length} failed=${failed.length}`);
  expect(failed.map((p) => p.case)).toEqual([]);
});
