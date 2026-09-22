import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// Flat config per the bundled Next.js 16 docs
// (node_modules/next/dist/docs, "ESLint Plugin" setup). `next lint` was
// removed in Next.js 16, so linting runs through this file via `eslint .`.
export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  // e2e/ holds the Plan 09 Task 2 Playwright spec, written ahead of the
  // runner: @playwright/test is installed by Plan 10, and until then ESLint
  // cannot resolve its import. tsconfig already excludes the directory.
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts", "e2e/**"]),
]);
