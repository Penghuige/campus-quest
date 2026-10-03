#!/usr/bin/env node
/**
 * Release-gate unexpected-skip assertion — PR #6 final review P1.
 *
 * Reads the Playwright JSON report the e2e run just wrote and fails
 * (exit 1) when any watched suite has skipped — or entirely missing —
 * tests. The watched default is the teacher/admin pair whose fixture
 * contract (CQ_E2E_STAFF* / CQ_E2E_TEACHER / CQ_E2E_ADMIN + the base32
 * TOTP world exports) landed with PR #6: before it, both suites
 * silently self-skipped and a green gate said nothing about them.
 *
 * Usage (from frontend/):
 *   node scripts/assert-e2e-no-skips.mjs [specBasename ...]   # default:
 *                                                           # teacher.spec.ts
 *                                                           # admin.spec.ts
 *
 * An ENV-OPTIONAL test may be exempted explicitly so its spec can be
 * watched without pretending the skip is a run:
 *   --allow-skip <specBasename>:<titleSubstring>
 * (repeatable; the exemption is per title substring, and the verdict
 * still logs every allowed skip it ignored — r4 M2, PR #19).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const reportPath = join(here, "..", "test-results", "report.json");

// argv: spec basenames positionally; --allow-skip spec:titleSubstring
// entries collect into an explicit exemption list (see the header).
const argv = process.argv.slice(2);
const allowSkip = new Map();
for (let i = 0; i < argv.length; i += 1) {
  if (argv[i] === "--allow-skip" && argv[i + 1] !== undefined) {
    const separator = argv[i + 1].indexOf(":");
    if (separator <= 0) {
      console.error(
        `[assert-e2e-no-skips] --allow-skip needs <specBasename>:<titleSubstring>, got ${JSON.stringify(argv[i + 1])}`,
      );
      process.exit(1);
    }
    const spec = argv[i + 1].slice(0, separator);
    const title = argv[i + 1].slice(separator + 1);
    allowSkip.set(spec, [...(allowSkip.get(spec) ?? []), title]);
    i += 1;
  }
}
const watchedSpecs = argv.filter(
  (arg, index) => arg !== "--allow-skip" && argv[index - 1] !== "--allow-skip",
);
const watched =
  watchedSpecs.length > 0 ? watchedSpecs : ["teacher.spec.ts", "admin.spec.ts"];

/** Every test in the report, flattened across describe nesting. */
function collect(spec) {
  const tests = [];
  const walk = (suite) => {
    for (const child of suite.suites ?? []) walk(child);
    for (const test of suite.specs ?? []) tests.push(test);
  };
  walk(spec);
  return tests;
}

/** A spec's verdict: the WORST of its test runs — a skip hides behind
 * nothing (any skipped run reports "skipped" even when a retry later
 * passed, and a spec that never executed reports "unknown"). */
function specStatus(spec) {
  const runs = spec.tests?.flatMap((test) => test.results ?? []) ?? [];
  if (runs.length === 0) {
    return "unknown";
  }
  const order = ["skipped", "unknown", "failed", "timedOut", "interrupted", "passed"];
  for (const status of order) {
    if (runs.some((run) => run.status === status)) {
      return status === "timedOut" || status === "interrupted" ? "failed" : status;
    }
  }
  return runs[runs.length - 1].status;
}

let report;
try {
  report = JSON.parse(readFileSync(reportPath, "utf-8"));
} catch (error) {
  console.error(
    `[assert-e2e-no-skips] cannot read the Playwright JSON report at ${reportPath}: ${error}`,
  );
  process.exit(1);
}

let failed = false;
for (const name of watched) {
  const spec = (report.suites ?? []).find((suite) =>
    suite.file !== undefined ? suite.file.endsWith(name) : false,
  );
  if (spec === undefined) {
    console.error(
      `[assert-e2e-no-skips] FAIL: ${name} is absent from the report — the suite did not run at all`,
    );
    failed = true;
    continue;
  }
  const tests = collect(spec);
  const counts = {};
  const skippedTitles = [];
  for (const test of tests) {
    const status = specStatus(test);
    counts[status] = (counts[status] ?? 0) + 1;
    if (status === "skipped") {
      skippedTitles.push(test.title ?? "(untitled)");
    }
  }
  // Allowed skips stay out of the verdict but NEVER out of the log.
  const allowed = allowSkip.get(name) ?? [];
  const unexpectedSkips = skippedTitles.filter(
    (title) => !allowed.some((fragment) => title.includes(fragment)),
  );
  for (const title of skippedTitles) {
    const exempted = unexpectedSkips.indexOf(title) === -1;
    console.log(
      `[assert-e2e-no-skips]   skipped: "${title}"${exempted ? " (allow-listed)" : ""}`,
    );
  }
  const skipped = unexpectedSkips.length;
  const summary = Object.entries(counts)
    .map(([status, count]) => `${count} ${status}`)
    .join(", ");
  console.log(`[assert-e2e-no-skips] ${name}: ${summary}`);
  if (skipped > 0) {
    console.error(
      `[assert-e2e-no-skips] FAIL: ${name} skipped ${skipped} test(s) — an expected world export is missing; a skip must never stand in for a run`,
    );
    failed = true;
  }
  const unknown = counts.unknown ?? 0;
  if (unknown > 0) {
    console.error(
      `[assert-e2e-no-skips] FAIL: ${name} has ${unknown} test(s) with no recorded run`,
    );
    failed = true;
  }
}

process.exit(failed ? 1 : 0);
