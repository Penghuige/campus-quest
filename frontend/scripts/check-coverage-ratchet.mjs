#!/usr/bin/env node
/**
 * P1 visibility: the unit-suite coverage ratchet — runs the unit suite
 * with Node's NATIVE --experimental-test-coverage (zero new deps; the
 * text report is parsed for the "all files" line), then fails if any
 * metric fell below the floor recorded in coverage-ratchet.json.
 *
 * Semantic boundary (see coverage-ratchet.json): unit suite only —
 * lib/view/pure layers; e2e is out of scope by design.
 *
 * Usage (from frontend/):
 *   npm run coverage:ratchet   # gate: run + compare (CI + release-gate)
 *   npm run coverage:update    # re-record floors (intentional change)
 */
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const ratchetPath = join(here, "..", "coverage-ratchet.json");

function runCoverage() {
  const out = execFileSync(
    "npx",
    [
      "tsx",
      "--test",
      "--experimental-test-coverage",
      "src/__tests__/*.test.ts",
    ],
    { cwd: join(here, ".."), encoding: "utf8", shell: true },
  );
  // The summary row (space-padded columns):
  // # all files  |  95.22 |  91.22 |  90.19 |
  const m = out.match(/^#\s+all files\s+\|\s+([\d.]+)\s+\|\s+([\d.]+)\s+\|\s+([\d.]+)/m);
  if (!m) {
    console.error("[coverage] FAIL: could not parse the native coverage summary");
    console.error(out.split("\n").slice(-8).join("\n"));
    process.exit(1);
  }
  return {
    lines: Number(m[1]),
    branches: Number(m[2]),
    functions: Number(m[3]),
  };
}

const current = runCoverage();

if (process.argv.includes("--update")) {
  writeFileSync(
    ratchetPath,
    JSON.stringify(
      { ...current, _recorded: new Date().toISOString().slice(0, 10) },
      null,
      2,
    ).replace(
      "{",
      `{\n  "_comment": ${JSON.stringify(
        JSON.parse(readFileSync(ratchetPath, "utf8"))._comment,
      )},\n  "_command": "npx tsx --test --experimental-test-coverage src/__tests__/*.test.ts",`,
      1,
    ) + "\n",
  );
  console.log(
    `[coverage] updated floors: lines ${current.lines} / branches ${current.branches} / functions ${current.functions}`,
  );
  process.exit(0);
}

const floors = JSON.parse(readFileSync(ratchetPath, "utf8"));
// Native coverage jitters ±0.01 between runs (file iteration order);
// the epsilon keeps the gate flake-free while still catching any real
// regression (which is orders of magnitude larger).
const EPSILON = 0.05;
const regressions = [];
for (const metric of ["lines", "branches", "functions"]) {
  if (current[metric] < floors[metric] - EPSILON) {
    regressions.push(
      `${metric}: ${current[metric].toFixed(2)} < floor ${floors[metric].toFixed(2)}`,
    );
  }
}
if (regressions.length > 0) {
  console.error("[coverage] FAIL: ratchet regression(s):");
  for (const r of regressions) console.error(`  ${r}`);
  console.error(
    "  If the decrease is intentional, `npm run coverage:update` in the same PR with the reason.",
  );
  process.exit(1);
}
console.log(
  `[coverage] OK: lines ${current.lines} / branches ${current.branches} / functions ${current.functions} (floors ${floors.lines}/${floors.branches}/${floors.functions})`,
);
