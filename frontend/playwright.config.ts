/**
 * CampusQuest Playwright config — Plan 10 Task 1 (E1), Task 2 (E2).
 *
 * Ruling (E1 brief): the config itself orchestrates BOTH servers — the
 * backend (uvicorn serving create_app) and the frontend (next dev) —
 * through webServer, so `npx playwright test` is self-contained against
 * the local docker-compose dependency stack. CI wiring lands with E5;
 * the ports follow the spec defaults (frontend 3000, backend 8000) and
 * are derived from the same CQ_E2E_* variables the specs read, so an
 * override moves the client and the servers together.
 *
 * E2 adds the seeded world: globalSetup runs browser_world.py (the
 * backend e2e factories) BEFORE the workers fork and publishes the
 * CQ_E2E_* contract through process env; globalTeardown cleans the same
 * world after every worker exits (e2e/global-setup.ts docstring).
 *
 * The suites themselves stay behind the CQ_E2E=1 guard (the spec-file
 * convention): a bare `npx playwright test` collects them as skipped,
 * so ordinary development never depends on a live backend.
 */
import { defineConfig } from "@playwright/test";
import { join } from "node:path";

import { ensureDevCertificate } from "./e2e/global-setup";

// P3-B: the https cert must exist BEFORE the webServers start — the
// config module body runs ahead of them, globalSetup does NOT (the
// first battery died exactly there: uvicorn booted against a cert
// path that globalSetup had not written yet).
ensureDevCertificate();

const BASE_URL = process.env.CQ_E2E_BASE_URL ?? "https://localhost:3000";
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
  // e2e-only https MinIO (compose profile "e2e", :9002): the pages are
  // https (P3-B), so an http presigned URL would be mixed content the
  // browser blocks. The shared :9000 instance stays http for everything
  // else. AWS_CA_BUNDLE makes boto3 trust the self-signed cert — a
  // deployment knob, zero product-code change — resolved absolutely so
  // it holds regardless of the spawned backend's CWD.
  S3_ENDPOINT_URL: "https://localhost:9002",
  AWS_CA_BUNDLE: join(__dirname, "..", "infra", "e2e-certs", "minio", "public.crt"),
  S3_BUCKET: "campusquest-test",
  S3_ACCESS_KEY: "campusquest",
  S3_SECRET_KEY: "campusquest-dev",
  BUSINESS_TIMEZONE: "Asia/Shanghai",
};

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  // The visual suite NEVER retries (C3 promotion): a pixel flake is a
  // font/rendering drift signal that must surface, not be masked by a
  // passing second attempt.
  retries: process.env.CQ_VISUAL ? 0 : process.env.CI ? 1 : 0,
  workers: 1,
  // Plan-12 task 9: the visual-regression suite's baselines live at the
  // default per-spec -snapshots path (e2e/<spec>-snapshots/<name>-<platform>.png
  // — Playwright's screenshot assertions default snapshotSuffix to
  // process.platform, playwright/lib/index.js, which is exactly the
  // Linux-authoritative mechanism: a macOS run misses its -darwin
  // baselines and fails instead of silently diffing across renderers).
  // Pinning the template explicitly — it IS the Playwright default —
  // turns the committed-baseline location into a contract: a future
  // config edit cannot silently relocate the PNGs.
  snapshotPathTemplate:
    "{testDir}/{testFilePath}-snapshots/{arg}{-snapshotSuffix}{ext}",
  // The JSON report (PR #6 final review P1) feeds the release gate's
  // unexpected-skip assertion (scripts/assert-e2e-no-skips.mjs): the
  // teacher/admin suites must RUN their tests, and a world export that
  // went missing has to fail the gate instead of quietly reading as a
  // green run with two absent suites. test-results/ is gitignored.
  reporter: [
    ["list"],
    ["json", { outputFile: "test-results/report.json" }],
  ],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  // P3-B: cross-browser smoke. Chromium stays the default (full suite,
  // zero change); firefox/webkit projects run ONLY the smoke spec with
  // CQ_SMOKE=1 injected — the trade-off (deliberately narrow subset,
  // no full matrix — CI time vs signal) is recorded in the spec header
  // and the PR.
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
      testIgnore: /cross-browser-smoke\.spec\.ts/,
    },
    {
      name: "firefox",
      testMatch: /cross-browser-smoke\.spec\.ts/,
      use: { browserName: "firefox" },
    },
    {
      name: "webkit",
      testMatch: /cross-browser-smoke\.spec\.ts/,
      use: { browserName: "webkit" },
    },
  ],
  use: {
    baseURL: BASE_URL,
    // P3-B https stack: the pages themselves are served over the same
    // self-signed cert the webServers probe — every browser context
    // must accept it (the webServer-level flag only covers readiness
    // probes, NOT page navigations).
    ignoreHTTPSErrors: true,
  },
  webServer: [
    {
      // P3-B: the stack speaks https (self-signed, generated by
      // globalSetup into e2e/.certs/) so WebKit — which, unlike
      // Chromium/Firefox, refuses to STORE Secure cookies over plain
      // http — exercises the production cookie semantics. Browsers run
      // with ignoreHTTPSErrors, so the throwaway cert needs no trust.
      // Experiment A: backend stays http — Next's rewrites proxy runs
      // on undici (strict TLS, ignores NODE_TLS_REJECT_UNAUTHORIZED),
      // so the proxy TARGET must be plain http; the BROWSER still sees
      // an https page origin, and Set-Cookie's Secure attribute rides
      // the same-origin response untouched (webkit stores it by page
      // origin, not backend protocol).
      command: `uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port ${backendPort}`,
      url: `${backendOrigin}/health/ready`,
      cwd: "../backend",
      env: backendEnv,
      // FAIL CLOSED for the release gate (PR #6 E6 parallel review P1):
      // a release proof must never silently adopt whatever process
      // already listens on the port — another checkout's uvicorn (or
      // another app entirely) would turn the gate green against the
      // WRONG artifact. Default false; developers opt into reuse for
      // fast iteration loops with CQ_E2E_REUSE_EXISTING=1.
      reuseExistingServer:
        process.env.CQ_E2E_REUSE_EXISTING === "1",
      // Self-signed dev cert: the readiness probe must accept it.
      ignoreHTTPSErrors: true,
      timeout: 120_000,
    },
    {
      command: `npx next dev -p ${frontendPort} --experimental-https --experimental-https-key e2e/.certs/server.key --experimental-https-cert e2e/.certs/server.crt`,
      url: BASE_URL,
      // The frontend calls same-origin /api/v1/* (lib/api.ts); local
      // development needs next.config.ts's opt-in proxy pointed at the
      // orchestrated backend, or every API call lands on Next itself.
      env: {
        ...process.env,
        // http target for the undici proxy (see the backend command note).
        CQ_DEV_API_PROXY: `http://localhost:${backendPort}`,
      },
      // Same fail-closed rule as the backend server above.
      reuseExistingServer:
        process.env.CQ_E2E_REUSE_EXISTING === "1",
      ignoreHTTPSErrors: true,
      timeout: 180_000,
    },
  ],
});
