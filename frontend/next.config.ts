import type { NextConfig } from "next";

/**
 * Local-dev knobs. Two deliberate defaults:
 *
 * - `agentRules: false` — Next 16 would otherwise (re)generate
 *   `AGENTS.md` + `CLAUDE.md` inside `frontend/` on every `next dev`
 *   run; the repository's root agent docs cover this project, so the
 *   per-directory churn stays out of feature diffs.
 * - optional API proxy — the frontend calls same-origin `/api/v1/*`
 *   (`lib/api.ts`); production fronts the backend on the same origin.
 *   For local development against a separately-running backend, set
 *   CQ_DEV_API_PROXY to the backend origin (e.g. `http://localhost:8000`)
 *   and every `/api/v1` request is rewritten there. Unset — the default,
 *   including production builds — changes nothing.
 */
const proxyTarget = process.env.CQ_DEV_API_PROXY;

const nextConfig: NextConfig = {
  agentRules: false,
  ...(proxyTarget !== undefined && proxyTarget !== ""
    ? {
        async rewrites() {
          return [
            {
              source: "/api/v1/:path*",
              destination: `${proxyTarget}/api/v1/:path*`,
            },
          ];
        },
      }
    : {}),
};

export default nextConfig;
