#!/usr/bin/env node
/**
 * P1 visibility: the npm audit gate — fails on HIGH/CRITICAL advisories
 * that are not covered by scripts/npm-audit-exemptions.mjs (each entry
 * carries a reason + re-review date; an EXPIRED entry re-fails until
 * re-triaged). Moderate/low never fail the gate.
 *
 * Usage (from frontend/):  node scripts/check-audit.mjs
 * Exits 1 with the failing set; exits 0 printing the accepted picture.
 */
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { AUDIT_EXEMPTIONS } from "./npm-audit-exemptions.mjs";

const here = dirname(fileURLToPath(import.meta.url));

/** `npm audit --json` parsed, or null when npm reports no advisories. */
function auditJson() {
  try {
    return JSON.parse(
      execFileSync("npm", ["audit", "--json", "--audit-level=high"], {
        cwd: join(here, ".."),
        encoding: "utf8",
      }),
    );
  } catch (error) {
    // npm audit exits non-zero when it FINDS something — the JSON still
    // parsed above when stdout was complete; a real failure to run is
    // fatal, not a pass.
    if (error.stdout) return JSON.parse(error.stdout);
    throw error;
  }
}

const GATING = new Set(["high", "critical"]);
const today = new Date().toISOString().slice(0, 10);

const report = auditJson();
const vulnerabilities = Object.values(report?.vulnerabilities ?? {});
const gating = vulnerabilities.filter((v) => GATING.has(v.severity));

const expired = AUDIT_EXEMPTIONS.filter((e) => e.reviewBy < today);
const exempted = new Map(
  AUDIT_EXEMPTIONS.filter((e) => e.reviewBy >= today).map((e) => [e.module, e]),
);

const failing = gating.filter((v) => !exempted.has(v.name));
const accepted = gating.filter((v) => exempted.has(v.name));
const subCritical =
  (report?.metadata?.vulnerabilities?.total ?? 0) -
  gating.length;

if (expired.length > 0) {
  console.error(
    "[audit] FAIL: exemption(s) past their re-review date — re-triage before anything else:",
  );
  for (const e of expired) {
    console.error(`  ${e.module} (${e.severity}) reviewBy ${e.reviewBy}`);
  }
  process.exit(1);
}

for (const v of accepted) {
  const e = exempted.get(v.name);
  console.log(`[audit] exempted: ${v.name} (${v.severity}) — ${e.reason.split(".")[0]}. (reviewBy ${e.reviewBy})`);
}
if (subCritical > 0) {
  console.log(`[audit] note: ${subCritical} sub-high advisories reported (below the gate level)`);
}

if (failing.length > 0) {
  console.error("[audit] FAIL: ungated HIGH/CRITICAL advisories:");
  for (const v of failing) {
    const via = v.via
      .map((x) => (typeof x === "string" ? x : x.name))
      .slice(0, 3)
      .join(", ");
    console.error(`  ${v.name} (${v.severity}) via ${via}`);
  }
  process.exit(1);
}

console.log("[audit] OK: no ungated high/critical advisories");
