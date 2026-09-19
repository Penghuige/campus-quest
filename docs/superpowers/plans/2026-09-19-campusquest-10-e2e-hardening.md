# CampusQuest 10 End-to-End Hardening & Release Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the integrated CampusQuest V1 satisfies the approved specification under normal flows, exact time boundaries, concurrency, worker retries, malicious files, Redis loss, permissions, and clean-database deployment.

**Architecture:** This plan adds no new product domain. It composes existing services through public APIs/workers and adds regression/load/security fixtures that become the final release gate.

**Tech Stack:** pytest, pytest-asyncio, PostgreSQL, Redis, Celery eager/test worker plus real worker smoke test, MinIO/S3-compatible storage, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

## Global Constraints

- A release is blocked by any failed invariant test.
- Test failures are fixed at the owning domain service, not patched around in E2E.
- Fresh database migration from base to head must succeed.
- Redis may be flushed and rebuilt without losing authoritative state.
- All core concurrency tests use real PostgreSQL transactions.
- Security fixtures must be bounded and safe; no test executes untrusted uploaded code.

## Review Focus

1. Submit/expire boundary race.
2. Same-user quota and same-Assignment allocation races.
3. Wallet/stock double-spend races.
4. Worker duplicate/retry behavior.
5. Unicode/time-zone/deadline exact boundaries.

---

### Task 1: Build Deterministic Full-System Test Fixtures

**Files:**
- Create: `backend/tests/e2e/conftest.py`
- Create: `backend/tests/e2e/factories.py`
- Create: `backend/tests/e2e/clock_control.py`
- Create: `frontend/e2e/fixtures.ts`

**Interfaces:**
- Produces helpers to create Student/Teacher/Admin, whitelist, Task, Assignments, RewardItem, fake notification adapters, and FrozenClock control.

- [ ] **Step 1: Write fixture smoke test**

Create all three roles, one Task with three Assignments, and one RewardItem; assert fixture IDs are independent and teardown restores clean DB.

- [ ] **Step 2: Implement no-production guard**

Fixture startup must reject database/storage endpoints that do not match explicit test environment naming/flag.

- [ ] **Step 3: Run and commit**

```bash
git add backend/tests/e2e frontend/e2e/fixtures.ts
git commit -m "test: add deterministic CampusQuest e2e fixtures"
```

### Task 2: Implement the Normal Happy-Path E2E

**Files:**
- Create: `backend/tests/e2e/test_happy_path.py`
- Extend: `frontend/e2e/submission.spec.ts`
- Extend: `frontend/e2e/rewards-ranking.spec.ts`

**Interfaces:**
- Consumes only public/service-supported flows.

- [ ] **Step 1: Write backend/API E2E**

```text
seed whitelist
-> phone verify
-> register
-> login
-> Teacher publishes Task/Assignments
-> Student claims
-> upload valid CSV
-> worker validates
-> Teacher approves
-> one reward ledger entry
-> wallet/ranking update
-> redeem RewardItem
-> Admin approves
-> Admin fulfills
```

Assert every expected state and ledger/reservation transition.

- [ ] **Step 2: Run test and fix owning modules only**

Run: `cd backend && pytest tests/e2e/test_happy_path.py -v`.

- [ ] **Step 3: Run browser counterpart**

