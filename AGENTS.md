# CampusQuest Agent Instructions

This file is the default entry point for coding agents working in this repository.

## Source-of-truth order

When instructions conflict, use this precedence:

1. The human's current explicit request.
2. docs/superpowers/specs/2026-09-19-campusquest-design.md
3. The relevant implementation plan under docs/superpowers/plans/
4. This AGENTS.md and the quality documents under docs/quality/
5. Official documentation matching the installed dependency version.
6. External reference repositories and skills.

External templates are examples, not product requirements.

## Required reading before editing

All agents MUST read:

- docs/superpowers/specs/2026-09-19-campusquest-design.md
- the implementation plan for the task they are executing
- docs/quality/quality-gates.md — including §16 Engineering Golden Rules,
  which is a long-term merge gate (PR #2 onward): every implementation and
  review must comply, with G1/G2/G3/G17/G18 as blocking checks

Frontend work MUST also read:

- docs/quality/frontend-design-system.md
- docs/quality/frontend-patterns.md
- docs/quality/agent-tooling.md

Backend, worker, database, and integration work MUST also read:

- docs/quality/backend-engineering.md
- docs/quality/agent-tooling.md

When deciding whether to copy a pattern from another codebase, read:

- docs/quality/reference-projects.md

## Development discipline

- Use test-driven development for features and bug fixes.
- Reproduce a bug with a failing regression test before fixing it.
- Run focused tests while developing, then the relevant module gate before completion.
- Use a fresh verification run before claiming the task is complete.
- Request code review for completed implementation tasks before merging them into a wider workstream.
- Do not silently change product semantics to make implementation easier. Amend the spec or plan first if a real ambiguity is discovered.
- Do not introduce a new dependency when a project dependency or platform primitive already solves the problem adequately.
- Do not make unrelated refactors.

## Next.js rule

After Next.js is installed, agents MUST read the version-matched documentation bundled with the installed package before relying on training-data assumptions:

frontend/node_modules/next/dist/docs/

If Next.js generates or updates its managed agent block in this file, preserve CampusQuest instructions outside that managed block.

## FastAPI rule

Prefer current FastAPI patterns and the official FastAPI skill or documentation.

In particular:

- use current dependency-injection forms;
- keep routers thin;
- keep public response schemas explicit;
- do not invent legacy patterns from memory when installed or current docs disagree.

## Frontend quality rule

CampusQuest is an academic productivity product with restrained gamification, not a generic SaaS template and not a game UI.

Agents MUST:

- use the design tokens and visual hierarchy defined in frontend-design-system.md;
- reuse project primitives and components before creating near-duplicates;
- keep rarity and gamification accents local rather than tinting whole pages;
- preserve visible focus, keyboard access, reduced-motion behavior, and non-color status cues;
- treat loading, empty, error, permission-denied, long-content, and mobile states as first-class states;
- avoid decorative gradients, glassmorphism, neon glows, excessive cards, and arbitrary animation unless the design spec explicitly calls for them;
- never expose student number, phone, email, object keys, internal IDs, or anonymous identities in public or student-facing DOM.

The backend is authoritative for reward tiers, points, permissions, deadlines, and workflow states. Frontend countdowns and optimistic updates are presentation conveniences only.

## Backend quality rule

The backend is a modular monolith. Keep domain ownership explicit:

router -> application/domain service -> repository/query/port -> infrastructure

Agents MUST:

- keep transaction boundaries in services or use cases;
- never implement core multi-table state changes directly in routers;
- never duplicate domain state-transition logic in Celery jobs;
- use PostgreSQL constraints and locks for real invariants, not only Python checks;
- keep Pydantic transport DTOs distinct from persistence models;
- use the injectable Clock for business time;
- access SMS, email, object storage, ranking projection, and other external systems through ports and adapters;
- preserve immutable history for Claims, Submissions, Ledger entries, Reviews, and AuditLog;
- use stable BusinessError codes for expected business conflicts;
- avoid catch-all exception swallowing.

## Database and migration rule

- Integration and concurrency behavior is verified on PostgreSQL, not SQLite.
- Every schema change gets an Alembic migration.
- Migrations must be deterministic and reviewable.
- Do not modify already-shipped migration history unless the branch has not been shared and the implementation plan explicitly allows it.
- Do not put application runtime logic into migrations.
- Serialize migration ownership when multiple agents work in parallel.

## Generated code and external references

Generated API clients, shadcn components, and template-derived code must be reviewed like handwritten code.

Do not copy entire external templates into CampusQuest. Extract the pattern that solves the task while preserving our architecture and design language.

See docs/quality/reference-projects.md for approved references and explicit learn/do-not-copy guidance.

## Skill usage

Use installed project or runtime skills when their trigger matches the task. Recommended skills and installation notes are documented in docs/quality/agent-tooling.md.

Skills do not override the CampusQuest spec or these instructions.

## Completion evidence

A task is not complete until:

- relevant tests pass;
- formatter, linter, and type checks for touched code pass;
- database migrations were exercised when applicable;
- accessibility and visual checks were performed for frontend work;
- high-risk concurrency and idempotency tests were run when applicable;
- the diff contains no secrets, debug artifacts, placeholder TODOs, or accidental generated files;
- the implementation matches the relevant plan acceptance criteria.
