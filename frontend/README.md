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

## e2e (spec only until Plan 10)

`e2e/auth.spec.ts` holds the registration/login Playwright flows written by
Plan 09 Task 2; `e2e/task-claim.spec.ts` (Plan 09 Task 3) covers task
discovery and the claim flow — including the spec §42 privacy pins (no
assignment list, no assignment_id input, platform/keyword only after the
server allocates) and a 375x812 claim-CTA viewport check. It additionally
skips its claim-flow tests unless `CQ_E2E_TASK_URL` + `CQ_E2E_STUDENT`
(the Plan 10 fixture contract) are provided. Playwright is NOT installed yet: the directory is excluded
from `tsconfig.json` (by include list) and from ESLint (`eslint.config.mjs`
globalIgnores), and every test is skipped unless `CQ_E2E=1` — so typecheck,
lint, and build never depend on it. Plan 10 installs `@playwright/test`, adds
the `test:e2e` script, removes the eslint ignore, and runs the suite with
`CQ_E2E=1 CQ_E2E_BASE_URL=… CQ_E2E_API_URL=… CQ_E2E_OTP_CODE=…`.