Ensure Student-visible labels and Teacher/Admin actions match backend state.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/e2e/test_happy_path.py frontend/e2e
git commit -m "test: cover CampusQuest full happy path"
```

### Task 3: Implement Late, Revision, Empty-Shell, and Expiry E2E

**Files:**
- Create: `backend/tests/e2e/test_deadline_flows.py`

**Interfaces:**
- Exercises Clock, Task, Submission, Review, Points.

- [ ] **Step 1: Add +2h late case**

Expect 80% locked and exact integer points after approval.

- [ ] **Step 2: Add late Teacher review revision case**

On-time valid submit; Teacher reviews two days later; revision deadline >= review +24h; revised approval keeps 100%.

- [ ] **Step 3: Add invalidated empty-shell case**

On-time machine-pass shell -> invalidate -> valid submit +7h -> 50% reward only.

- [ ] **Step 4: Add no-submit expiry/reallocation case**

Grace expires -> Claim EXPIRED -> Assignment AVAILABLE -> different Student claims it.

- [ ] **Step 5: Run and commit**

```bash
cd backend && pytest tests/e2e/test_deadline_flows.py -v
git add backend/tests/e2e/test_deadline_flows.py
git commit -m "test: cover late revision invalidation and expiry flows"
```

### Task 4: Implement Exact Time Boundary Matrix

**Files:**
- Create: `backend/tests/e2e/test_time_boundaries.py`

**Interfaces:**
- Exercises `reward_fraction`, submission acceptance, business-day/month behavior.

- [ ] **Step 1: Parameterize all reward instants**

Test `deadline ±1ms`, `+4h ±1ms`, `+12h ±1ms`, `grace ±1ms` plus exact instants.

- [ ] **Step 2: Add business timezone rollover**

Test abandon quota and daily/monthly ranking at local 23:59:59.999 and 00:00:00.

- [ ] **Step 3: Add DST test timezone**

Set test business timezone to a DST-observing zone and assert local period grouping is zone-aware, proving no hardcoded +8 arithmetic.

- [ ] **Step 4: Run and commit**

```bash
git add backend/tests/e2e/test_time_boundaries.py
git commit -m "test: pin CampusQuest time and timezone boundaries"
```

### Task 5: Implement Concurrency Release Gate

**Files:**
- Create: `backend/tests/e2e/test_concurrency_gate.py`

**Interfaces:**
- Exercises real PostgreSQL transactions.

- [ ] **Step 1: Add 50 users / 10 assignments test**

Exactly ten successful unique claims; forty controlled conflicts; zero 500s.

- [ ] **Step 2: Add same-user quota race**

User has two quota-consuming Claims; race two additional Tasks; exactly one new Claim.

- [ ] **Step 3: Add same-Task same-user race**

Two concurrent claims same user/task -> at most one.

- [ ] **Step 4: Add concurrent approve**

Two reviewers -> one reward ledger.

- [ ] **Step 5: Add redemption double-spend and last-stock races**

Balance and inventory never negative.

- [ ] **Step 6: Add submit/expire race**

Use transaction synchronization to force overlap; valid in-window Submission prevents release.

- [ ] **Step 7: Run repeatedly**

```bash
cd backend
for i in 1 2 3 4 5; do pytest tests/e2e/test_concurrency_gate.py -q || exit 1; done
```

Expected: five consecutive clean runs.

- [ ] **Step 8: Commit**

```bash
git add backend/tests/e2e/test_concurrency_gate.py
git commit -m "test: add CampusQuest concurrency release gate"
```

### Task 6: Add Malicious and Pathological File Fixtures

**Files:**
- Create: `backend/tests/fixtures/files/build_fixtures.py`
- Create: `backend/tests/e2e/test_file_security.py`

**Interfaces:**
- Produces bounded generated fixtures at test runtime, not committed giant binaries.

- [ ] **Step 1: Generate safe pathological cases**

Generate:
- binary renamed .csv;
- fake SQLite;
- SQLite multiple tables;
- oversized logical row count with small bounded fixture;
- very long CSV cell;
- XLSX missing target sheet;
- high compression-ratio XLSX/ZIP fixture below CI resource limits.

- [ ] **Step 2: Assert bounded failure codes**

Each parser returns validation failure, not worker crash/MemoryError/process hang.

- [ ] **Step 3: Add formula-injection display/export regression**

Values beginning `= + - @` must never be executed by server and any generated spreadsheet export path must escape them.

- [ ] **Step 4: Run and commit**

```bash
git add backend/tests/fixtures/files/build_fixtures.py backend/tests/e2e/test_file_security.py
git commit -m "test: harden uploaded file validation"
```

### Task 7: Add Worker Retry and External-Failure Gate

**Files:**
- Create: `backend/tests/e2e/test_worker_retries.py`

**Interfaces:**
- Exercises notification, validation, cleanup, ranking workers.

- [ ] **Step 1: Duplicate notification job**

Same job twice -> one delivery.

- [ ] **Step 2: Provider timeout sequence**

Three configured failures -> FAILED and bounded attempts; business Task/Claim state unchanged.

- [ ] **Step 3: Duplicate validation job**

One report/current state only.

- [ ] **Step 4: Duplicate cleanup job**

Already-deleted object is reconciled without metadata loss.

- [ ] **Step 5: Worker crash simulation**

Raise after external fake call but before retry completion; rerun with provider idempotency key and assert no duplicate externally recorded message.

- [ ] **Step 6: Run and commit**

```bash
git add backend/tests/e2e/test_worker_retries.py
git commit -m "test: verify retry-safe CampusQuest workers"
```

### Task 8: Add Redis Loss and Ranking Reconstruction Gate

**Files:**
- Create: `backend/tests/e2e/test_ranking_recovery.py`

**Interfaces:**
- Exercises ledger -> Redis rebuild.

- [ ] **Step 1: Seed multi-period rewards and reversal**

Capture daily/monthly/all/around-me responses.

- [ ] **Step 2: Flush ranking keys**

Delete all `ranking:*` keys.

- [ ] **Step 3: Run rebuild worker**

Compare all captured responses exactly after rebuild.

- [ ] **Step 4: Run and commit**

```bash
git add backend/tests/e2e/test_ranking_recovery.py
git commit -m "test: prove rankings rebuild from PostgreSQL"
```

### Task 9: Add Privacy and Authorization Gate

**Files:**
- Create: `backend/tests/e2e/test_privacy_rbac.py`
- Extend: `frontend/e2e/community.spec.ts`
- Extend: `frontend/e2e/admin.spec.ts`

**Interfaces:**
- Exercises public/student/teacher/admin serializers and routes.

- [ ] **Step 1: Assert public/student responses exclude sensitive fields**

Search serialized JSON for seeded student number, phone, email, object key where forbidden.

- [ ] **Step 2: Assert anonymous comment privacy**

Student and Teacher anonymous moderation views do not expose prohibited identifiers. Admin normal list also stays anonymous.

- [ ] **Step 3: Assert explicit reveal**

Admin reveal with reason succeeds and creates AuditLog. Missing reason fails.

- [ ] **Step 4: Direct-route RBAC matrix**

Student/Teacher/Admin call every sensitive route class and assert expected 2xx/403 matrix.

- [ ] **Step 5: Run and commit**

```bash
git add backend/tests/e2e/test_privacy_rbac.py frontend/e2e
git commit -m "test: enforce privacy and role boundaries"
```

### Task 10: Migration and Clean-Environment Gate

**Files:**
- Create: `scripts/verify-migrations.sh`
- Create: `scripts/verify-clean-start.sh`

**Interfaces:**
- Produces repeatable deployment verification.

- [ ] **Step 1: Implement migration script**

Script creates/uses empty test DB, runs `alembic upgrade head`, validates expected tables/indexes, downgrades to base where supported for test, upgrades again.

- [ ] **Step 2: Implement clean-start script**

Bring Compose down with volumes, start dependencies, migrate, seed test Admin safely, start API/worker/frontend, hit readiness endpoint.

- [ ] **Step 3: Run both scripts**

```bash
bash scripts/verify-migrations.sh
bash scripts/verify-clean-start.sh
```

Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add scripts
git commit -m "test: verify migrations and clean environment startup"
```

