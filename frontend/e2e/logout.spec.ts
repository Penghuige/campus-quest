/**
 * CampusQuest logout e2e — defects #11/#18 (QA 2026-10-03).
 *
 * #11 (致命): the student profile had NO logout entry at all — the
 * 个人信息 tab now carries an account-actions section (LogoutSection).
 * #18 (严重): the staff shells had none either — a teacher/admin could
 * not free the device for another account; both shells' topbars now
 * carry the shared StaffLogoutButton.
 *
 * The round-trip contract under test (the reviewer's prescription):
 * after 退出登录 the login surface is reachable, the revoked session
 * really is gone (/me answers 401 from the same browser context), and
 * ANOTHER account can log in on the freed device — the exact QA #18
 * complaint. The logout semantics themselves (epoch bump, cache
 * invalidation, cross-tab fence) are the session module's unit-tested
 * contract; here the browser drives the real button against the real
 * endpoint.
 *
 * Environment contract (defaults work against the orchestrated stack):
 * - CQ_E2E=1                 enable the suite (required);
 * - CQ_E2E_BASE_URL          frontend origin (default https://localhost:3000);
 * - CQ_E2E_STUDENT           the shared seeded student (required);
 * - CQ_E2E_AUTHOR_STUDENT    a SECOND seeded student — drives the
 *                            "another account can log in" leg;
 * - CQ_E2E_STAFF / CQ_E2E_STAFF_TOTP_SECRET — the browser staff login
 *   contract (fixtures.ts staffLogin);
 * - CQ_E2E_STAFF2 / CQ_E2E_STAFF2_TOTP_SECRET — the second staff
 *   account for the staff takeover leg.
 */
import { ensureStudentLogin, expect, staffLogin, test } from "./fixtures";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
const STUDENT = process.env.CQ_E2E_STUDENT;
const AUTHOR_STUDENT = process.env.CQ_E2E_AUTHOR_STUDENT;
const STAFF = process.env.CQ_E2E_STAFF;
const STAFF_TOTP_SECRET = process.env.CQ_E2E_STAFF_TOTP_SECRET;
const STAFF2 = process.env.CQ_E2E_STAFF2;
const STAFF2_TOTP_SECRET = process.env.CQ_E2E_STAFF2_TOTP_SECRET;

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* fixtures) to run this suite.");

test.describe("student logout (defect #11)", () => {
  const studentReady = STUDENT !== undefined && AUTHOR_STUDENT !== undefined;
  test.skip(
    !studentReady,
    "needs CQ_E2E_STUDENT plus CQ_E2E_AUTHOR_STUDENT (a second seeded student for the takeover leg); Plan 10's fixture provides both.",
  );

  test("profile logout revokes the session and frees the device for another account", async ({ page }) => {
    await ensureStudentLogin(page);
    await page.goto(`${BASE_URL}/profile?tab=info`);

    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/login`));

    // The revoked session is really gone: /me must answer 401 from
    // THIS context (page.request rides the same cookie jar).
    const me = await page.request.get(`${BASE_URL}/api/v1/me`);
    expect(me.status()).toBe(401);

    // QA #18's complaint, student side: another student can log in on
    // the freed device right away.
    const [username, password] = AUTHOR_STUDENT!.split(":");
    await page.getByLabel("学号").fill(username);
    await page.getByLabel("密码").fill(password);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/$`));
  });
});

test.describe("staff logout (defect #18)", () => {
  const staffReady =
    STAFF !== undefined &&
    STAFF_TOTP_SECRET !== undefined &&
    STAFF2 !== undefined &&
    STAFF2_TOTP_SECRET !== undefined;
  test.skip(
    !staffReady,
    "needs CQ_E2E_STAFF(+TOTP) and CQ_E2E_STAFF2(+TOTP) (two seeded staff accounts for the takeover leg); Plan 10's fixture provides both.",
  );

  test("teacher-shell logout revokes the session and frees the device for another staff account", async ({ page }) => {
    await staffLogin(page, STAFF!, STAFF_TOTP_SECRET!);
    // Any staff surface carries the shell topbar; the queue is the home.
    await page.goto(`${BASE_URL}/teacher/reviews`);
    await expect(page.getByRole("banner")).toBeVisible();

    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page).toHaveURL(new RegExp(`${BASE_URL}/staff/login`));

    const me = await page.request.get(`${BASE_URL}/api/v1/me`);
    expect(me.status()).toBe(401);

    // A SECOND staff account takes over the same device.
    await staffLogin(page, STAFF2!, STAFF2_TOTP_SECRET!);
    await expect(page).not.toHaveURL(/\/staff\/login/);
  });
});
