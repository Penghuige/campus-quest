import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// Flat config per the bundled Next.js 16 docs
// (node_modules/next/dist/docs, "ESLint Plugin" setup). `next lint` was
// removed in Next.js 16, so linting runs through this file via `eslint .`.
export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);
