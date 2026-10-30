# Load Testing & Capacity Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the locust suite, seeded world, and correctness checkers that produce CampusQuest's first capacity report with breaking points (spec §Goals), per the approved design.

**Architecture:** On-demand load tooling under `backend/loadtest/` (outside pytest lanes) driven by a Makefile section; a seed script builds the world and mints tokens offline through the existing session-issuance code path; three locust scenarios (S1 deadline rush primary, S2 claim rush, S3 read baseline) plus post-run SQL checkers that assert the repo's correctness guarantees held under load.

**Tech Stack:** locust (new, dev-group only), FastAPI/uvicorn live app, Celery worker, PostgreSQL 15432 via `infra/docker-compose.yml`, MinIO presigned PUT.

**Spec:** `docs/superpowers/specs/2026-10-29-load-testing-design.md` — the plan argues from it; read it first.

## Global Constraints

- Local compose stack only; **never** point anything at production. DB default: `postgresql+asyncpg://test:test@localhost:15432/campusquest_test` (Makefile `TEST_STACK_ENV`, line ~178).
- API under test: `http://localhost:8000/api/v1` (uvicorn `app.main:create_app --factory`, e2e precedent `frontend/playwright.config.ts:144`).
- locust is the **only** new dependency (`[dependency-groups] dev` in `backend/pyproject.toml`). py-spy if used: `uvx py-spy`, no repo dep.
- No product-semantics changes. Any performance fix: failing regression test first, module gate, review (spec §Non-goals).
- Load suite stays out of pre-push/CI lanes (spec). Invoke the campusquest-test-process skill when touching test infrastructure.
- New pytest files under `tests/` carry `pytestmark = pytest.mark.integration` (repo trap: silent deselect otherwise).
- Ruff applies to everything under `backend/`; mypy strict scope is `app/` only (loadtest scripts excluded by config — do not widen).
- Repo commit style: `feat(loadtest): …` / `test(loadtest): …`, no AI trailer.
- Run artifacts land in `backend/loadtest/runs/<UTC-timestamp>/` — gitignored; numbers live in the report.
- Rate-limit reality to design around: `auth:login-ip` 50/h/IP (why login is offline-minted), `submissions:upload-intent` 10/min/user, `tasks:claim` 20/min/user (`backend/app/integrations/rate_limit.py`).

## Review Focus

1. **§32 replay**: a retried `upload-complete` after success must return the SAME submission, not a duplicate — pinned by Task 5 dry-run step + Task 6 checker (`submissions_per_claim` uniqueness).
2. **Deadline race**: submissions before deadline_at accepted, after rejected — never both-ways flaky. Pinned by Task 5 last-second cohort + Task 6 assertion on `created_at` vs claim `deadline_at`.
3. **Oversell/fairness (§8.2, §31.4)**: one active `AssignmentClaim` per `Assignment`; losers get stable `AssignmentLimitReachedError`/`NoAssignmentAvailableError` codes. Pinned by Task 7 checker.
4. **Token expiry mid-run**: access tokens live 15 min; escalation legs run longer. Pinned by Task 3 auto-refresh-on-401 with its own stat, unit-tested.
5. **Limiter pollution**: 429 must be counted as its own class, never as scenario failure — else all headline numbers are lies. Pinned by Task 3 classifier, unit-tested, used by every scenario.

---

### Task 1: Stack runner and loadtest env module

**Files:**
- Create: `backend/loadtest/__init__.py` (empty), `backend/loadtest/env.py`
- Modify: `Makefile` (append a `# --- loadtest ---` section at the end)
- Modify: `backend/.gitignore` (or root `.gitignore`) — add `loadtest/runs/`

**Interfaces:**
- Produces: `loadtest_env() -> LoadtestEnv` in `backend/loadtest/env.py` — dataclass with `api_base: str` (default `http://localhost:8000/api/v1`) and `database_url: str` (TEST_STACK_ENV default); env vars override (`CQ_LOADTEST_API_BASE`, `DATABASE_URL`).
- Produces Makefile targets: `loadtest-stack-up`, `loadtest-stack-down`.

- [ ] **Step 1: Implement `backend/loadtest/env.py`** — dataclass + `loadtest_env()` reading the two env vars with the defaults above. Ruff-clean (`cd backend && uv run ruff check loadtest`).

