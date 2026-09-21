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
 *
 * PRIVACY PIN under test (spec §21.4/§40): an anonymous comment is
 * 匿名用户 on student surfaces — the author's nickname, student number,
 * phone, email, and user id appear NEITHER in rendered text NOR in the
 * DOM source.
 */
import { expect, test } from "@playwright/test";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const LOGIN_URL = process.env.CQ_E2E_LOGIN_URL ?? `${BASE_URL}/login`;
const TASK_URL = process.env.CQ_E2E_TASK_URL;
const STUDENT = process.env.CQ_E2E_STUDENT; // "20240002:correct-horse"
const AUTHOR_STUDENT = process.env.CQ_E2E_AUTHOR_STUDENT;
const AUTHOR_SECRETS = (process.env.CQ_E2E_AUTHOR_SECRETS ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter((value) => value.length > 0);
const NON_COMPLETER = process.env.CQ_E2E_NON_COMPLETER_STUDENT;

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

    // Context A: the author posts ONE anonymous comment.
    const authorContext = await browser.newContext();
    const authorPage = await authorContext.newPage();
    await loginAs(authorPage, AUTHOR_STUDENT!);
    await authorPage.goto(TASK_URL!);
    await authorPage
      .getByRole("radiogroup", { name: "发布身份" })
      .getByLabel("匿名", { exact: true })
      .check();
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
    await loginAs(viewerPage, STUDENT!);
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
    await loginAs(page, STUDENT!);
    await page.goto(TASK_URL!);
  });

  test("defaults to 公开昵称 with a clear preview; anonymous is a visible opt-in", async ({
    page,
  }) => {
    const identity = page.getByRole("radiogroup", { name: "发布身份" });
    await expect(identity.getByLabel("公开昵称")).toBeChecked();
    await expect(page.getByText(/将以公开昵称/)).toBeVisible();

    await identity.getByLabel("匿名", { exact: true }).check();
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
    await loginAs(page, STUDENT!);
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
    await loginAs(page, STUDENT!);
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
    await expect(fire.getByText("1")).toBeVisible();

    await fire.click();
    await expect(fire).toHaveAttribute("aria-pressed", "false");
    await expect(fire.getByText("0")).toBeVisible();
  });
});

test.describe("report (§23: filing removes nothing)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, STUDENT!);
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
    await dialog.getByLabel("骚扰辱骂").check();
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
    await loginAs(page, STUDENT!);
    await page.goto(TASK_URL!);
  });

  test("the aggregate renders and a star tap produces a definite outcome", async ({
    page,
  }) => {
    const section = page.getByLabel("任务评分");
    await expect(section).toBeVisible();
    // Aggregate (暂无评分 while unrated) never blocks the picker.
    await section.getByRole("radio", { name: "4 星" }).check();
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
    await section.getByRole("radio", { name: "5 星" }).check();
    await expect(section.getByRole("alert")).toContainText(
      "完成任务后才能评价该任务",
    );
  });
});

test.describe("sort tabs + narrow viewport", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, STUDENT!);
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
