/**
 * P3-A: axe-core accessibility gate — rides the SAME 11-shot page
 * collection as the pixel suite (imports SHOTS/authenticate from
 * shot-surfaces.ts — one source of truth for the surfaces).
 *
 * Policy (owner-approved): NEW violations are zero-tolerance; EXISTING
 * ones are baselined in e2e/a11y-baseline.ts with a reason each —
 * fix one, delete its entry. The gate is a BOTH-DIRECTION ratchet:
 *   - a finding with no baseline entry  -> FAIL (new violation);
 *   - a baseline entry with no finding  -> FAIL (stale entry — the fix
 *     landed, delete it; a rotting baseline hides nothing).
 *
 * A `dev-gallery` note: it is a design specimen surface (NODE_ENV-gated
 * route); its violations still count — the gallery documents the
 * primitives, so it obeys the same accessibility contract.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "./fixtures";

import { authenticate, SHOTS } from "./shot-surfaces";
import { A11Y_BASELINE } from "./a11y-baseline";

const E2E_ENABLED = process.env.CQ_E2E === "1";
const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";

test.skip(!E2E_ENABLED, "set CQ_E2E=1 (and the CQ_E2E_* env) to run this suite.");

/** Stable fingerprint of a violation: rule + impact + the target's own
 * selector segment with volatile parts (nth-child indexes, dynamic ids,
 * varying ancestor specificity) normalized away — axe emits different
 * ancestor chains for the same element between runs, so only the final
 * compound matters. */
function fingerprint(violation: {
  id: string;
  impact?: string | null;
  nodes: { target: (string | undefined)[] }[];
}): string[] {
  return violation.nodes.map((node) => {
    const selector = (node.target?.[0] ?? "")
      .replace(/:nth-child\(\d+\)/g, "")
      .replace(/#[\w-]{8,}/g, "#dynamic");
    const ownSegment = selector.split(">").map((s) => s.trim()).filter(Boolean).pop() ?? selector;
    return `${violation.id}(${violation.impact ?? "?"})@${ownSegment}`;
  });
}

for (const shot of SHOTS) {
  test(`axe: ${shot.name}`, async ({ page }) => {
    test.skip(
      shot.envPath !== undefined && process.env[shot.envPath] === undefined,
      `world did not export ${shot.envPath} — same conditional discipline as the pixel suite`,
    );
    await authenticate(page, shot.auth);
    await page.goto(`${BASE_URL}${shot.envPath !== undefined ? process.env[shot.envPath] : shot.path}`, {
      waitUntil: "domcontentloaded",
    });
    await expect(page.locator(".page-head, main").first()).toBeVisible({
      timeout: 15_000,
    });
    // Match the pixel harness's settle beat so lazy sections render.
    await page.waitForTimeout(800);

    const results = await new AxeBuilder({ page }).analyze();
    const found = new Set<string>();
    for (const violation of results.violations) {
      for (const fp of fingerprint(violation)) found.add(fp);
    }
    const foundList = [...found];

    const baselined = new Set(
      (A11Y_BASELINE[shot.name] ?? []).map((e) => e.fingerprint),
    );
    const fresh = foundList.filter((f) => !baselined.has(f));
    const stale = [...baselined].filter((f) => !found.has(f));

    if (fresh.length > 0) {
      console.error(`[axe] NEW violations on ${shot.name}:`);
      for (const f of fresh) console.error(`  + ${f}`);
    }
    if (stale.length > 0) {
      console.error(`[axe] STALE baseline entries for ${shot.name} (fix landed — delete them):`);
      for (const f of stale) console.error(`  - ${f}`);
    }
    for (const entry of A11Y_BASELINE[shot.name] ?? []) {
      if (found.has(entry.fingerprint)) {
        console.log(`[axe]   baselined (${shot.name}): ${entry.fingerprint} — ${entry.reason.split(";")[0]}.`);
      }
    }
    expect(
      fresh.length + stale.length,
      `${shot.name}: axe ratchet — 0 new violations, 0 stale baseline entries (found ${foundList.length}, baseline ${baselined.size})`,
    ).toBe(0);
  });
}
