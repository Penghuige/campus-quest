#!/usr/bin/env node
/**
 * Architecture boundary gate (owner-approved 2026-10-16).
 *
 * Turns the written frontend boundary rules into machine-checked
 * dependencies. Source of truth for the rules:
 * - docs/quality/frontend-patterns.md §1: "components/ui owns generic
 *   primitives" (hence ui must never reach up into features/app/shell),
 *   "Feature components own product composition".
 * - lib/ is shared plumbing (api/auth/time/formatting) and therefore a
 *   bottom layer.
 *
 * Deliberately NOT errors:
 * - feature -> feature imports: the patterns doc does not forbid them, and
 *   the tree has real aggregation consumers (dashboard, admin). They are
 *   warn-level so the drift stays visible; tightening to error is an owner
 *   decision, not a gate decision.
 *
 * Run via `npm run arch:lint` or `make arch-lint`.
 */
const { readdirSync } = require("node:fs");
const { join } = require("node:path");

const srcRoot = join(__dirname, "src");
const features = readdirSync(join(srcRoot, "features"), { withFileTypes: true })
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name);

const notTests = {
  pathNot: ["^src/__tests__/", "\\.test\\.[cm]?[jt]sx?$", "\\.d\\.ts$"],
};

module.exports = {
  forbidden: [
    {
      name: "ui-primitives-stay-low",
      comment:
        "components/ui owns generic primitives (frontend-patterns §1); reaching into features, app routes, or the shell turns a primitive into product composition.",
      severity: "error",
      from: { path: "^src/components/ui/", ...notTests },
      to: { path: ["^src/features/", "^src/app/", "^src/components/shell/"] },
    },
    {
      name: "lib-is-a-bottom-layer",
      comment: "src/lib is shared plumbing; it must not depend on product code above it.",
      severity: "error",
      from: { path: "^src/lib/", ...notTests },
      to: { path: ["^src/features/", "^src/components/", "^src/app/"] },
    },
    {
      name: "features-never-import-routes",
      comment: "Route composition lives in app/; a feature importing route files inverts the composition direction.",
      severity: "error",
      from: { path: "^src/features/", ...notTests },
      to: { path: "^src/app/" },
    },
    // One warn rule per feature (depcruise matches from.path and to.path as
    // separate regexes, so cross-feature exclusion is built per source dir).
    ...features.map((feature) => ({
      name: `feature-${feature}-keeps-to-itself`,
      comment:
        "Cross-feature imports are not forbidden by the patterns doc; warn-level visibility so the owner can decide when to tighten.",
      severity: "warn",
      from: { path: `^src/features/${feature}/`, ...notTests },
      to: { path: "^src/features/", pathNot: `^src/features/${feature}(/|$)` },
    })),
  ],
  options: {
    doNotFollow: { path: "node_modules" },
    tsConfig: { fileName: join(__dirname, "tsconfig.json") },
    reporterOptions: {
      text: { highlightFocused: true },
    },
  },
};
