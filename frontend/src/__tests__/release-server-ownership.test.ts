/**
 * Release-mode server ownership proof (PR #6 E6 parallel review P1):
 * a release run must never silently adopt whatever process already
 * listens on the orchestrated ports — another checkout's uvicorn (or
 * another app entirely) would turn the gate green against the WRONG
 * artifact. The proof reads the committed playwright.config.ts source
 * and evaluates its reuseExistingServer expression under both env
 * shapes: absent/other => false (release, fail closed); "1" => true
 * (explicit development opt-in).
 *
 * CQ_E2E-skipped: this is a config contract, not a browser flow.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const configPath = fileURLToPath(new URL("../../playwright.config.ts", import.meta.url));
const source = readFileSync(configPath, "utf8");

/**Extract every reuseExistingServer assignment's enabling condition.*/
function conditions(): string[] {
  const matches = source.matchAll(/reuseExistingServer:\s*\n?\s*([^,\n]+)/g);
  return [...matches].map((match) => match[1].trim());
}

function evaluate(condition: string, envValue: string | undefined): boolean {
  // `process` is shadowed INSIDE the evaluated scope (the outer test
  // file also has a global `process`, and a bare redeclaration throws).
  const fn = new Function(
    `"use strict"; const env = { CQ_E2E_REUSE_EXISTING: ${JSON.stringify(envValue)} };
     const process = { env };
     return (${condition});`,
  );
  return fn();
}

test("the config pins reuse to the explicit env flag (both servers)", () => {
  const found = conditions();
  assert.equal(found.length, 2, "both webServer entries must pin reuse");
  for (const condition of found) {
    assert.match(
      condition,
      /CQ_E2E_REUSE_EXISTING/,
      "reuse must be governed by the explicit opt-in flag",
    );
  }
});

test("release mode (no flag) fails closed — never adopts a listener", () => {
  for (const condition of conditions()) {
    assert.equal(evaluate(condition, undefined), false);
    assert.equal(evaluate(condition, "0"), false);
  }
});

test("development can explicitly opt into reuse", () => {
  for (const condition of conditions()) {
    assert.equal(evaluate(condition, "1"), true);
  }
});
