#!/usr/bin/env node
// CSS integrity guard: used-but-undefined classes, raw colors outside
// token declarations, !important. Zero-dependency; runs in CI.
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";

const cssPath = "src/app/globals.css";
const css = readFileSync(cssPath, "utf8");
const errors = [];
const warnings = [];

// --- defined classes: every .class token appearing in any selector ---
const defined = new Set();
for (const m of css.matchAll(/\.([a-z][a-z0-9-]*)/g)) defined.add(m[1]);

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
        if (!/^[a-z][a-z0-9-]*$/.test(tok)) continue;
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

// --- defined-but-unreferenced: warning only ---
const tsxAll = files.map((f) => readFileSync(f, "utf8")).join("\n");
for (const cls of defined) {
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
