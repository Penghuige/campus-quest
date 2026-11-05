import { defineConfig } from "@playwright/test";

// Public document/session-shell tests do not use the shared DB, API or MinIO stack.
const prefix = process.env.CQ_BASE_PATH ?? "";
export default defineConfig({
  testDir: "./e2e",
  testMatch: "landing.integration.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 60_000,
  outputDir: "test-results/landing",
  reporter: [["list"], ["json", { outputFile: "test-results/landing-report.json" }]],
  use: { baseURL: "http://127.0.0.1:49356", browserName: "chromium", channel: process.env.CQ_LANDING_CHANNEL, viewport: { width: 1280, height: 720 }, trace: "retain-on-failure" },
  webServer: {
    command: "node node_modules/next/dist/bin/next start --port 49356",
    url: `http://127.0.0.1:49356${prefix}/about`,
    reuseExistingServer: false,
    env: { CQ_BASE_PATH: prefix, NEXT_TELEMETRY_DISABLED: "1" },
    timeout: 120_000,
  },
});
