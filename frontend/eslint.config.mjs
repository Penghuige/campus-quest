import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// Flat config per the bundled Next.js 16 docs
// (node_modules/next/dist/docs, "ESLint Plugin" setup). `next lint` was
// removed in Next.js 16, so linting runs through this file via `eslint .`.
export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  // e2e/ holds the Playwright specs + shared fixtures. @playwright/test
  // is installed (Plan 10 E1), so the directory is linted; tsc still
  // skips it (tsconfig includes only src/** — Playwright transpiles the
  // specs itself), and `next build` never touches files outside src/app.
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);