- [ ] **Step 2: Add Makefile targets** — `loadtest-stack-up`: `docker compose -f infra/docker-compose.yml up -d`, then `cd backend && $(TEST_STACK_ENV) uv run alembic upgrade head`, then start the SUT: `uv run uvicorn app.main:create_app --factory --port 8000 --workers 2` and `uv run celery -A app.workers.celery_app worker --loglevel=INFO` in background (check `app/workers/celery_app.py` for a beat schedule; if one is configured, also start `... beat`); if `app` needs the stack env exported, wrap with `$(TEST_STACK_ENV)` like BACKEND_INTEGRATION_CMD. `loadtest-stack-down` kills the two processes and `docker compose ... down`. Pinned SUT shape for the report: uvicorn 2 workers + 1 Celery worker; write it into `loadtest/runs/SUT-SHAPE.md` whenever a run is made (Task 8 formalizes).

- [ ] **Step 3: Verify the stack comes up**

Run: `make loadtest-stack-up && sleep 5 && curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/api/v1/auth/login -H 'content-type: application/json' -d '{}'`
Expected: uvicorn logs "Application startup complete", Celery logs readiness, and curl prints `4xx` (401/422 — any 4xx proves routing alive; docs/openapi may be disabled by settings, which is why the login route is the probe). Then `make loadtest-stack-down`.

- [ ] **Step 4: Commit**

```bash
git add backend/loadtest/ Makefile .gitignore
git commit -m "feat(loadtest): stack runner and env module for the capacity suite"
```

### Task 2: Seed the load world and mint tokens offline

**Files:**
- Create: `backend/scripts/seed_load_world.py`
- Test: `backend/tests/integration/test_seed_load_world.py`

**Interfaces:**
- Consumes: `TEST_STACK_ENV` env (DB/Redis/S3), models `app.modules.identity.models.User`, `app.modules.tasks.models.{Task, Assignment, AssignmentClaim}`, claim snapshot fields (`base_reward_points_snapshot`, `deadline_at`, `grace_deadline_at`, `reward_policy_snapshot`, `submission_schema_version`), session issuance in `app/modules/identity/session_service.py` (the method whose body calls `self._codec.encode` and persists the `UserSession` — the same code path the login route calls; locate it once, call it directly; bypassing HTTP bypasses the per-IP login limiter by construction).
- Produces: CLI `uv run python scripts/seed_load_world.py --students N --claim-slots M --race-users K --deadline-in-minutes X --out-dir loadtest/runs/<ts>` and `world.json`:
  `{"api_base": str, "task_id": str, "deadline_at": iso8601, "slots_total": int, "users": [{"user_id": str, "username": str, "access_token": str, "refresh_token": str, "claim_id": str|null}]}` — first M users get `claim_id` (S1 submitters), remaining K have `claim_id: null` (S2 racers).

- [ ] **Step 1: Write the failing integration test**

```python
pytestmark = pytest.mark.integration
```
Seed a tiny world (`--students 3 --claim-slots 2 --race-users 1 --deadline-in-minutes 30`). Assert: the Task is PUBLISHED with a FIXED deadline; `Assignment` count == claim-slots; every pre-seeded `AssignmentClaim` carries all five snapshot fields non-null; S1 users' `claim_id` set; a minted token authenticates — `GET` the authenticated profile route with `Authorization: Bearer <token>` returns 200 (exact route from `app/modules/identity/profile_router.py`).

- [ ] **Step 2: Run it to verify it fails**

