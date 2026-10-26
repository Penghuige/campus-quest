/** Batch-2 pixel contract: the campus landing as batch 2 ships it, and
 *  the empty project-draft workspace. The upstream R1 shots pinned its
 *  own /logout page and operations rail — neither exists in this merge,
 *  so those scenarios are gone with their baselines. */
import AxeBuilder from "@axe-core/playwright";
import { BASE_URL, ensureStudentLogin, expect, test } from "./fixtures";

test.skip(process.env.CQ_E2E !== "1" || process.env.CQ_VISUAL !== "1" || process.env.CQ_E2E_FIXED_LABELS !== "1", "CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1 pins Linux fonts and disposable labels");

test("R2 navigation visual: campus landing and the empty project workspace", async ({ page }) => {
  test.setTimeout(90_000);
  await ensureStudentLogin(page);
  const pinFonts = async () => { await page.addStyleTag({ content: ':root { --font-sans: "Noto Sans CJK SC", sans-serif; } .mono { font-family: "Liberation Mono", monospace !important; } *, *::before, *::after { animation: none !important; transition: none !important; }' }); };
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${BASE_URL}/innovation`);
  await expect(page.getByRole("heading", { name: "创新创业", exact: true })).toBeVisible();
  await pinFonts();
  await expect(page).toHaveScreenshot("innovation-campus-landing.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.goto(`${BASE_URL}/profile/project-drafts`);
  await expect(page.getByRole("button", { name: "新建项目草稿", exact: true })).toBeVisible();
  await pinFonts();
  await expect(page).toHaveScreenshot("innovation-project-drafts.png", { fullPage: true, maxDiffPixelRatio: 0.01 });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});
