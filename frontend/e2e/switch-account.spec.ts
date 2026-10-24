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
 * listed with the 免登录 chip, the student's own card NOT) → picking
 * the teacher card → /login auto-drives the TARGETED slot verify →
 * the teacher cockpit, zero code requests.
 * Chain D (multi-slot, the owner scenario): teacher A remembers →
 * 退出登录 → student B REMEMBERS TOO (B's remember must not evict A's
 * slot — the retired single-name cookie did, the owner-reported
 * 切换账号仍要验证码 bug) → switch back to A through the menu → A's
 * card carries 免登录 → the targeted slot verify lands the teacher
 * cockpit, zero code requests.
 * Chain E (the mask collision, slot-matching contract 2026-10-24):
 * the world's TWO teachers mask alike (`e***@school.edu` ×2) — each
 * remembers, then the switch walks BOTH ways and each pick lands ITS
 * OWN account (pinned by the topbar nickname). The retired mask
 * matching routed by first-hit and landed the wrong one; the card's
 * minted device_slot cannot collide.
 * Chain F (the student-number gain): a student logs in BY NUMBER with
 * remember — the minted slot lands on the number card (nothing
 * derivable from the number itself) — and that card direct-enters.
 * Chain G (mint-only retention): an account with a slot re-logs in
 * WITHOUT the checkbox — the response omits device_slot, the card
 * keeps its older slot, and the pick still direct-enters.
 */
import { expect, test } from "@playwright/test";

import { loginThroughUi, parseSeededAccount, recoverMobileCode } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const RUN = process.env.CQ_E2E_RUN;
const STAFF = process.env.CQ_E2E_STAFF;
const STAFF2 = process.env.CQ_E2E_STAFF2;

test.skip(
  !(E2E_ENABLED && RUN && STAFF && STAFF2),
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
  // teacher's card is offered (with the 免登录 chip — its slot is in
  // the probe array), and the student's OWN card (the head) is not.
  await page.getByRole("button", { name: "切换账号" }).click();
  const menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  const teacherCard = menu.locator(".login-recent-card", { hasText: teacherMask });
  await expect(teacherCard).toBeVisible();
  await expect(teacherCard.getByText("免登录")).toBeVisible();
  await expect(menu.getByText("使用其他账号")).toBeVisible();
  await expect(menu.getByText(studentMask)).toHaveCount(0);

  // The pick hands off through the logout hop; /login auto-drives the
  // teacher's remembered card through the TARGETED slot verify — the
  // cockpit, zero code requests.
  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });
  await teacherCard.getByRole("button", { name: `切换到 ${teacherMask}` }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
  expect(requestCount, "the menu pick's remembered switch must not request a code").toBe(0);
});

test("multi-slot: A remembers, B remembers, switching back to A needs no code (the owner scenario)", async ({
  page,
}) => {
  const [email, staffPassword] = STAFF!.split(":");
  const account = parseSeededAccount(process.env.CQ_E2E_STUDENT, "CQ_E2E_STUDENT");
  const teacherMask = `${email[0]}***@school.edu`;

  // Account A (teacher) remembers — its slot cookie is armed.
  await page.goto(`${BASE_URL}/login`);
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(email);
  await page.getByLabel("密码").fill(staffPassword);
  await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // Account B (student) remembers too — the multi-slot batch: B's
  // remember must NOT evict A's slot (the retired single-name cookie
  // did; the owner-reported 切换账号仍要验证码 bug).
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(account.username);
  await page.getByLabel("密码").fill(account.password);
  await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));

  // Switching back to A through the menu: A's card carries the 免登录
  // chip (its slot survived B's remember), and the pick lands in the
  // teacher cockpit with ZERO code requests — the owner scenario.
  await page.getByRole("button", { name: "切换账号" }).click();
  const menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  const teacherCard = menu.locator(".login-recent-card", { hasText: teacherMask });
  await expect(teacherCard).toBeVisible();
  await expect(teacherCard.getByText("免登录")).toBeVisible();
  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });
  await teacherCard.getByRole("button", { name: `切换到 ${teacherMask}` }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
  expect(requestCount, "switching back to a remembered account must not request a code").toBe(0);
});

test("mask collision: two same-mask remembered accounts each land THEIR account", async ({
  page,
}) => {
  const [emailA, passwordA] = STAFF!.split(":");
  const [emailB, passwordB] = STAFF2!.split(":");
  // Both staff identifiers mask alike: first char + *** + the same
  // domain — the collision the retired mask matching routed wrong.
  // The cards carry no nickname (recordLogin records none for a
  // password login), so the same-mask pair is told apart by the
  // LANDING, not the card: each menu holds exactly one card (the
  // current account rides excluded) and the topbar nickname is the
  // verdict.
  const sharedMask = `${emailA[0]}***@${emailA.split("@")[1]}`;

  async function rememberedStaffLogin(email: string, password: string) {
    await page.goto(`${BASE_URL}/login`);
    await page.getByRole("button", { name: "使用密码登录" }).click();
    await page.getByLabel("账号", { exact: true }).fill(email);
    await page.getByLabel("密码").fill(password);
    await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  }

  await rememberedStaffLogin(emailA, passwordA);
  const nickA = await page.locator(".app-user-name").innerText();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));
  await rememberedStaffLogin(emailB, passwordB);
  const nickB = await page.locator(".app-user-name").innerText();
  expect(nickA, "the two teachers must be distinguishable").not.toEqual(nickB);

  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });

  // From B's session the menu offers A's card under the SHARED mask.
  await page.getByRole("button", { name: "切换账号" }).click();
  let menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  const cardA = menu.locator(".login-recent-card").first();
  await expect(cardA.getByText(sharedMask)).toBeVisible();
  await expect(cardA.getByText("免登录")).toBeVisible();

  // Pick A → A's OWN slot → A's cockpit under A's nickname (the
  // retired mask matching landed the most-recent slot here — B).
  await cardA.getByRole("button", { name: /切换到/ }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
  await expect(page.locator(".app-user-name")).toHaveText(nickA);
  expect(requestCount, "the collision pick must not request a code").toBe(0);

  // And the same-shape hop back to B — both ways land their own.
  await page.getByRole("button", { name: "切换账号" }).click();
  menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  const cardB = menu.locator(".login-recent-card").first();
  await expect(cardB.getByText(sharedMask)).toBeVisible();
  await cardB.getByRole("button", { name: /切换到/ }).click();
  await expect(page.locator(".app-user-name")).toHaveText(nickB, { timeout: 10_000 });
  expect(requestCount, "the return pick must not request a code either").toBe(0);
});

