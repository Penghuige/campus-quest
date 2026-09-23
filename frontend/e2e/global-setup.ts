/**
 * CampusQuest browser-e2e global setup — Plan 10 Task 2 (E2).
 *
 * Seeds the browser world through the backend e2e factories
 * (tests/e2e/browser_world.py — the same factories the pytest e2e
 * modules use) BEFORE any spec collects, then publishes the contract as
 * process env: Playwright forks its workers from THIS process after
 * globalSetup, so every spec — including the Plan 09 ones that read
 * `process.env.CQ_E2E_*` at module scope — sees the seeded world.
 *
 * The cleanup counterpart (global-teardown.ts) consumes the world file
 * this setup persists; both shells pin the same test-stack env the
 * playwright webServer pins for the backend.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

// Playwright loads global-setup files through its TypeScript shim in
// CommonJS mode (package.json has no "type": "module"), so __dirname —
// not import.meta.url — is the portable module-location anchor here.
const here = __dirname;
export const BACKEND_DIR = join(here, "..", "..", "backend");

/** The pinned test-stack env (the webServer's backendEnv contract). */
export const backendEnv: NodeJS.ProcessEnv = {
  ...process.env,
  DATABASE_URL: "postgresql+asyncpg://test:test@localhost:15432/campusquest_test",
  REDIS_URL: "redis://localhost:6379/0",
  S3_ENDPOINT_URL: "http://localhost:9000",
  S3_BUCKET: "campusquest-test",
  S3_ACCESS_KEY: "campusquest",
  S3_SECRET_KEY: "campusquest-dev",
  BUSINESS_TIMEZONE: "Asia/Shanghai",
  CQ_E2E: "1",
};

/** Run one browser_world.py action; the JSON answer or a clear failure. */
export function runWorldAction<T = unknown>(
  action: string,
  ...args: string[]
): T {
  const result = spawnSync(
    "uv",
    ["run", "python", "tests/e2e/browser_world.py", action, ...args],
    { cwd: BACKEND_DIR, env: backendEnv, encoding: "utf-8" },
  );
  if (result.status !== 0) {
    throw new Error(
      `browser_world ${action} failed (${result.status}): ${result.stderr}`,
    );
  }
  return JSON.parse(result.stdout) as T;
}

export default function globalSetup(): void {
  // The suite's own guard (the spec-file convention): a bare
  // `npx playwright test` collects every spec as skipped and must not
  // need the dependency stack — so neither does the seed.
  if (process.env.CQ_E2E !== "1") {
    return;
  }
  const world = runWorldAction<{
    run: string;
    world_file: string;
    env: Record<string, string>;
  }>("seed");
  if (!existsSync(world.world_file)) {
    throw new Error(`seed did not persist its world file at ${world.world_file}`);
  }
  for (const [name, value] of Object.entries(world.env)) {
    process.env[name] = value;
  }
  process.env.CQ_E2E_WORLD_FILE = world.world_file;
}