### Task 11: Define and Run the Full Release Command

**Files:**
- Modify: `Makefile`
- Create: `docs/operations/release-checklist.md`

**Interfaces:**
- Produces `make release-gate`.

- [ ] **Step 1: Add release target**

It must run, in deterministic order:

```text
backend unit tests
backend integration tests
backend worker tests
backend e2e tests
migration verification
frontend typecheck
frontend lint
frontend unit tests
frontend production build
Playwright e2e
ranking rebuild test
concurrency gate
```

- [ ] **Step 2: Run the full command**

Run:

```bash
make release-gate
```

Expected: exit code 0 and zero failed tests.

- [ ] **Step 3: Review spec section 45 line-by-line**

For each of the 30 acceptance criteria, add a row to `docs/operations/release-checklist.md` naming the automated test or manual deployment check that proves it.

- [ ] **Step 4: Commit**

```bash
git add Makefile docs/operations/release-checklist.md
git commit -m "chore: add CampusQuest V1 release gate"
```

### Task 12: Whole-Branch Review Before Release

**Files:**
- Review: all source, migrations, plan/spec deviations.
- Modify only files required by review findings.

**Interfaces:**
- Produces release candidate.

- [ ] **Step 1: Run a fresh `make release-gate`**

Do not use a previous run as evidence.

- [ ] **Step 2: Inspect schema invariants manually**

Verify actual PostgreSQL indexes/checks for:
- Assignment uniqueness;
- active Claim uniqueness;
- same user/task non-terminal uniqueness;
- ledger reward uniqueness;
- vote/reaction/rating uniqueness;
- notification delivery uniqueness.

- [ ] **Step 3: Inspect high-risk code paths**

Review:
- token/cookie security;
- TOTP enforcement;
- object key authorization;
- SQLite read-only configuration;
- XLSX ZIP limits;
- anonymous reveal audit;
- reward reservation locks;
- Claim expiry locks.

- [ ] **Step 4: Commit review fixes individually**

Each discovered defect gets a regression test and a focused commit. Do not create a success-only empty commit.

- [ ] **Step 5: Re-run `make release-gate` after the final fix**

Only a fresh zero-failure result satisfies the V1 implementation gate.
