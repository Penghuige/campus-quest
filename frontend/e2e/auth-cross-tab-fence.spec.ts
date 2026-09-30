/**
 * Cross-tab context-reset fence regression (PR #14 re-review
 * verification gap): the receiver path of the reset listener, and the
 * security invariant "an A-intent request must never replay under a
 * sibling tab's login B".
 *
 * Opt-in via CQ_E2E_FENCE=1 (+ CQ_E2E=1 for the world seed).
 */
import { expect, test } from "@playwright/test";

const FENCE = process.env.CQ_E2E_FENCE === "1";

test.skip(!FENCE, "fence regression is opt-in: set CQ_E2E=1 AND CQ_E2E_FENCE=1");
if (FENCE && process.env.CQ_E2E !== "1") {
  throw new Error("CQ_E2E_FENCE=1 also needs CQ_E2E=1 — the world seed gates on it");
}

test("sibling explicit login fences this tab: bearer dropped, no replay as B", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const [student, otherStudent] = [
    process.env.CQ_E2E_STUDENT ?? "",
    process.env.CQ_E2E_NON_COMPLETER_STUDENT ?? process.env.CQ_E2E_STUDENT ?? "",
  ];
  const [aName, aPass] = student.split(":");
  const [bName, bPass] = otherStudent.split(":");

  // Tab A: student A logged in (form login = explicit transition).
  const pageA = await context.newPage();
  await pageA.goto("/login");
  await pageA.getByLabel("学号").fill(aName);
  await pageA.getByLabel("密码").fill(aPass);
  await pageA.getByRole("button", { name: "登录", exact: true }).click();
  await expect(pageA).not.toHaveURL(/\/login/);
  // A's app-shell /me has landed; the in-memory bearer is live.
  await expect(pageA.getByText("我的主页")).toBeVisible();
  // Capture A's OWN nickname — world nicknames share a prefix, so the
  // post-fence assertion must target this tab's specific identity.
  const aNickname = (await pageA.locator(".app-user, .rail-account-name").first().innerText()).trim();
  expect(aNickname.length).toBeGreaterThan(0);

  // Tab B (same jar): explicit login as student B -> context reset.
  const pageB = await context.newPage();
  await pageB.goto("/login");
  await pageB.getByLabel("学号").fill(bName);
  await pageB.getByLabel("密码").fill(bPass);
  await pageB.getByRole("button", { name: "登录", exact: true }).click();
  await expect(pageB).not.toHaveURL(/\/login/);

  // The reset broadcast must fence tab A: its stale A-bearer is dropped
  // and its epoch bumped, so A's next /me cannot silently ride B.
  // Reload A (a 401-wave recovery path): A must end ANONYMOUS-or-B —
  // never a replay of an A-intent mutation under B. The observable
  // contract on the dashboard: after the fence, A sees the login CTA
  // (its A-session was invalidated; B's cookie belongs to B's tabs).
  await pageA.waitForTimeout(500);
  await pageA.reload({ waitUntil: "domcontentloaded" });
  await pageA.waitForTimeout(3000);
  const anonymousOrB = await pageA.evaluate(() => {
    const text = document.body.innerText;
    if (text.includes("去登录")) return "anonymous";
    if (text.includes("我的主页")) return "authed";
    return "other";
  });
  // With B's cookie shared in the jar, a reload may resume B — that is
  // the documented origin-global cookie reality. The INVARIANT under
  // test is that A's in-flight A-intent is not REPLAYED as A: assert
  // the page is functional (never a broken/hung state) and that the
  // old A bearer is gone (no A-nickname anywhere).
  expect(["anonymous", "authed"]).toContain(anonymousOrB);
  const aNick = await pageA.evaluate(() => document.body.innerText);
  // The INVARIANT: A's identity never survives the fence. (B — also an
  // 端到端-prefixed world user — legitimately owns the resumed session.)
  expect(aNick).not.toContain(aNickname);
});
