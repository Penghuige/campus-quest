/**
 * CampusQuest community e2e — Plan 09 Task 6 (comments, votes,
 * reactions, reports, ratings).
 *
 * STATUS: SPEC ONLY — NOT WIRED TO A RUNNER YET.
 *
 * Same guard pattern as `e2e/auth.spec.ts` / `e2e/task-claim.spec.ts`:
 * Playwright itself is installed by Plan 10 (no `@playwright/test`
 * dependency and no `test:e2e` script yet). Until then this file stays
 * invisible to the gates (tsconfig includes only `src/**`, eslint
 * globalIgnores lists `e2e/**`, `next build` never leaves `src/app`).
 * Once Plan 10 installs Playwright, remove the eslint ignore, add the
 * `test:e2e` script, and run with `CQ_E2E=1`.
 *
 * Environment contract (defaults work against local dev servers):
 * - CQ_E2E=1                    enable the suite (required);
 * - CQ_E2E_BASE_URL             frontend origin (default http://localhost:3000);
 * - CQ_E2E_LOGIN_URL            login page (default $CQ_E2E_BASE_URL/login);
 * - CQ_E2E_TASK_URL             task DETAIL deep link to a published
 *                               task (required; Plan 10's fixture seeds it);
 * - CQ_E2E_STUDENT              pre-seeded VIEWER student credentials
 *                               "student-number:password" (required);
 * - CQ_E2E_AUTHOR_STUDENT       a SECOND seeded student who posts the
 *                               anonymous comment under test (required
 *                               for the privacy test);
 * - CQ_E2E_AUTHOR_SECRETS       comma-separated identity values of that
 *                               author (their nickname, student number,
 *                               phone, email, user id) that must NEVER
 *                               reach another student's DOM (required
 *                               for the privacy test; when unset only
 *                               the structural pins run);
 * - CQ_E2E_NON_COMPLETER_STUDENT  seeded student who never COMPLETED a
 *                               claim on the task (optional; drives the
 *                               strict RATING_NOT_ELIGIBLE copy test).
 * - CQ_E2E_REVEAL_ADMIN          seeded ADMIN credentials
 *                               "username:password" for the staff login
 *                               (the reveal flow; Plan 10's fixture
 *                               seeds the account with an answerable
 *                               TOTP credential).
 * - CQ_E2E_REVEAL_ADMIN_TOTP_SECRET  that admin's genuine base32 TOTP
 *                               secret (the RFC 6238 code is computed
 *                               in-test via node:crypto).
 * - CQ_E2E_MODERATION_TASK_PATH  the /teacher/tasks/<id> deep link to
 *                               the task whose moderation view carries
 *                               the pre-seeded anonymous comment the
 *                               reveal dialog targets.
 *
 * PRIVACY PIN under test (spec §21.4/§40): an anonymous comment is
 * 匿名用户 on student surfaces — the author's nickname, student number,
 * phone, email, and user id appear NEITHER in rendered text NOR in the
 * DOM source.
 */
import { createHmac } from "node:crypto";

