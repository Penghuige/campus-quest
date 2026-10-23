/**
 * The shells' 切换账号 entry (owner batch 2026-10-23; popover batch —
 * owner feedback same day): the click opens an IN-PLACE account menu
 * of the device's recent logins (the current account excluded — the
 * jar's head); a pick hands off through sessionStorage and /login
 * auto-drives the picked card. With NO other account on the device,
 * the click keeps the v1 direct hop.
 *
 * Chain A (student, single-account device): password login → 切换账号
 * = the EMPTY-menu fallback hop → /login + the student card → its
 * click prefills and hands the password form back (a student-number
 * card has no code stream — the format band refuses it).
 * Chain B (teacher, single-account device): password login WITH the
 * remember checkbox (arms cq_staff_device; the logout contract never
 * clears it) → 切换账号 = the fallback hop → /login + the email card →
 * its click lands back in the workspace in ONE step, zero code
 * requests.
 * Chain C (the menu): teacher remembered login → 退出登录 → student
 * login (the jar now holds student + teacher, student at the head) →
 * 切换账号 opens the POPOVER in place (URL unmoved; the teacher card
 * listed, the student's own card NOT) → picking the teacher card →
 * /login auto-drives the remembered branch → the teacher cockpit, zero
 * code requests (the switch loop closed through the menu).
 */
import { expect, test } from "@playwright/test";

import { loginThroughUi, parseSeededAccount } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;
const STAFF = process.env.CQ_E2E_STAFF;

test.skip(
  !(E2E_ENABLED && RUN && STAFF),
  "set CQ_E2E=1 (and the CQ_E2E_* vars) to run this suite.",
);

test("student: 切换账号 reaches the login page and its card returns to the form", async ({
  page,
}) => {
  const account = parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT");
  await loginThroughUi(page, account);
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));

  // The topbar entry: this device holds ONLY the student's login (the
  // jar's single row IS the current account) — the menu is empty, so
  // the click keeps the v1 direct hop (the same logout contract:
  // session revoked, cookies cleared).
  await page.getByRole("button", { name: "切换账号" }).click();
  await expect(page.locator(".switch-menu")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // The student's card is waiting (the ledger recorded this login); its
  // click prefills and hands the PASSWORD form back — a student number
  // has no code stream.
  const mask = `${account.username.slice(0, 2)}****${account.username.slice(-2)}`;
  const card = page.locator(".login-recent-card", { hasText: mask });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: /切换到/ }).click();
  await expect(page.getByLabel("账号", { exact: true })).toHaveValue(account.username);
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
});

test("teacher: 切换账号 + a remembered card closes the loop in one click", async ({ page }) => {
  const [email, password] = STAFF!.split(":");

  // The password leg WITH the remember checkbox: arms cq_staff_device
  // (a TEACHER's password is the whole auth — no totp leg).
  await page.goto(`${BASE_URL}/login`);
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(email);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));

  // The teacher shell's topbar entry (next to 退出登录, which stays).
  // This device holds only the teacher's login — the empty-menu
  // fallback hop, straight to /login.
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  await page.getByRole("button", { name: "切换账号" }).click();
  await expect(page.locator(".switch-menu")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // Logout never clears cq_staff_device — the remembered card rides it
  // back into the workspace in ONE click, zero code requests.
  const mask = `${email[0]}***@school.edu`;
  const card = page.locator(".login-recent-card", { hasText: mask });
  await expect(card).toBeVisible();
  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });
  await card.getByRole("button", { name: /切换到/ }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
  expect(requestCount, "the remembered switch must not request a code").toBe(0);
});

test("the menu: pick another device account and /login auto-drives its card", async ({
  page,
}) => {
  const [email, staffPassword] = STAFF!.split(":");
  const account = parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT");
  const teacherMask = `${email[0]}***@school.edu`;
  const studentMask = `${account.username.slice(0, 2)}****${account.username.slice(-2)}`;

  // Seed the jar with BOTH accounts: the teacher first (WITH the
  // remember checkbox — the pick's fast branch needs cq_staff_device),
  // then leave via 退出登录 (the plain logout; the jar survives it).
  await page.goto(`${BASE_URL}/login`);
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(email);
  await page.getByLabel("密码").fill(staffPassword);
  await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // The student login lands the jar's head on the student (the current
  // account); the teacher rides right behind it.
  await loginThroughUi(page, account);
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));

  // 切换账号 opens the popover IN PLACE: the URL does not move, the
  // teacher's card is offered, and the student's OWN card (the head)
  // is not.
  await page.getByRole("button", { name: "切换账号" }).click();
  const menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  await expect(menu.getByText(teacherMask)).toBeVisible();
  await expect(menu.getByText("使用其他账号")).toBeVisible();
  await expect(menu.getByText(studentMask)).toHaveCount(0);

  // The pick hands off through the logout hop; /login auto-drives the
  // teacher's remembered card — the cockpit, zero code requests.
  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });
  await menu.getByRole("button", { name: `切换到 ${teacherMask}` }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
  expect(requestCount, "the menu pick's remembered switch must not request a code").toBe(0);
});
