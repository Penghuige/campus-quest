/**
 * Plan-14 T2: the CSS integrity guard must treat Tailwind utilities from
 * the compiled pipeline (postcss + @tailwindcss/postcss, the same pipeline
 * the app builds with) as defined classes. Before the guard learned this,
 * any utility in a className was a used-but-undefined false positive.
 *
 * The guard is driven as a subprocess against a throwaway git fixture dir
 * (the guard lists tsx via `git grep`, so the fixture is `git init`ed and
 * staged — no repo index is touched).
 */
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const guardScript = join(frontendRoot, "scripts/check-css-integrity.mjs");

const FIXTURE_GLOBALS = `@import "tailwindcss/utilities" layer(utilities);

@theme inline {
  --color-surface-1: var(--surface-1);
  --breakpoint-md: 48rem;
}

:root {
  --surface-1: oklch(100% 0 0);
}

.fixture-card {
  background: var(--surface-1);
}
`;

function makeFixture(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "cq-css-guard-"));
  mkdirSync(join(dir, "src/app"), { recursive: true });
  writeFileSync(join(dir, "src/app/globals.css"), FIXTURE_GLOBALS);
  for (const [name, body] of Object.entries(files)) {
    writeFileSync(join(dir, "src", name), body);
  }
  // Tailwind resolves `@import "tailwindcss/…"` by walking node_modules
  // up from the CSS file — point the fixture at the real install.
  // Windows junctions link directories without requiring symlink privileges.
  symlinkSync(
    join(frontendRoot, "node_modules"),
    join(dir, "node_modules"),
    process.platform === "win32" ? "junction" : undefined,
  );
  execFileSync("git", ["init", "-q"], { cwd: dir });
  execFileSync("git", ["add", "--", "src"], { cwd: dir });
  return dir;
}

function runGuard(dir: string) {
  return spawnSync(process.execPath, [guardScript], {
    cwd: dir,
    encoding: "utf8",
  });
}

test("compiled Tailwind utilities (incl. variants) are defined classes", () => {
  const dir = makeFixture({
    "Widget.tsx": `export function Widget() {
  return <div className="fixture-card flex items-center bg-surface-1 md:grid">hi</div>;
}
`,
  });
  const result = runGuard(dir);
  assert.equal(
    result.status,
    0,
    `guard must accept compiled utilities; stderr:\n${result.stderr}`,
  );
  assert.match(result.stdout, /CSS integrity OK/);
});

test("a class that is neither hand-written nor compiled still errors", () => {
  const dir = makeFixture({
    "Widget.tsx": `export function Widget() {
  return <div className="fixture-card definitely-not-a-real-class">hi</div>;
}
`,
  });
  const result = runGuard(dir);
  assert.equal(result.status, 1, "guard must reject the unknown class");
  assert.match(result.stderr, /definitely-not-a-real-class/);
});

test("a bracket arbitrary value fails the gate (C3: none allowed outside theme)", () => {
  const dir = makeFixture({
    "Widget.tsx": `export function Widget() {
  return <div className="w-[40rem] fixture-card">hi</div>;
}
`,
  });
  const result = runGuard(dir);
  assert.equal(result.status, 1, "guard must reject the bracket arbitrary value");
  assert.match(result.stderr, /arbitrary value "w-\[40rem\]" outside the theme bridge/);
});

test("theme-bridge colors compile to plain utilities and pass (no brackets)", () => {
  const dir = makeFixture({
    "Widget.tsx": `export function Widget() {
  return <div className="bg-surface-1 fixture-card">hi</div>;
}
`,
  });
  const result = runGuard(dir);
  assert.equal(result.status, 0, `bridge color must pass; stderr:\n${result.stderr}`);
  assert.match(result.stdout, /CSS integrity OK/);
});

test("deleted tracked tsx files are skipped", () => {
  const dir = makeFixture({
    "Widget.tsx": `export function Widget() {
  return <div className="definitely-not-a-real-class">hi</div>;
}
`,
    "Remaining.tsx": `export function Remaining() {
  return <div className="fixture-card">hi</div>;
}
`,
  });
  unlinkSync(join(dir, "src", "Widget.tsx"));
  const result = runGuard(dir);
  assert.equal(result.status, 0, `deleted file must not fail the guard; stderr:\n${result.stderr}`);
  assert.match(result.stdout, /CSS integrity OK/);
});
