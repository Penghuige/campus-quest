# Load Testing & Capacity Evidence — Design

Date: 2026-10-29
Status: approved 2026-10-29 — owner approved the decision table (fix round
included); file profile: all homework families occur, mix pinned as agent
call; targets: agent-derived default accepted as working anchor, owner may
adjust before the headline run.
History: builder check 2026-10-29 raised 6 questions; resolutions recorded in
§Decision log.

## Intent and why

CampusQuest is feature-complete per the product spec and deployed, but carries
**zero scale evidence**: no load tests, no performance numbers, no
observability dependencies (verified 2026-10-29: no load tooling in the
Makefile or backend deps; no prometheus/otel/sentry in `backend/pyproject.toml`).

The project's goal is now a **portfolio for architecture-flavored backend
internships**. Interviews reliably probe: "how many users can it hold, where
does it break, how do you know?" — and system-design rounds open with
capacity estimation, not architecture diagrams. This effort turns the repo's
existing correctness discipline (PG constraints/locks, idempotency, immutable
history) into scale evidence, and doubles as deliberate rehearsal of the
capacity-estimation method.

Secondary honest goal: shake out real concurrency bugs locally before
production hits them. Production runs real recruitment-collection tasks
today; a lost submission there is a real incident.

We are doing this *smarter, not harder* (owner's framing): capacity
estimation first, scenario-as-code, correctness assertions under load, and a
phase-2 path to a CI performance budget — not a one-off script and a
screenshot.

## Goals

1. A reproducible locust suite covering three scenarios (S1 primary, S2, S3
   baseline) that runs against the local compose stack on demand.
2. An honest one-page capacity report: estimation method, targets, results
   table, bottlenecks found, one fix round with before/after numbers, and the
   hardware the numbers were produced on.
3. **Post-test DB correctness assertions** — the centerpiece: zero lost or
   duplicate submissions, zero over-claimed slots, deadline checks fair under
   race. This proves the existing guarantees hold under load, on PostgreSQL,
   which is what this repo is actually good at.
4. A **breaking point** for each headline scenario, via a step-ramp
   escalation leg that increases load until degradation or failure — the
   second-most-asked interview number ("where does it break"). "Comfortably
   above target" is a valid pass-line result *only together with* the
   measured breaking point.
5. One ADR ("how and why we load test") — the interview narrative's written
   form.
6. A phase-2 *sketch only*: performance budget in CI, ratchet-style, mirroring
   the existing coverage-ratchet philosophy. Not built in this phase.

## Non-goals

- No **product-semantics** changes. Performance fixes that preserve semantics
  ARE in scope (goal 2's fix round), riding full repo discipline: regression
  tests, module gate, review, contract regen if applicable. (Clarified after
  builder check: the earlier "no product changes" contradicted the fix-round
  goal.)
- No observability stack (Prometheus/Grafana/tracing) — that is track B,
  sequenced after this one.
- Never load-test the production server. Production serves real tasks.
- No distributed load generation. Single load generator; numbers are honest
  single-machine numbers with hardware stated.
- No login-rush measurement in phase 1 (see §Auth): the per-IP login limiter
  (50/hour/IP, hardcoded in `backend/app/integrations/rate_limit.py`) makes a
  single-generator login rush impossible — that is the limiter working as
  designed, and the report says so. Numbers describe the post-auth path.
- No CI integration in phase 1 (sketch only); the suite is on-demand tooling.

## Scenarios

All scenarios run against a local stack seeded with synthetic data; per-user
pacing models a human (think time), because the API rate-limits per user and
a robotic zero-delay client would mostly measure our own limiter.

**S1 — deadline submission rush (primary).** The owner's original picture:
"submitting homework must never lag or break." Pre-state (seeded, not
created during the run): one hot task with a **FIXED deadline** (spec §9.1)
whose window closes shortly after the run's burst phase; hundreds of claims
pre-seeded with their deadline snapshots — pre-seeding is mandatory because
§9.1 stops new claims within 4 hours of a FIXED deadline, and
`upload-intent` requires an existing `claim_id`
(`backend/app/modules/submissions/router.py:396-404`). Reward-tier assertions
follow the FIXED oracle (spec §9.3).

Per student: authenticated task list/detail → `POST /submissions/upload-intent`
→ PUT a representative file to MinIO (upload bandwidth reported separately
from API latency) → `POST /submissions/upload-complete`. Load concentrates in
the final window. The suite intentionally includes:

- client retries replaying the same intent — exercises §32 idempotency
  (replay returns the SAME submission) under load;
- last-second submissions — exercises deadline checks under race;
- the async validation pipeline depth after the burst (dispatcher/worker
  drain time);
- a step-ramp escalation leg past the target until degradation/failure,
  yielding the breaking point.

**File profile (amended 2026-10-30 during implementation):** the product
forbids mixing structured (CSV/XLSX/SQLITE) and document (DOCX/PDF) types
on one task (publish validation) — discovered by the seeder's first
failing test. The hot task is therefore a **structured** task
(CSV/XLSX/SQLITE), which also mirrors production reality (recruitment
data collection). Weighted mix: 60% CSV (0.1–2 MB), 30% XLSX (2–10 MB),
10% SQLITE (10–50 MB) — none approaching the 200 MB cap. Weights
adjustable before the headline run.

**S2 — claim rush (second act).** `POST /tasks/{task_id}/claim` with limited
slots: N racers for S slots at task-open (no pre-seeded claims for racers;
this scenario tests the claim path itself, within its 20/min/user limit at
human pacing). Assertions: no oversell (slot count constraint), no lost
claims, losers get clean rejections. Escalation leg included.

**S3 — read baseline (control leg).** Rankings, task list, session refresh at
moderate concurrency; gives the report a comparison axis so write-path
numbers have context.

## Auth in the suite

Tokens are minted offline at seed time (extend `backend/scripts/seed_*`
tooling; precedent: `mint_demo_recovery_codes.py`), keeping login entirely
out of S1/S2 measurement. Access tokens live 15 min, so the suite re-mints or
refreshes as a bookkeeping step, not a measured leg. The report states
plainly: login capacity is unmeasured in phase 1, by design (per-IP limiter,
single generator).

## Environment and data

- Local compose stack: `docker compose -f infra/docker-compose.yml up -d`
  (postgres/redis/minio) with fresh volumes for load runs; app + worker
  started by the suite runner. Never prod.
- **Canonical SUT shape** — the headline numbers describe a pinned,
  production-like process form, recorded in the report: production topology
  confirmed via the ops handover doc (Caddy → Next + uvicorn + Celery +
  PostgreSQL/Redis/MinIO). Exact worker counts are confirmed against the
  server's systemd units during implementation if reachable; otherwise the
  suite pins explicit values (uvicorn worker count, Celery concurrency, beat
  on/off) and states them as the tested shape.
- Seeding reuses `backend/scripts/seed_demo_accounts.py` /
  `seed_demo_content.py`, extended for scale (hundreds–thousands of students,
  the hot task, pre-seeded claims). Follow the e2e stack protocol for ports/
  tokens/cleanup; load runs use their own world and never touch shared e2e
  state.
- Hardware recorded in the report.

## Measurement without an observability stack

Track B (observability) does not exist yet, so phase 1 measures with what is
already here:

- locust stats: RPS, P50/P95/P99, failure counts (429 from our own limiter
  counted separately from errors);
- Postgres: slow query log / `pg_stat_statements` + `EXPLAIN` on top
  offenders;
- ad-hoc sampling profiler (e.g. `py-spy` via `uvx`, no repo dependency) on
  API and worker processes during hot runs;
- async pipeline drain time observed from DB state (pending → done).

Every step scripted so a rerun reproduces the same evidence.

## Targets (agent-derived default; owner may adjust before the headline run)

Method (fixed): start from real campus scale — plausible task participation
at course/college level → fraction submitting in the final minutes →
concurrent submitters; claim rush sized the same way.

Derived default (2026-10-29): school scale ~30k students → a large
general-education course task with **1000 enrollees**; procrastination
concentration puts ~100 submitters in the final 10 minutes (realistic
in-flight concurrency ~10–20; the suite targets **50 concurrent active
users** to leave headroom — the escalation leg measures the true breaking
point); S2 = **300 racers for 60 slots** opening at one fixed second. Pass
line: 0 unexpected errors, zero lost/duplicate submissions, no oversell,
API-leg P99 ≤ 2 s (upload bandwidth reported separately). The report states
targets, actuals, breaking points, and the delta honestly — "comfortably
above target" is a valid result when paired with the measured breaking point.

## Dependencies

- `locust` as a backend dev dependency. Verified nothing in the repo covers
  load testing today; chosen for Python-ecosystem consistency and reuse of
  seed tooling/env conventions.

## Repo conventions this work must follow

- Invoke the campusquest-test-process skill when building the suite (it is
  test infrastructure); load suites stay out of pre-push/CI lanes in phase 1.
- No API surface change is planned; if any fixture forces one, contract
  artifacts regen rides in the same PR (test-artifacts manifest, G19).
- New integration-style test files carry `pytestmark` if they land under the
  pytest tree (repo trap on record).
- Performance fixes ride the standard PR/gate/review pipeline; docstring
  edits flip contract freshness gates (two incidents on record).

## Acceptance criteria

- A documented entrypoint (e.g. `make loadtest-s1` or equivalent scripts/
  doc) reproduces a full run end-to-end on a fresh stack.
- The capacity report exists with: estimation, targets, results, ≥1
  identified bottleneck with a semantics-preserving fix, before/after
  numbers, breaking point per headline scenario, correctness assertions
  green, SUT shape and hardware stated.
- ADR written; scripts and report in the repo; production untouched.
- Targets and file profile reflect the owner's accepted defaults (any owner
  adjustment lands before the headline run).

## Decision log

- Confirmed by owner: portfolio-first identity; S1 as the primary picture;
  "make the load-testing work itself smarter" (estimation-first, phase-2
  ratchet); continue with this track.
- My calls, owner can veto (builder-check round 1 resolutions):
  - offline token minting; login excluded from phase-1 numbers (Q1);
  - canonical production-like SUT shape, pinned and stated (Q2);
  - performance fixes in scope; non-goal narrowed to product-semantics (Q3);
  - step-ramp escalation legs for breaking points; soak optional/stretch (Q4);
  - S1 pre-state: FIXED deadline + pre-seeded claims + §9.3 FIXED oracle (Q5).
- Owner decisions 2026-10-29: fix round approved (Q3, cost acknowledged);
  file profile — all homework families occur (mix weights = agent call);
  targets — agent-derived default accepted as working anchor, adjustable
  before the headline run.
- No open owner items. Next: superpowers:writing-plans.
- Builder-check round 1 (2026-10-29): 6 questions, all resolved above; no
  second round planned.
