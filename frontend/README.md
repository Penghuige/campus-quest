# CampusQuest Frontend

Next.js (App Router) client for the CampusQuest backend. Product rules live in
`docs/superpowers/specs/2026-09-19-campusquest-design.md`; visual language in
`docs/quality/frontend-design-system.md`; implementation patterns in
`docs/quality/frontend-patterns.md`.

## Commands

| Script | Purpose |
| --- | --- |
| `npm run dev` | local dev server |
| `npm run build` | production build |
| `npm run lint` | ESLint (eslint-config-next) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run test:unit` | unit tests (node:test via tsx) |
| `npm run test:e2e` | Playwright e2e battery (needs `CQ_E2E=1`; the config starts both servers) |
| `npm run test:e2e:visual` | visual-regression suite (`CQ_VISUAL_UPDATE=1` regenerates baselines) |
| `npm run test:e2e:a11y` | axe-core accessibility suite |
| `npm run test:e2e:smoke` | firefox/webkit cross-browser smoke projects |
| `npm run coverage:ratchet` | unit-suite coverage ratchet gate |
| `npm run coverage:update` | re-record coverage floors (intentional change) |
| `npm run audit:gate` | audit guard (scripts/check-audit.mjs) |
| `npm run api:types` | regenerate `src/lib/api/schema.d.ts` from a running backend |
| `npm run api:types:snapshot` | regenerate it from the committed snapshot instead |

## API types

`src/lib/api/schema.d.ts` is GENERATED code (openapi-typescript) — never edit
it by hand. It is produced at build/development time from the backend's
OpenAPI artifact, one of two ways:

1. **Live backend**: `OPENAPI_URL=http://localhost:8000/openapi.json npm run api:types`
   (default `OPENAPI_URL` shown; FastAPI serves the spec at its root
   `/openapi.json`).
2. **Committed snapshot**: `npm run api:types:snapshot` regenerates
   deterministically from `frontend/openapi.snapshot.json` — no backend
   required. The committed snapshot covers the identity, tasks/claims,
   points/rewards, and rankings surfaces (`/api/v1/auth/*`, `/api/v1/me*`,
   `/api/v1/tasks*`, `/api/v1/points/me`, `/api/v1/rewards*`,
   `/api/v1/rankings/*`, `/api/v1/growth/me`, plus the staff surfaces);
   refresh it from a running backend (`curl -o openapi.snapshot.json
   $OPENAPI_URL`) as later streams freeze more routers. (Task 3 of Plan 09
   regenerated it from the merged backend via `python -c "from app.main
   import app; ...app.openapi()"` — no server needs to be running.)

Note: `openapi-typescript@7` declares a `typescript@^5` peer range while this
project pins TypeScript 6, so `frontend/.npmrc` sets `legacy-peer-deps=true`
(installs and `npm ci` would otherwise fail on peer resolution). The
generated artifact is consumed by the project's own `tsc`, which type-checks
it in every `npm run typecheck` run; drop the flag once upstream widens the
peer range.

## Local dev API proxy

The app calls same-origin `/api/v1/*` (`src/lib/api.ts`); production fronts
the backend on the same origin. For local development against a
separately-running backend, start the dev server with
`CQ_DEV_API_PROXY=http://localhost:8000 npm run dev` and every `/api/v1`
request is rewritten there (see `next.config.ts`). Unset, the flag changes
nothing. `next.config.ts` also sets `agentRules: false` so `next dev` does
not regenerate `AGENTS.md`/`CLAUDE.md` inside `frontend/` on every run.

## Testing

Unit tests use the Node built-in runner (`node:test`) executed through
`tsx` — no extra framework dependency. Files live in `src/__tests__/*.test.ts`
and are type-checked by `npm run typecheck` like any other source. API calls
in tests are exercised through a stubbed `globalThis.fetch` (the api client's
test seam); no mocking library is used because
`docs/quality/frontend-patterns.md` prescribes none.

## e2e

The Playwright battery lives in `e2e/` — 20 spec files covering auth, task
claim, submission, community, notifications, the teacher/admin/staff
surfaces, rewards + rankings, accessibility, visual regression, and the
cross-browser/fence specials. Every spec is skipped unless `CQ_E2E=1`, so
ordinary development never depends on a live stack. ESLint lints `e2e/**`;
`tsc` does not (Playwright transpiles the specs itself).

`playwright.config.ts` is self-contained: its two `webServer` entries start
the backend (uvicorn) and the frontend (`next dev`) themselves, and
`globalSetup` seeds the e2e world and publishes the `CQ_E2E_*` contract
before the workers fork. Run it with `cd frontend && CQ_E2E=1 npm run
test:e2e` (or `make playwright-e2e` from the repo root).

The battery runs under a no-skip gate: `scripts/assert-e2e-no-skips.mjs`
reads the JSON report and fails on any skip not covered by a per-title
entry in `e2e/noskip-exemptions.mjs` (gate + reason), so a silently
skipped spec can never read as a green run.