Run: `make loadtest-stack-up` then `cd backend && $(TEST_STACK_ENV equivalent) uv run pytest tests/integration/test_seed_load_world.py -v` (copy the env prefix from the Makefile line)
Expected: FAIL (script doesn't exist).

- [ ] **Step 3: Implement the seed script** — follow `seed_demo_accounts.py`'s pattern (sys.path insert, `get_settings`, async engine, idempotency: refuse if the load task slug already exists unless `--force`). Build the PUBLISHED task via the task service's publish path so publish-time invariants (file policy, schema version, deadline policy) hold — direct ORM only for the bulk `Assignment` rows and pre-seeded `AssignmentClaim`s, mirroring `claim_service`'s snapshot writes (one active claim per Assignment, §31.4). Mint tokens through the session-issuance method per user; never log token values except the out-file. Write `world.json`.

- [ ] **Step 4: Run test to verify it passes**

Run: same pytest command. Expected: PASS. Then `make loadtest-stack-down`.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/seed_load_world.py backend/tests/integration/test_seed_load_world.py
git commit -m "feat(loadtest): seed load world with pre-seeded claims and offline-minted tokens"
```

### Task 3: locust common harness + S3 read baseline

**Files:**
- Create: `backend/loadtest/common.py`, `backend/loadtest/s3_baseline.py`
- Test: `backend/tests/unit/loadtest/test_common.py` (pure-logic unit tests; no pytestmark needed — unit)
- Modify: `Makefile` (`loadtest-s3`)

**Interfaces:**
- Consumes: `world.json` (Task 2).
- Produces: `backend/loadtest/common.py`:
  - `class CampusQuestUser(locust.HttpUser)` — loads a user from world.json on start; sets `Authorization: Bearer`; on 401, refreshes via the refresh route once and retries the request once; classifies responses via `classify()`;
  - `classify(response) -> None` — with `locust`'s `catch_response`: 2xx pass; 429 → `response.failure("rate-limited")` tagged so the summary counts it separately (`FAILURE_RATE` must be reported excluding 429 — write `summary.json` with `ok_count, err_count, rate_limited_429, p99_ms`);
  - `write_summary(environment, run_dir, scenario) -> Path` — locust stats to `runs/<ts>/<scenario>/summary.json` + locust `--csv` files beside it.
- Produces: `loadtest-s3` target: seed world (if missing) → `uv run locust -f loadtest/s3_baseline.py --headless -u 20 -r 5 -t 2m --csv …`.

- [ ] **Step 1: Write failing unit tests for `classify` and the 401-refresh logic** — feed fake response objects (status codes 200/429/401/500; a stub `client` whose refresh call flips the token) and assert: 429 counted only in `rate_limited_429`; 401 triggers exactly one refresh+retry then gives up as `err`; 500 → `err`. These pin Review Focus items 4 and 5.

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run pytest tests/unit/loadtest/test_common.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `common.py` and `s3_baseline.py`** — S3 task set: GET task list, GET task detail, GET rankings (exact paths from `tasks/router.py` GET routes and `rankings/router.py`), think time 3–10 s, 20 users. On start each user picks its identity round-robin from world.json.

- [ ] **Step 4: Unit tests pass**

Run: same pytest command. Expected: PASS.

- [ ] **Step 5: Live verification**

Run: `make loadtest-stack-up && make loadtest-s3`
Expected: run completes in ~2 min; `runs/<ts>/s3/summary.json` shows `err_count == 0`, `p99_ms` present, 429 count 0 (read path at 20 users should not trip limiters; if it does, that's a finding — record it).

- [ ] **Step 6: Commit**

```bash
git add backend/loadtest/common.py backend/loadtest/s3_baseline.py backend/tests/unit/loadtest/test_common.py Makefile
git commit -m "feat(loadtest): token-aware locust harness and S3 read baseline"
```

### Task 4: Synthetic file generator (declared_type families)

**Files:**
- Create: `backend/loadtest/files.py`
- Test: `backend/tests/unit/loadtest/test_files.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `synthetic_file(rng: random.Random, declared_type: str, target_bytes: int) -> bytes` for the five families CSV/XLSX/SQLITE/DOCX/PDF (real-enough payloads: CSV/XLSX must be parseable by the validation pipeline — XLSX via openpyxl already a dep; DOCX/PDF minimally valid containers; SQLITE a real tiny sqlite3 db); `pick_profile(rng: random.Random) -> tuple[str, int]` implementing the mix: 60% DOCX/PDF 0.1–2 MB, 30% CSV/XLSX 2–10 MB, 10% XLSX/SQLITE 10–50 MB (spec §Scenarios S1).

- [ ] **Step 1: Write failing unit tests** — with a seeded rng over 500 draws: family frequencies within ±8pp of the mix; every `synthetic_file` output: size within ±10% of target; CSV parses with ≥1 header row; XLSX opens via openpyxl; declared_type never outside the five.

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run pytest tests/unit/loadtest/test_files.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `files.py`**

- [ ] **Step 4: Tests pass**

Run: same. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/loadtest/files.py backend/tests/unit/loadtest/test_files.py
git commit -m "feat(loadtest): synthetic homework-file generator for the five declared_type families"
```

### Task 5: S1 deadline submission rush scenario

**Files:**
- Create: `backend/loadtest/s1_deadline.py`
- Modify: `Makefile` (`loadtest-s1`)

**Interfaces:**
- Consumes: `CampusQuestUser`, `write_summary` (Task 3); `synthetic_file`, `pick_profile` (Task 4); `world.json` users with `claim_id` (Task 2); endpoints `POST /submissions/upload-intent` → PUT `upload_url` with the returned signed headers + `pinned_content_length` → `POST /submissions/upload-complete` (`submissions/router.py:377,414`).
- Produces: run dirs `runs/<ts>/s1/…` with summary.json including `submit_ok`, `submit_err`, `rate_limited_429`, replay counters, and upload-leg timings kept separate from API-leg timings (locust request names: `s1:intent`, `s1:put`, `s1:complete`).

- [ ] **Step 1: Implement the task set** — each virtual user: think 5–15 s → `s1:intent` → generate file → `s1:put` real bytes to MinIO with the signed headers → `s1:complete`. Retry policy: on any non-2xx from `complete` other than 4xx-business, replay the SAME intent once (exercises §32). Last-second cohort: 20% of users wait until `deadline_at - 5 s` then submit (deadline race leg). Escalation leg = separate Makefile invocation with stepped `-u` (50 → 100 → 200 → …) until err rate > 1% or P99 > 5 s; each step is its own run dir.

- [ ] **Step 2: Dry-run verification (5 users, tiny world)**

Run: `make loadtest-stack-up`, seed small world, `uv run locust -f loadtest/s1_deadline.py --headless -u 5 -r 1 -t 1m` (or via a `loadtest-s1-dryrun` target)
Expected: `submit_ok == 5`; manual replay check: rerun one user's complete → same submission id (§32 visible); validation_status eventually terminal (worker drains).

- [ ] **Step 3: Commit**

```bash
git add backend/loadtest/s1_deadline.py Makefile
git commit -m "feat(loadtest): S1 deadline-rush scenario with replay and last-second legs"
```

### Task 6: S1 post-run correctness checker

**Files:**
- Create: `backend/loadtest/check_s1.py`
- Test: `backend/tests/integration/test_check_s1.py`

**Interfaces:**
- Consumes: `world.json`, run summary; models `submissions.Submission`, `AssignmentClaim`; DB via `TEST_STACK_ENV`.
- Produces: CLI `uv run python -m loadtest.check_s1 --run-dir <dir>`; exit 0 = all green, 1 = violation; writes `checks.json` into the run dir. Checks: (a) one Submission max per (claim, intent) — replay created no dup; (b) `submit_ok` (summary) == new Submission rows — nothing lost; (c) every Submission `created_at <= claim.deadline_at` or inside grace window per §6 (24 h CHECK-pinned grace) — deadline fair; (d) all Submissions reached terminal `validation_status` within the drain window — record drain seconds; (e) reward tiers: sample 20 claims' ledger entries against the §9.3 FIXED oracle (100/80/50/20% by submit time vs deadline).

- [ ] **Step 1: Write the failing integration test** — seed tiny world, run a 3-user S1 dry-run via the scenario's task functions (or drive the endpoints directly), then plant anomalies in a second DB world: one duplicate Submission row variant and one post-deadline row; assert `check_s1` exits 1 and `checks.json` names exactly the planted violations on the planted world, and exits 0 on the clean world.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && $(TEST_STACK_ENV …) uv run pytest tests/integration/test_check_s1.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `check_s1.py`**

- [ ] **Step 4: Test passes**

Run: same. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/loadtest/check_s1.py backend/tests/integration/test_check_s1.py
git commit -m "feat(loadtest): S1 correctness checker — loss/dup/deadline/tier assertions"
```

### Task 7: S2 claim rush scenario + checker

**Files:**
- Create: `backend/loadtest/s2_claim.py`, `backend/loadtest/check_s2.py`
- Test: `backend/tests/integration/test_check_s2.py`
- Modify: `Makefile` (`loadtest-s2`)

**Interfaces:**
- Consumes: `CampusQuestUser` (Task 3); world.json users with `claim_id: null`; `POST /tasks/{task_id}/claim` (`tasks/router.py:569`); loser codes `AssignmentLimitReachedError` / `NoAssignmentAvailableError` (`tasks/claim_service.py:144,152`).
- Produces: `runs/<ts>/s2/…`; checker `python -m loadtest.check_s2 --run-dir <dir>` exit 0/1 + `checks.json`. Checks: (a) active `AssignmentClaim` count == `slots_total` (== min(slots, racers)) — no oversell; (b) no user holds 2 claims on this task; (c) every loser response carried a stable BusinessError code (assert from summary.json's classified errors), no 5xx among losers; (d) `Assignment` occupancy: exactly slots_total rows in an occupied status.

- [ ] **Step 1: Write the failing integration test for `check_s2`** — two seeded worlds: clean (racers 3, slots 2 → expect exactly 2 claims, checker green) and planted (a second claim for one user forced through ORM bypassing the service — if the DB unique/partial constraint blocks the plant, assert THAT instead: the constraint itself is the guarantee) → checker names the violation or the constraint held.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && $(TEST_STACK_ENV …) uv run pytest tests/integration/test_check_s2.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `s2_claim.py`** — the open-gate lives in locust, not in the seed: all racers wait until a common `t0 = now + 10 s` (computed at swarm start), then each fires `POST /tasks/{task_id}/claim` exactly once; one retry only on 5xx. Then implement `check_s2.py`.

- [ ] **Step 4: Test passes; live verification**

Run: pytest PASS; then `make loadtest-s2` with a 30-racer/10-slot smoke world: summary shows ~10 ok / ~20 clean business rejections, 0 5xx; checker exit 0.

- [ ] **Step 5: Commit**

```bash
git add backend/loadtest/s2_claim.py backend/loadtest/check_s2.py backend/tests/integration/test_check_s2.py Makefile
git commit -m "feat(loadtest): S2 claim-rush scenario and oversell/fairness checker"
```

### Task 8: Headline runs, fix round, capacity report, ADR

**Files:**
- Create: `docs/loadtest/capacity-report-2026-10.md`, `docs/architecture/adr/0001-load-testing.md`, `backend/loadtest/runs/SUT-SHAPE.md`
- Modify: whatever the one fix round touches (discovered at runtime — see Step 3)

**Interfaces:**
- Consumes: everything above; targets `loadtest-s1/s2/s3` at spec §Targets: S1 = 50 concurrent submitters (target) + escalation 50→100→200→…; S2 = 300 racers / 60 slots; S3 = 20 users baseline.

- [ ] **Step 1: Enable DB slow-query logging on the load stack** — compose PG with `log_min_duration_statement=200` (compose override file `infra/docker-compose.loadtest.yml`, do not touch the shared compose file); note in SUT-SHAPE.md. Restart stack, confirm slow lines appear in `docker logs`.

- [ ] **Step 2: Run the matrix** — fresh seed per scenario; for each: run → checker → keep run dir; escalation until break (>1% err or P99>5 s). Record: targets vs actuals vs breaking points; slow-query top offenders; py-spy flamegraph of uvicorn+celery at the breaking step (`uvx py-spy record -- …`, save svg into run dir).

- [ ] **Step 3: Fix round (exactly one, TDD)** — pick the highest-leverage finding; write the failing regression test FIRST; fix preserving semantics; run the module gate + `make pre-push`; re-run the affected scenario head-to-head; before/after numbers into the report. If nothing actionable emerges, that is a valid outcome — say so in the report and skip the fix PR (spec allows "comfortably above target" + breaking point).

- [ ] **Step 4: Write the report** — one page: estimation → targets → results table (per scenario: load, RPS, P50/P95/P99, err/429, checker verdict, breaking point) → bottleneck analysis + fix before/after → hardware + SUT shape → honest limitations (login unmeasured, single generator, API-level). Write the ADR: why estimation-first, why scenario-as-code, why correctness assertions are the centerpiece, phase-2 CI budget sketch.

- [ ] **Step 5: Final verification** — `make loadtest-s1` on a fresh stack end-to-end reproduces (spec acceptance); `make pre-push` green; diff contains no run artifacts (runs/ ignored), no secrets, no TODOs.

- [ ] **Step 6: Commit**

```bash
git add docs/loadtest/ docs/architecture/adr/ backend/loadtest/runs/SUT-SHAPE.md
git commit -m "docs(loadtest): first capacity report with breaking points and ADR 0001"
```
