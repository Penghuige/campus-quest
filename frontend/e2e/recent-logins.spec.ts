/**
 * The recent-logins quick-switch e2e (batch 2, owner brief
 * 2026-10-20): the login page's remembered-account cards. ONE test,
 * one browser context (the ledger is per-context localStorage):
 *
 * - a completed login records its account — the teacher's email code
 *   login and the seeded student's password login each leave a card;
 * - the cards show the G11 masks + role badges (never the full
 *   address), newest first;
 * - clicking a card PREFILLS the identifier (the input the user
 *   themselves typed into — the remembered-username convention);
 * - 从本机移除 is a pure jar delete: the card vanishes and STAYS
 *   gone across a reload (no backend call exists to make).
 */
import { expect, test } from "@playwright/test";

import { recoverEmailCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;

test.skip(!(E2E_ENABLED && RUN), "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.");

test("cards record on login, prefill on click, and removal is device-local", async ({ page }) => {
  test.setTimeout(120_000);
  const [teacherEmail, teacherPassword] = (process.env.CQ_E2E_STAFF ?? "").split(":");

  // The TEACHER rides the email-code flow (the six chains' helper
  // shape, inline): request → recover → verify → workspace.
  await page.goto(`${BASE_URL}/login`);
  const field = page.getByLabel("账号", { exact: true });
  await field.fill(teacherEmail);
  await page.getByRole("button", { name: "获取验证码", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page.getByLabel("验证码", { exact: true }).fill(recoverEmailCode(teacherEmail));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));

  // Logout: the card for the teacher session greets the login page.
  // The mask keeps the address's FIRST local char (e2e-teacher… → e***).
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));
  const teacherMask = `${teacherEmail[0]}***@school.edu`;
  const teacherCard = page.locator(".login-recent-card", { hasText: teacherMask });
  await expect(teacherCard).toBeVisible();
  await expect(teacherCard.getByText("教师")).toBeVisible();

  // The SEEDED STUDENT's password login adds the second card (newest
  // first: the student rides on top).
  const [studentNumber, studentPassword] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(studentNumber);
  await page.getByLabel("密码").fill(studentPassword);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  // The student's logout rides the profile's INFO tab (the growth tab
  // is the default; defect #11's section lives under ?tab=info).
  await page.goto(`${BASE_URL}/profile?tab=info`);
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  const cards = page.locator(".login-recent-card");
  await expect(cards).toHaveCount(2);
  const studentCard = cards.first();
  await expect(studentCard.getByText("学生")).toBeVisible();
  await expect(studentCard.getByText(new RegExp(`^\\d{2}\\*\\*\\*\\*\\d{2}$`))).toBeVisible();

  // The click PREFILLS the identifier with the teacher's full address
  // (the field the user typed into — G11 lives on the CARD, not the input).
  await teacherCard.getByRole("button", { name: /切换到/ }).click();
  await expect(page.getByLabel("账号", { exact: true })).toHaveValue(teacherEmail);

  // 从本机移除: the student's card leaves, and stays gone on reload.
  await studentCard.getByRole("button", { name: /从本机移除/ }).click();
  await expect(page.locator(".login-recent-card")).toHaveCount(1);
  await page.reload();
  await expect(page.locator(".login-recent-card")).toHaveCount(1);
  await expect(teacherCard).toBeVisible();
});