test("a code-leg mint lands on the student's card — it direct-enters", async ({
  page,
}) => {
  const [email, staffPassword] = STAFF!.split(":");
  // CQ_E2E_AUTHOR_SECRETS = nickname,username,phone,email,userId — the
  // seeded author student's E.164 phone (the code leg's identifier).
  const [, , phoneE164] = (process.env.CQ_E2E_AUTHOR_SECRETS ?? "").split(",");
  const phoneMask = `+86****${phoneE164.slice(-4)}`;

  // Teacher A remembers (the password leg's craft).
  await page.goto(`${BASE_URL}/login`);
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(email);
  await page.getByLabel("密码").fill(staffPassword);
  await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

  // The author student logs in BY PHONE through the CODE leg with the
  // remember box checked — the student mint rides THIS leg (the
  // student password leg mints nothing, interfaces.md), and the
  // minted slot lands on the student's PHONE card. The code band's
  // identifier takes the BARE national number (the +86 form fails the
  // client band); code recovery rides the E.164.
  await page.getByLabel("账号", { exact: true }).fill(phoneE164.replace(/^\+86/, ""));
  await page.getByRole("button", { name: "获取验证码", exact: true }).click();
  await expect(page.getByRole("button", { name: /重新发送（\d+ 秒）/ })).toBeVisible();
  await page
    .getByLabel("验证码", { exact: true })
    .fill(recoverMobileCode(phoneE164));
  await page.getByRole("checkbox", { name: /30 天内免登录/ }).check();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));

  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });

  // The student's menu offers the teacher; the pick lands the teacher
  // cockpit (zero code) and puts the session where the PHONE card is.
  await page.getByRole("button", { name: "切换账号" }).click();
  let menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  const teacherCard = menu.locator(".login-recent-card").first();
  await expect(teacherCard.getByText("免登录")).toBeVisible();
  await teacherCard.getByRole("button", { name: /切换到/ }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });

  // Now the student's PHONE card: chip on, direct entry into the
  // student dashboard, zero code — the code leg's minted slot.
  await page.getByRole("button", { name: "切换账号" }).click();
  menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  const studentCard = menu.locator(".login-recent-card").first();
  await expect(studentCard.getByText(phoneMask)).toBeVisible();
  await expect(studentCard.getByText("免登录")).toBeVisible();
  await studentCard.getByRole("button", { name: /切换到/ }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`), { timeout: 10_000 });
  expect(requestCount, "neither pick may request a code").toBe(0);
});

test("an unminted re-login keeps the older slot — the card still direct-enters", async ({
  page,
}) => {
  const [emailA, passwordA] = STAFF!.split(":");
  const [emailB, passwordB] = STAFF2!.split(":");

  async function staffLogin(email: string, password: string, remember: boolean) {
    await page.goto(`${BASE_URL}/login`);
    await page.getByRole("button", { name: "使用密码登录" }).click();
    await page.getByLabel("账号", { exact: true }).fill(email);
    await page.getByLabel("密码").fill(password);
    if (remember) {
      await page.getByRole("checkbox", { name: /记住此设备 30 天/ }).check();
    }
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));
    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));
  }

  // A mints (remember), B mints (remember)…
  await staffLogin(emailA, passwordA, true);
  await staffLogin(emailB, passwordB, true);
  // …then A re-logs in WITHOUT the checkbox: the response omits
  // device_slot, and A's card must KEEP its older, still-live slot
  // (the mint-only retention rule — revoking is revoke_all's job).
  await staffLogin(emailA, passwordA, false);

  // B takes the session (unminted login; the helper left /login open).
  await page.getByRole("button", { name: "使用密码登录" }).click();
  await page.getByLabel("账号", { exact: true }).fill(emailB);
  await page.getByLabel("密码").fill(passwordB);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`));

  let requestCount = 0;
  page.on("request", (request) => {
    if (request.url().includes("/auth/challenge/request")) {
      requestCount += 1;
    }
  });
  await page.getByRole("button", { name: "切换账号" }).click();
  const menu = page.locator(".switch-menu");
  await expect(menu).toBeVisible();
  const cardA = menu.locator(".login-recent-card").first();
  await expect(cardA.getByText("免登录")).toBeVisible();
  await cardA.getByRole("button", { name: /切换到/ }).click();
  await expect(page).toHaveURL(new RegExp(`${BASE_URL}/teacher/`), { timeout: 10_000 });
  expect(requestCount, "the retained slot's pick must not request a code").toBe(0);
});
