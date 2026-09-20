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
   required. The committed snapshot currently covers the identity surface
   only (`/api/v1/auth/*`, `/api/v1/me*`); refresh it from a running backend
   (`curl -o openapi.snapshot.json $OPENAPI_URL`) as later streams freeze
   more routers.

Note: `openapi-typescript@7` declares a `typescript@^5` peer range while this
project pins TypeScript 6, so `frontend/.npmrc` sets `legacy-peer-deps=true`
(installs and `npm ci` would otherwise fail on peer resolution). The
generated artifact is consumed by the project's own `tsc`, which type-checks
it in every `npm run typecheck` run; drop the flag once upstream widens the
peer range.

## Testing

Unit tests use the Node built-in runner (`node:test`) executed through
`tsx` — no extra framework dependency. Files live in `src/__tests__/*.test.ts`
and are type-checked by `npm run typecheck` like any other source.
