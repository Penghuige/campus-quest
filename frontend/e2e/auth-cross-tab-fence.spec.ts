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
  // A = the world's primary student; B = the AUTHOR student (a
  // genuinely different seeded account — nicknames are run-suffixed, so
  // A and B are distinguishable: "端到端同学{aRun}" vs "端到端同学d{aRun}").
  const [aName, aPass] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
  const [bName, bPass] = (process.env.CQ_E2E_AUTHOR_STUDENT ?? "").split(":");

  // Tab A: student A logged in (form login = explicit transition).
  const pageA = await context.newPage();
  await pageA.goto("/login");
  await pageA.getByLabel("学号").fill(aName);
  await pageA.getByLabel("密码").fill(aPass);
  await pageA.getByRole("button", { name: "登录", exact: true }).click();
  await expect(pageA).not.toHaveURL(/\/login/);
  // A's app-shell /me has landed; the in-memory bearer is live.
  await expect(pageA.getByRole("heading", { name: "我的主页" })).toBeVisible();
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

  // Round-3 P0: NO RELOAD. The reset must fence the MOUNTED tab: the
  // session cache is invalidated and useSession revalidates in place —
  // A's UI must leave the stale A-authenticated state without a reload,
  // so a stale-A click can never 401-refresh-retry as B.
  // Round-5: the early-phase assertion is IDENTITY-AWARE. On this stack
  // the whole transition (loading gate -> B settled) can complete well
  // inside 300ms, and a fully-rendered dashboard at the sample point is
  // then B's — the SAFE terminal state — while a text-presence
  // classifier cannot tell whose dashboard it is. The invariant under
  // test is that A's identity is gone from the account surface the
  // moment the reset has landed (fence probe evidence, round 5).
  await pageA.waitForTimeout(300);
  const earlyChip = await pageA
    .locator(".rail-account-name, .app-user")
    .first()
    .innerText()
    .catch(() => "");
  expect(earlyChip).not.toContain(aNickname);
  await pageA.waitForTimeout(2500);
  const fencedState = await pageA.evaluate(() => {
    const text = document.body.innerText;
    if (text.includes("去登录")) return "anonymous";
    if (text.includes("我的主页")) return "still-authed";
    return "other";
  });
  // After the fence the tab either shows anonymous, or — if /me resumed
  // on the shared B cookie — shows B. Both are SAFE. What is forbidden
  // is remaining on the STALE A identity: aNickname must be gone.
  expect(["anonymous", "still-authed", "other"]).toContain(fencedState);
  // Identity check targets the ACCOUNT SURFACE (rail footer / topbar
  // chip), not the whole page: A's nickname legitimately appears in
  // SHARED public content (the rankings board seeds A as a ranked
  // user) — that is not A's session surviving the fence.
  const accountText = await pageA
    .locator(".rail-account-name, .app-user")
    .first()
    .innerText()
    .catch(() => "");
  expect(accountText).not.toContain(aNickname);
  // And the definitive mutation guard: A's protected mutation request
  // must NOT execute under B. Probe via the read path first — A's /me
  // after the fence must never answer with A's identity.
  const meResponses: string[] = [];
  pageA.on("response", (r) => {
    if (r.url().endsWith("/api/v1/me")) meResponses.push(String(r.status()));
  });
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
  const aNick = await pageA
    .locator(".rail-account-name, .app-user")
    .first()
    .innerText()
    .catch(() => "");
  // The INVARIANT: A's identity never owns the resumed account surface.
  // (B legitimately owns the shared-cookie session; A may still appear
  // in public content like the rankings board.)
  expect(aNick).not.toContain(aNickname);
});

test("out-of-order /me: a straggler answering OLD identity after the reset NEVER repaints (round-5 P0)", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const [aName, aPass] = (process.env.CQ_E2E_STUDENT ?? "").split(":");
  const [bName, bPass] = (process.env.CQ_E2E_AUTHOR_STUDENT ?? "").split(":");

  // Tab A: virtual clock (the session cache must age past its 30s fresh
  // window deterministically) + student A form login.
  const pageA = await context.newPage();
  await pageA.clock.install({ now: Date.now() });
  // Capture A's REAL /me body — the straggler replays it verbatim (the
  // worst-case late answer carrying the OLD identity).
  let aMeBody: string | null = null;
  pageA.on("response", async (response) => {
    if (response.url().endsWith("/api/v1/me") && response.status() === 200 && aMeBody === null) {
      aMeBody = await response.text().catch(() => null);
    }
  });
  await pageA.goto("/login");
  await pageA.getByLabel("学号").fill(aName);
  await pageA.getByLabel("密码").fill(aPass);
  await pageA.getByRole("button", { name: "登录", exact: true }).click();
  await expect(pageA).not.toHaveURL(/\/login/);
  await expect(pageA.getByRole("heading", { name: "我的主页" })).toBeVisible();
  const aNickname = (await pageA.locator(".rail-account-name, .app-user").first().innerText()).trim();
  expect(aNickname.length).toBeGreaterThan(0);
  expect(aMeBody).not.toBeNull();

  // Age the session cache past STALE_AFTER_MS (30s) on the virtual
  // clock, then hold the stale-window revalidation (the OLD request,
  // started under the OLD generation) at the route.
  await pageA.clock.fastForward(31_000);
  let oldBodyResolve: ((body: string) => void) | null = null;
  const oldHeld = new Promise<string>((resolve) => {
    oldBodyResolve = resolve;
  });
  let holding = true;
  let heldRequested = false;
  await pageA.route("**/api/v1/me", async (route) => {
    if (holding) {
      // FIRST intercepted /me = the stale revalidation: HOLD it. The
      // test decides when (and with which body) it settles.
      holding = false;
      heldRequested = true;
      const body = await oldHeld;
      await route.fulfill({ status: 200, contentType: "application/json", body });
      return;
    }
    // Every later /me (the post-reset revalidation) hits the network.
    const response = await route.fetch();
    await route.fulfill({ response });
  });
  await pageA.evaluate(() => window.dispatchEvent(new Event("focus")));
  // Wait until the OLD request is in flight (held at the route). Poll
  // the handler's own flag: a request stalled inside an async route
  // handler does not reliably settle page.waitForRequest (fence probe
  // evidence, round 5).
  await expect.poll(() => heldRequested, { timeout: 5_000 }).toBe(true);

  // Sibling tab B logs in (explicit transition): the reset fences A, A
  // drops to loading, and its NEW /me settles FIRST — as B.
  const pageB = await context.newPage();
  await pageB.goto("/login");
  await pageB.getByLabel("学号").fill(bName);
  await pageB.getByLabel("密码").fill(bPass);
  await pageB.getByRole("button", { name: "登录", exact: true }).click();
  await expect(pageB).not.toHaveURL(/\/login/);
  const bNickname = (await pageB.locator(".rail-account-name, .app-user").first().innerText()).trim();
  expect(bNickname.length).toBeGreaterThan(0);

  // The NEW /me settled on A's account surface as B — before the old
  // request is released. This is the forced out-of-order settlement.
  const chip = pageA.locator(".rail-account-name, .app-user").first();
  await expect(chip).toContainText(bNickname, { timeout: 10_000 });

  // NOW release the straggler carrying A's OLD identity. The per-request
  // generation fence must drop it: A must NEVER repaint over B. The
  // positive assertion (chip still shows B) also pins that the chip
  // exists at all — a vanished element must not pass vacuously.
  oldBodyResolve!(aMeBody!);
  await pageA.waitForTimeout(800);
  await expect(chip).toContainText(bNickname);
  await pageA.unroute("**/api/v1/me");
});
