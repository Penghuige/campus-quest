#!/usr/bin/env node
// CSS integrity guard: used-but-undefined classes, raw colors outside
// token declarations, !important. Runs in CI; the only dependencies are
// the already-installed postcss + @tailwindcss/postcss, used to harvest
// the utilities the app's own compiled pipeline generates (Plan 14 T2).
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";
import postcss from "postcss";
import tailwindcss from "@tailwindcss/postcss";

const cssPath = "src/app/globals.css";
const css = readFileSync(cssPath, "utf8");
const errors = [];
const warnings = [];

// --- defined classes: every .class token appearing in any selector ---
// `definedInGlobals` alone feeds the dead-CSS warning (its purpose is
// flagging stale hand-written rules); the union feeds the used-check.
const definedInGlobals = new Set();
for (const m of css.matchAll(/\.([a-z][a-z0-9-]*)/g)) {
  definedInGlobals.add(m[1]);
}
const defined = new Set(definedInGlobals);

// --- defined classes, compiled pipeline: Tailwind v4 generates utilities
// by scanning the source for candidates, so a used utility never appears
// in globals.css. Compile through the SAME postcss pipeline the app uses
// and harvest the emitted selectors (unescaping variant syntax, so the
// compiled `.md\:grid` defines the class token `md:grid`). A compilation
// failure propagates and fails the guard — never silently skipped.
const compiled = await postcss([tailwindcss()]).process(css, {
  from: cssPath,
});
// Strip comments first: the compiler banner carries a URL whose ".tld"
// fragment the selector regex would otherwise harvest as a class token.
const compiledCss = compiled.css.replace(/\/\*[\s\S]*?\*\//g, "");
for (const m of compiledCss.matchAll(
  /\.((?:\\.|[-_a-zA-Z])(?:\\.|[-_a-zA-Z0-9])*)/g,
)) {
  defined.add(m[1].replace(/\\(.)/g, "$1"));
}

// --- used classes: static string literals inside className={...} ---
// File list: pathspec form ('' pattern matches every line, so -l lists
// all .tsx under src); the plan's `git grep -l -- '*.tsx' -- src` treated
// the glob as the match pattern and listed nothing.
const files = execSync(
  "git grep -l '' -- 'src/*.tsx' | grep -v __tests__",
  { encoding: "utf8" },
).trim().split("\n").filter(Boolean);
const used = new Map(); // class -> [file]
for (const f of files) {
  const text = readFileSync(f, "utf8");
  for (const m of text.matchAll(/className\s*=\s*(\{[^}]*\}|"[^"]*"|'[^']*')/gs)) {
    const expr = m[1];
    if (expr.includes("${")) {
      // template literal: still scan the static segments
    }
    for (const s of expr.matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`/gs)) {
      const lit = s[1] ?? s[2] ?? s[3];
      for (const tok of lit.split(/\s+/)) {
        // Variant syntax joins segments with colons (responsive/state
        // prefixes); each segment stays lowercase-token shaped, and
        // arbitrary values (bracket form) still do not match and are
        // skipped as before.
        if (!/^[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)*$/.test(tok)) continue;
        if (/\$\{/.test(tok)) continue;
        if (!used.has(tok)) used.set(tok, []);
        used.get(tok).push(f);
      }
    }
  }
}

// Classes produced dynamically or by libraries; extend only with a
// comment citing the producing code.
const ALLOWED_UNDEFINED = new Set([
  // e.g. "active" toggled via data attributes elsewhere
  // Ternary comparison operands / template-interpolation suffixes misread
  // as class tokens (the rendered classes — badge-*, alert-*, btn-* — are
  // defined; these literals never reach the DOM as classes):
  "success",    // badge-${... "success" ...}: WhitelistAdmin/RewardsAdmin/SystemAdmin/ValidationReport
  "muted",      // badge-${...} suffix + claim.tone === "muted": RewardsAdmin/SubmissionReview/SystemAdmin/WhitelistAdmin
  "info",       // badge-${... "info" ...}: SubmissionReview.tsx:243
  "danger",     // badge-${... "danger" ...}: ValidationReport.tsx:26
  "approved",   // outcome.kind === "approved": SubmissionReview.tsx:688
  "revision",   // outcome.kind === "revision": SubmissionReview.tsx:688
  "grant",      // mode === "grant": RewardsAdmin.tsx:780
  "compact",    // size === "compact": TaskLifecycleActions.tsx:60
  "ok",         // outcome.tone === "ok": TaskRating.tsx:145
  "verified",   // view.state === "verified": AccountSettings.tsx:531
  "unverified", // view.state === "unverified": AccountSettings.tsx:533
]);

for (const [cls, where] of used) {
  if (defined.has(cls)) continue;
  if (ALLOWED_UNDEFINED.has(cls)) continue;
  errors.push(`used-but-undefined class ".${cls}" in ${[...new Set(where)].join(", ")}`);
}

// --- raw colors: strip custom-property declarations, then scan ---
const withoutDecls = css.replace(/^\s*--[a-z0-9-]+\s*:[^;]+;.*$/gim, "");
for (const [i, line] of withoutDecls.split("\n").entries()) {
  if (/#[0-9a-fA-F]{3,8}\b/.test(line))
    errors.push(`raw hex color at ${cssPath}:${i + 1}: ${line.trim()}`);
  if (/\boklch\(/.test(line))
    errors.push(`raw oklch() outside token declaration at ${cssPath}:${i + 1}: ${line.trim()}`);
  if (/!important/.test(line))
    errors.push(`!important at ${cssPath}:${i + 1}: ${line.trim()}`);
}

// --- defined-but-unreferenced: warning only (hand-written rules; the
// compiled utility harvest is generated, never "dead CSS") ---
const tsxAll = files.map((f) => readFileSync(f, "utf8")).join("\n");
for (const cls of definedInGlobals) {
  if (!tsxAll.includes(cls) && !new RegExp(`\\.${cls}[^{]*\\{[^}]*\\.`, "s").test(css))
    warnings.push(`defined-but-unreferenced ".${cls}" (dead CSS candidate)`);
}

for (const w of warnings) console.warn(`warn: ${w}`);
if (errors.length) {
  console.error(`CSS integrity: ${errors.length} error(s)`);
  errors.forEach((e, i) => console.error(`${i + 1}. ${e}`));
  process.exit(1);
}
console.log(`CSS integrity OK (${used.size} used classes, ${defined.size} defined, ${warnings.length} warnings)`);
