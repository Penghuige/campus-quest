/**
 * CampusQuest Playwright config — Plan 10 Task 1 (E1).
 *
 * Ruling (E1 brief): the config itself orchestrates BOTH servers — the
 * backend (uvicorn serving create_app) and the frontend (next dev) —
 * through webServer, so `npx playwright test` is self-contained against
 * the local docker-compose dependency stack. CI wiring lands with E5;
 * the ports follow the spec defaults (frontend 3000, backend 8000) and
 * are derived from the same CQ_E2E_* variables the specs read, so an
 * override moves the client and the servers together.
 *
 * The suites themselves stay behind the CQ_E2E=1 guard (the spec-file
 * convention): a bare `npx playwright test` collects them as skipped,
 * so ordinary development never depends on a live backend.
 */
import { defineConfig } from "@playwright/test";

const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "http://localhost:3000";
const API_URL = process.env.CQ_E2E_API_URL ?? "http://localhost:8000/api/v1";

const frontendPort = new URL(BASE_URL).port || "3000";
const backendOrigin = new URL(API_URL).origin;
const backendPort = new URL(API_URL).port || "8000";

/**
 * The disposable local dependency stack (db_guard's integration
 * defaults, CI's env): published by
 * `docker compose -f infra/docker-compose.yml up -d` from the repo
 * root. Spread over process.env (uv needs PATH/HOME to resolve), with
 * the pinned test-stack values LAST — the e2e world must sit on the
 * test stack, not on whatever a developer's shell happens to export.
 */
const backendEnv: Record<string, string> = {
  ...process.env,
  DATABASE_URL: "postgresql+asyncpg://test:test@localhost:15432/campusquest_test",
  REDIS_URL: "redis://localhost:6379/0",
  S3_ENDPOINT_URL: "http://localhost:9000",
  S3_BUCKET: "campusquest-test",
  S3_ACCESS_KEY: "campusquest",
  S3_SECRET_KEY: "campusquest-dev",
  BUSINESS_TIMEZONE: "Asia/Shanghai",
};

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
  },
  webServer: [
    {
      command: `uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port ${backendPort}`,
      url: `${backendOrigin}/health/ready`,
      cwd: "../backend",
      env: backendEnv,
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: `npx next dev -p ${frontendPort}`,
      url: BASE_URL,
      reuseExistingServer: true,
      timeout: 180_000,
    },
  ],
});
