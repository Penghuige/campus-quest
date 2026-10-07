#!/usr/bin/env node
/**
 * Release-gate unexpected-skip assertion — P1 automation (v2).
 *
 * v1 (PR #6 → #19) watched a hard-coded argv spec list; v2 watches
 * EVERY e2e/*.spec.ts by default: a spec absent from the report, or any
 * skipped test not covered by e2e/noskip-exemptions.mjs (each entry
 * names its gate + reason), fails the gate. Every accepted exemption is
 * LOGGED — hidden skips are the failure mode this gate exists for.
 *
 * Usage (from frontend/):   node scripts/assert-e2e-no-skips.mjs
 * (No arguments; the exemption file is the only override surface.)
 */
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { NOSKIP_EXEMPTIONS } from "../e2e/noskip-exemptions.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const reportPath = join(here, "..", "test-results", "report.json");
const e2eDir = join(here, "..", "e2e");

/** Every spec file on disk (zz-* scratch specs excluded by convention). */
const specFiles = readdirSync(e2eDir)
  .filter((name) => name.endsWith(".spec.ts") && !name.startsWith("zz-"))
  .sort();

let report;
try {
  report = JSON.parse(readFileSync(reportPath, "utf8"));
} catch (error) {
  console.error(
    `[assert-e2e-no-skips] cannot read the Playwright JSON report at ${reportPath}: ${error}`,
  );
  process.exit(1);
}

/** Top-level suites carry the file name; describes nest below. */
function specByName(file) {
  return (
    report.suites?.find(
      (suite) => suite.file !== undefined && suite.file.endsWith(file),
    ) ?? null
  );
}

/** Flatten (describe-title, test-title, worstStatus) per test entry.
 * Report shape: suite.specs[] are TEST entries; each carries tests[]
 * (its runs/parameterizations), each run with results[].status. The
 * worst status wins (a skip hides behind nothing — v1 semantics). */
function allTests(spec) {
  const out = [];
  const walk = (suite, describe) => {
    for (const child of suite.suites ?? []) {
      walk(child, `${describe} ${child.title ?? ""}`.trim());
    }
    for (const entry of suite.specs ?? []) {
      const runs = (entry.tests ?? []).flatMap((t) => t.results ?? []);
      const status =
        runs.length === 0
          ? "unknown"
          : runs.some((r) => r.status === "skipped")
            ? "skipped"
            : runs[runs.length - 1].status;
      out.push({ describe, title: entry.title ?? "", status });
    }
  };
  walk(spec, "");
  return out;
}

let failed = false;
for (const file of specFiles) {
  const spec = specByName(file);
  if (spec === null) {
    console.error(
      `[assert-e2e-no-skips] FAIL: ${file} is absent from the report — the suite did not run at all`,
    );
    failed = true;
    continue;
  }

  const counts = {};
  const skipped = [];
  for (const test of allTests(spec)) {
    counts[test.status] = (counts[test.status] ?? 0) + 1;
    if (test.status === "skipped") skipped.push(test);
  }

  // Match each skipped test against an exemption entry; unmatched fail.
  const unmatched = [];
  for (const test of skipped) {
    const fullTitle = `${test.describe} ${test.title}`.trim();
    const entry = NOSKIP_EXEMPTIONS.find(
      (e) => e.file === file && fullTitle.includes(e.title),
    );
    if (entry === undefined) unmatched.push(fullTitle);
    else {
      console.log(
        `[assert-e2e-no-skips]   exempted: "${fullTitle.slice(0, 72)}" (${entry.gate})`,
      );
    }
  }
  if (unmatched.length > 0) {
    console.error(
      `[assert-e2e-no-skips] FAIL: ${file} has ${unmatched.length} UNEXEMPTED skip(s):`,
    );
    for (const title of unmatched) console.error(`  "${title}"`);
    failed = true;
  }

  const unknown = counts.unknown ?? 0;
  if (unknown > 0) {
    console.error(
      `[assert-e2e-no-skips] FAIL: ${file} has ${unknown} test(s) with no recorded run`,
    );
    failed = true;
  }

  const summary = Object.entries(counts)
    .map(([status, count]) => `${count} ${status}`)
    .join(", ");
  console.log(`[assert-e2e-no-skips] ${file}: ${summary}`);
}

process.exit(failed ? 1 : 0);