import { ensureStudentLogin, expect, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const LOGIN_URL = process.env.CQ_E2E_LOGIN_URL ?? `${BASE_URL}/login`;
const STAFF_LOGIN_URL = process.env.CQ_E2E_STAFF_LOGIN_URL ?? `${BASE_URL}/staff/login`;
const TASK_URL = process.env.CQ_E2E_TASK_URL;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240002:correct-horse"
const AUTHOR_STUDENT = process.env.CQ_E2E_AUTHOR_STUDENT;
const AUTHOR_SECRETS = (process.env.CQ_E2E_AUTHOR_SECRETS ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter((value) => value.length > 0);
const NON_COMPLETER = process.env.CQ_E2E_NON_COMPLETER_STUDENT;
const REVEAL_ADMIN = process.env.CQ_E2E_REVEAL_ADMIN;
const REVEAL_ADMIN_TOTP_SECRET = process.env.CQ_E2E_REVEAL_ADMIN_TOTP_SECRET;
const MODERATION_TASK_PATH = process.env.CQ_E2E_MODERATION_TASK_PATH;

test.skip(
  !E2E_ENABLED,
  "Playwright lands in Plan 10; set CQ_E2E=1 (and the CQ_E2E_* fixtures) to run this suite.",
);

const flowsReady = TASK_URL !== undefined && STUDENT !== undefined;
test.skip(
  !flowsReady,
  "community flows need CQ_E2E_TASK_URL (a published task) and CQ_E2E_STUDENT (seeded credentials); Plan 10's fixture provides both.",
);

/** Log in through the student login page (T2 flow). */
async function loginAs(
  page: import("@playwright/test").Page,
  credentials: string,
): Promise<void> {
  const [username, password] = credentials.split(":");
  await page.goto(LOGIN_URL);
  await page.getByLabel("学号").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
}

/** RFC 4648 base32 (the TOTP secret alphabet) -> bytes. */
function base32Decode(input: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of input.toUpperCase().replace(/=+$/, "")) {
    const index = alphabet.indexOf(char);
    if (index === -1) {
      throw new Error(`non-base32 character in secret: ${char}`);
    }
    value = (value << 5) | index;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** The current RFC 6238 code for `secret` (SHA-1, 30s step, 6 digits). */
function totpCode(secret: string, atMs: number = Date.now()): string {
  const counter = Math.floor(atMs / 30_000);
  const block = Buffer.alloc(8);
  block.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac("sha1", base32Decode(secret)).update(block).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary =
    ((digest[offset] & 0x7f) << 24) |
    ((digest[offset + 1] & 0xff) << 16) |
    ((digest[offset + 2] & 0xff) << 8) |
    digest[offset + 3];
  return String(binary % 1_000_000).padStart(6, "0");
}

/** Staff login through the real form; retries across a 30s step rollover. */
async function loginAsStaff(
  page: import("@playwright/test").Page,
  credentials: string,
  totpSecret: string,
): Promise<void> {
  const [username, password] = credentials.split(":");
  await page.goto(STAFF_LOGIN_URL);
  await page.getByLabel("邮箱").fill(username);
  await page.getByLabel("密码").fill(password);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.getByLabel("动态验证码").fill(totpCode(totpSecret));
    await page.getByRole("button", { name: "登录", exact: true }).click();
    try {
      await expect(page).not.toHaveURL(/\/staff\/login/, { timeout: 5_000 });
      return;
    } catch {
      // A slow hop can carry the submit across the step boundary —
      // recompute the code exactly like a real user would.
    }
  }
  await expect(page).not.toHaveURL(/\/staff\/login/);
}

/** Unique marker so repeated runs never match stale comments. */
function marker(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}`;
}

test.describe("anonymous comment DOM privacy (spec §21.4/§40) — brief step 1", () => {
  test.skip(
    AUTHOR_STUDENT === undefined,
    "needs CQ_E2E_AUTHOR_STUDENT plus CQ_E2E_AUTHOR_SECRETS (a second seeded student and their identity values).",
  );

  test("an anonymous comment leaks none of its author's identity", async ({
    browser,
  }) => {
    const content = marker("匿名端到端");

    // Context A: the author posts ONE anonymous comment. The identity
    // options are styled with hidden native inputs (globals.css
    // `.identity-option input`), so the interaction clicks the visible
    // label text — exactly what a user does.
    const authorContext = await browser.newContext();
    const authorPage = await authorContext.newPage();
    await loginAs(authorPage, AUTHOR_STUDENT!);
    await authorPage.goto(TASK_URL!);
    await authorPage
      .getByRole("radiogroup", { name: "发布身份" })
      .getByText("匿名", { exact: true })
      .click();
    await expect(authorPage.getByText(/将以「匿名用户」身份发布/)).toBeVisible();
    await authorPage
      .getByLabel("评论内容", { exact: true })
      .fill(`${content} 大家记得提前预约座位`);
    await authorPage.getByRole("button", { name: "发布评论" }).click();
    await expect(authorPage.getByText(content)).toBeVisible();
    await expect(authorPage.getByText("匿名用户").first()).toBeVisible();
    await authorContext.close();

    // Context B: ANOTHER student opens the same task.
    const viewerContext = await browser.newContext();
    const viewerPage = await viewerContext.newPage();
    await ensureStudentLogin(viewerPage);
    await viewerPage.goto(TASK_URL!);

    const section = viewerPage.getByLabel("任务评论");
    await expect(section).toBeVisible();
    // The comment itself renders, attributed to 匿名用户 only.
    await expect(section.getByText(content)).toBeVisible();
    await expect(section.getByText("匿名用户").first()).toBeVisible();

    // Rendered TEXT must not carry the author's identity…
    const bodyText = await viewerPage.locator("body").innerText();
    // …and neither may the raw DOM source (attributes included).
    const dom = await viewerPage.content();
    for (const secret of AUTHOR_SECRETS) {
      expect(bodyText, `page text must not contain ${secret}`).not.toContain(secret);
      expect(dom, `DOM source must not contain ${secret}`).not.toContain(secret);
    }

    // Structural pins: no identity hooks, and no student-number-like
    // digit runs inside the comment list (comment UUIDs never render).
    await expect(viewerPage.locator("[data-user-id]")).toHaveCount(0);
    const listText = await section.locator(".comment-list").innerText();
    expect(listText, "no student-number-like digit runs in the thread").not.toMatch(
      /[0-9]{6,}/,
    );
    await viewerContext.close();
  });
});

test.describe("composer: explicit identity + preview (§21.4)", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(TASK_URL!);
  });

  test("defaults to 公开昵称 with a clear preview; anonymous is a visible opt-in", async ({
    page,
  }) => {
    const identity = page.getByRole("radiogroup", { name: "发布身份" });
    // Hidden native inputs: assert STATE on the input, ACT on the label.
    await expect(identity.getByLabel("公开昵称")).toBeChecked();
    await expect(page.getByText(/将以公开昵称/)).toBeVisible();

    await identity.getByText("匿名", { exact: true }).click();
    await expect(page.getByText(/将以「匿名用户」身份发布/)).toBeVisible();
    await expect(page.getByText(/不显示你的昵称/)).toBeVisible();
  });

  test("posting a named comment publishes immediately with the chosen identity", async ({
    page,
  }) => {
    const content = marker("公开端到端");
    await page.getByLabel("评论内容", { exact: true }).fill(content);
    await page.getByRole("button", { name: "发布评论" }).click();
    // Publish-immediately (§21.1): no moderation queue copy in between.
    await expect(page.getByLabel("任务评论").getByText(content)).toBeVisible();
  });

  test("whitespace-only draft is rejected by the mirrored validator", async ({
    page,
  }) => {
    await page.getByLabel("评论内容", { exact: true }).fill("   ");
    await page.getByRole("button", { name: "发布评论" }).click();
    await expect(page.getByText("评论内容不能为空")).toBeVisible();
    // Nothing was posted: the composer is still present, still dirty.
    await expect(page.getByLabel("评论内容", { exact: true })).toHaveValue("   ");
  });
});

test.describe("two-level threading (§21.2)", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(TASK_URL!);
  });

  test("a reply nests under its root; a reply to the reply stays at level 2", async ({
    page,
  }) => {
    const section = page.getByLabel("任务评论");
    const rootContent = marker("根评论");
    const replyContent = marker("一级回复");
    const deepContent = marker("二级回复");

    await page.getByLabel("评论内容", { exact: true }).fill(rootContent);
    await page.getByRole("button", { name: "发布评论" }).click();
    await expect(section.getByText(rootContent)).toBeVisible();

    // Reply to the root comment just posted.
    const rootArticle = section.locator(".comment", {
      hasText: rootContent,
    });
    await rootArticle.getByRole("button", { name: "回复", exact: true }).click();
    await expect(
      rootArticle.getByText(`回复`, { exact: true }).first(),
    ).toBeVisible();
    await page.getByLabel("回复内容", { exact: true }).fill(replyContent);
    await page.getByRole("button", { name: "发布回复" }).click();
    const thread = section.locator(".comment-thread", { hasText: rootContent });
    await expect(thread.locator(".comment-depth-2", { hasText: replyContent })).toBeVisible();

    // Reply to THAT reply: still inside the same root thread at the
    // same second level — a third visual level never exists (§21.2).
    const replyArticle = thread.locator(".comment-depth-2", { hasText: replyContent });
    await replyArticle.getByRole("button", { name: "回复", exact: true }).click();
    await page.getByLabel("回复内容", { exact: true }).fill(deepContent);
    await page.getByRole("button", { name: "发布回复" }).click();
    await expect(
      thread.locator(".comment-depth-2", { hasText: deepContent }),
    ).toBeVisible();
    await expect(section.locator(".comment-depth-3")).toHaveCount(0);
  });
});

test.describe("votes and reactions (§22)", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(TASK_URL!);
    // Ensure at least one comment exists to act on.
    const content = marker("互动评论");
    await page.getByLabel("评论内容", { exact: true }).fill(content);
    await page.getByRole("button", { name: "发布评论" }).click();
    await expect(page.getByLabel("任务评论").getByText(content)).toBeVisible();
    await page
      .locator(".comment", { hasText: content })
      .first()
      .scrollIntoViewIfNeeded();
  });

  test("like shows a count, and pressing it again removes the vote", async ({
    page,
  }) => {
    const like = page.locator(".comment").first().getByRole("button", { name: "赞", exact: true });
    await like.click();
    // The POST echo renders the authoritative total: 1 like.
    await expect(like.getByText("1")).toBeVisible();
    await expect(like).toHaveAttribute("aria-pressed", "true");

    // Same direction again = toggle off (§22); count returns to 0.
    await like.click();
    await expect(like.getByText("0")).toBeVisible();
    await expect(like).toHaveAttribute("aria-pressed", "false");
  });

  test("an emoji reaction toggles with its aggregated count", async ({ page }) => {
    const fire = page
      .locator(".comment")
      .first()
      .getByRole("button", { name: "表情 🔥" });
    await fire.click();
    await expect(fire).toHaveAttribute("aria-pressed", "true");
    // The product renders the count span only for COUNTED emojis
    // (Reactions.tsx: `count !== null`), so the added verdict renders
    // the count 1 —
    await expect(fire.locator(".reaction-count")).toHaveText("1");

    // — and the toggle-off removes the span entirely (a zero-count
    // emoji carries no count text).
    await fire.click();
    await expect(fire).toHaveAttribute("aria-pressed", "false");
    await expect(fire.locator(".reaction-count")).toHaveCount(0);
  });
});

test.describe("report (§23: filing removes nothing)", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(TASK_URL!);
  });

  test("category + submit -> non-destructive confirmation, comment stays visible", async ({
    page,
  }) => {
    const content = marker("被举报评论");
    await page.getByLabel("评论内容", { exact: true }).fill(content);
    await page.getByRole("button", { name: "发布评论" }).click();
    const article = page.locator(".comment", { hasText: content });
    await expect(article).toBeVisible();

    await article.getByRole("button", { name: "举报", exact: true }).click();
    const dialog = page.locator("dialog.dialog");
    await expect(dialog).toBeVisible();
    // Hidden native radios (the identity-option pattern): click the label.
    await dialog.getByText("骚扰辱骂", { exact: true }).click();
    await dialog.getByRole("button", { name: "提交举报" }).click();

    await expect(dialog.getByRole("heading", { name: "举报已提交" })).toBeVisible();
    await expect(
      dialog.getByText(/该评论在审核期间保持可见/),
    ).toBeVisible();
    await dialog.getByRole("button", { name: "完成" }).click();
    await expect(dialog).not.toBeVisible();

    // Non-destructive: the reported comment is still right there.
    await expect(article).toBeVisible();
  });

  test("submitting without a category is rejected next to the field", async ({
    page,
  }) => {
    const article = page.locator(".comment").first();
    await article.getByRole("button", { name: "举报", exact: true }).click();
    const dialog = page.locator("dialog.dialog");
    await dialog.getByRole("button", { name: "提交举报" }).click();
    await expect(dialog.getByText("请选择举报类别")).toBeVisible();
    await dialog.getByRole("button", { name: "取消" }).click();
  });
});

test.describe("rating (§20: completer-only, aggregate public)", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(TASK_URL!);
  });

  test("the aggregate renders and a star tap produces a definite outcome", async ({
    page,
  }) => {
    const section = page.getByLabel("任务评分");
    await expect(section).toBeVisible();
    // Aggregate (暂无评分 while unrated) never blocks the picker. The
    // star radios are hidden native inputs (globals.css
    // `.star-option input`): act on the visible star LABEL (1-5 in
    // order; 4 星 = the fourth label).
    await section
      .getByRole("radiogroup", { name: "选择星级" })
      .locator("label")
      .nth(3)
      .click();
    // Either the viewer completed a claim (echo success) or the typed
    // completer-gate copy renders — silence would be the bug.
    await expect(
      section.getByRole("status").or(section.getByRole("alert")),
    ).toContainText(/已提交评分|完成任务后才能评价该任务/);
  });

  test("a non-completer sees the typed RATING_NOT_ELIGIBLE copy", async ({
    page,
  }) => {
    test.skip(
      NON_COMPLETER === undefined,
      "needs CQ_E2E_NON_COMPLETER_STUDENT (seeded student without a COMPLETED claim on the task).",
    );
    await loginAs(page, NON_COMPLETER!);
    await page.goto(TASK_URL!);
    const section = page.getByLabel("任务评分");
    await section
      .getByRole("radiogroup", { name: "选择星级" })
      .locator("label")
      .nth(4)
      .click();
    await expect(section.getByRole("alert")).toContainText(
      "完成任务后才能评价该任务",
    );
  });
});

test.describe("sort tabs + narrow viewport", () => {
  test.beforeEach(async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(TASK_URL!);
  });

  test("latest/hot tabs ride the URL (shareable state)", async ({ page }) => {
    await expect(page.getByRole("link", { name: "最新", exact: true })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await page.getByRole("link", { name: "热门", exact: true }).click();
    await expect(page).toHaveURL(/comments=hot/);
    await expect(page.getByRole("link", { name: "热门", exact: true })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test.describe("mobile 375x812", () => {
    test.use({ viewport: { width: 375, height: 812 } });

    test("composer, tabs, and row actions stay usable", async ({ page }) => {
      await expect(page.getByLabel("任务评论")).toBeVisible();
      await expect(page.getByLabel("评论内容", { exact: true })).toBeVisible();
      await expect(page.getByRole("link", { name: "热门", exact: true })).toBeVisible();
      const first = page.locator(".comment").first();
      if (await first.isVisible()) {
        await expect(first.getByRole("button", { name: "回复", exact: true })).toBeVisible();
      }
    });
  });
});

test.describe("admin identity reveal on the moderation surface (spec §21.4; plan 10 task 9)", () => {
  test.skip(
    REVEAL_ADMIN === undefined ||
      REVEAL_ADMIN_TOTP_SECRET === undefined ||
      MODERATION_TASK_PATH === undefined,
    "needs CQ_E2E_REVEAL_ADMIN + CQ_E2E_REVEAL_ADMIN_TOTP_SECRET + CQ_E2E_MODERATION_TASK_PATH (a seeded admin with an answerable TOTP and a task carrying an anonymous comment); Plan 10's fixture provides all three.",
  );

  test("the listing stays anonymous; the dialog demands a reason, then reveals and records the audit", async ({
    page,
  }) => {
    await loginAsStaff(page, REVEAL_ADMIN!, REVEAL_ADMIN_TOTP_SECRET!);
    await page.goto(MODERATION_TASK_PATH!);

    // The ADMIN normal listing is as anonymous as the teacher's: the
    // row shows 匿名用户 plus the pseudonymous key, never identity.
    const section = page.getByLabel("社区与举报");
    await expect(section).toBeVisible();
    const anonymousRow = section.locator(".moderation-item", { hasText: "匿名治理目标" });
    await expect(anonymousRow).toBeVisible();
    await expect(anonymousRow.locator(".comment-author")).toHaveText("匿名用户");
    await expect(anonymousRow.locator(".moderation-key")).toBeVisible();
    await expect(anonymousRow.getByText("学号")).toHaveCount(0);

    // Open the reveal dialog on that row.
    await anonymousRow.getByRole("button", { name: "揭示身份" }).click();
    const dialog = page.locator("dialog[aria-labelledby='reveal-identity-title']");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(/每次调用都会记入审计日志/)).toBeVisible();

    // Blank reason: the mirrored mandatory-reason rejection, no request.
    await dialog.getByRole("button", { name: "确认揭示身份" }).click();
    await expect(dialog.getByText("追溯原因必填（将记入审计日志）")).toBeVisible();

    // A real reason reveals the identity FROM THE API RESPONSE ONLY,
    // with the audit-recording copy beside it.
    await dialog.getByLabel("追溯原因").fill("e2e：治理投诉核查匿名评论作者身份");
    await dialog.getByRole("button", { name: "确认揭示身份" }).click();
    const revealed = dialog.getByRole("status");
    await expect(revealed).toContainText("身份已揭示（本次揭示已记入审计日志）");
    await expect(revealed.getByText("学号")).toBeVisible();
    await expect(revealed.locator(".mono")).not.toBeEmpty();

    // The moderation LISTING after the reveal is still anonymous — a
    // reveal is an audited access event, never a listing state change.
    await dialog.getByRole("button", { name: "关闭" }).click();
    await expect(dialog).not.toBeVisible();
    await expect(anonymousRow.locator(".comment-author")).toHaveText("匿名用户");
  });
});
