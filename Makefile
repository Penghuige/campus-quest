# CampusQuest top-level verification commands.
# Backend tool versions are committed in backend/pyproject.toml (dev group),
# so `uv run` never drifts; frontend tooling is committed in frontend/package.json.

.PHONY: test-backend test-integration lint-backend verify
# Plan 10 task 11's release steps are individually runnable and composed
# by `release-gate` below; .NOTPARALLEL keeps prerequisite order
# deterministic even under `make -j`.
.NOTPARALLEL:

test-backend:
	cd backend && uv run pytest tests/unit tests/workers -v

test-integration:
	cd backend && uv run pytest tests/integration -v -m integration

lint-backend:
	cd backend && uv run ruff format --check app tests alembic
	cd backend && uv run ruff check app tests alembic
	cd backend && uv run mypy

verify: lint-backend
	cd backend && uv run pytest -v
	cd frontend && npm run typecheck && npm run lint && npm run check:css && npm run build

# --- Plan 10 task 11: the V1 release command ------------------------------
#
# `make release-gate` runs, in this order (plan 10 task 11 step 1):
#
#   0. coverage accumulation reset  (coverage-clean: rm backend/.coverage —
#      --cov-append must not inherit an earlier local run's data)
#      + test-database bootstrap     (release-test-db; idempotent
#      `alembic upgrade head` on campusquest_test — the integration/e2e
#      suites need a migrated schema and nothing else creates it on
#      fresh compose volumes)
#   1. backend unit tests            (backend-unit-cov; same suite as
#      backend-unit plus the --cov-append chain feeding step 14)
#   2. backend integration tests     (backend-integration-cov; real
#      PostgreSQL/Redis/MinIO, S3 + composition smokes ON)
#   3. backend worker tests          (backend-worker-cov)
#   4. backend e2e tests             (backend-e2e, CQ_E2E=1)
#   5. migration verification        (migration-verify; dedicated
#      campusquest_migrate_test database: upgrade head -> schema
#      assertions -> downgrade base -> re-upgrade)
#   6. frontend typecheck            (frontend-typecheck)
#   7. frontend lint                 (frontend-lint)
#   8. frontend CSS integrity        (frontend-css-guard)
#   9. frontend unit tests           (frontend-unit)
#  10. frontend production build     (frontend-build)
#  11. Playwright e2e                (playwright-e2e, CQ_E2E=1; the config
#      orchestrates both servers itself)
#  12. ranking rebuild test          — inside backend-e2e:
#      tests/e2e/test_ranking_recovery.py
#  13. concurrency gate              — inside backend-e2e:
#      tests/e2e/test_concurrency_gate.py
#  14. coverage ratchet              (coverage-ratchet; owner-approved
#      addition — backend: the .coverage accumulated by the -cov legs
#      of steps 1-3 vs coverage-ratchet.json floors; frontend: the
#      native-coverage ratchet self-runs its unit suite)
#
# Prerequisites (quality-gates §15; docs/operations/release-checklist.md
# has the operator runbook):
#   - the dependency stack: docker compose -f infra/docker-compose.yml up -d
#     (integration/e2e/migration/Playwright steps all need it);
#   - TEST_STACK_ENV below pins the compose test-stack values as SHELL
#     defaults — an exported real variable still wins, and db_guard's
#     non-test-database refusal stays the last line of defense. Without
#     these defaults a standalone `pytest tests/workers` process has no
#     deployment env (the full-suite run gets it from db_guard's import
#     side effect, CI from the job env), which is exactly the gap the
#     gate's first end-to-end run exposed;
#   - CQ_S3_SMOKE / CQ_COMPOSITION_SMOKE / CQ_E2E are set by the targets
#     themselves, so the gated suites and smokes never silently skip;
#   - the Playwright default ports are frontend 3000 / backend 8000; when
#     either is occupied by an unrelated process, export
#     CQ_E2E_BASE_URL / CQ_E2E_API_URL (WITH the /api/v1 path) to move
#     both servers together — reuseExistingServer would otherwise adopt
#     the wrong process.
TEST_STACK_ENV = DATABASE_URL=$${DATABASE_URL:-postgresql+asyncpg://test:test@localhost:15432/campusquest_test} \
                 REDIS_URL=$${REDIS_URL:-redis://localhost:6379/0} \
                 S3_ENDPOINT_URL=$${S3_ENDPOINT_URL:-http://localhost:9000} \
                 S3_BUCKET=$${S3_BUCKET:-campusquest-test} \
                 S3_ACCESS_KEY=$${S3_ACCESS_KEY:-campusquest} \
                 S3_SECRET_KEY=$${S3_SECRET_KEY:-campusquest-dev} \
                 BUSINESS_TIMEZONE=$${BUSINESS_TIMEZONE:-Asia/Shanghai}

# The three backend suites, single-sourced: the bare targets, the
# coverage-accumulating -cov variants the gate runs, and coverage-baseline
# below all compose these, so suite paths and stack env cannot drift
# between them. COV_APPEND_FLAGS is the accumulate-into-one-.coverage
# chain the step-14 ratchet compares.
COV_APPEND_FLAGS = --cov=app --cov-branch --cov-report= --cov-append
BACKEND_UNIT_CMD = cd backend && $(TEST_STACK_ENV) uv run pytest tests/unit
BACKEND_INTEGRATION_CMD = cd backend && $(TEST_STACK_ENV) CQ_S3_SMOKE=1 CQ_COMPOSITION_SMOKE=1 uv run pytest tests/integration -m integration
BACKEND_WORKER_CMD = cd backend && $(TEST_STACK_ENV) uv run pytest tests/workers

.PHONY: backend-unit backend-integration backend-worker backend-e2e \
        backend-unit-cov backend-integration-cov backend-worker-cov \
        migration-verify frontend-typecheck frontend-lint frontend-css-guard \
        frontend-unit frontend-build playwright-e2e release-test-db release-gate \
        coverage-clean coverage-baseline coverage-ratchet pip-audit

# The gate's self-bootstrapping first step (PR #6 final review P1): the
# integration and e2e suites assume a MIGRATED campusquest_test and
# nothing else creates one on fresh compose volumes (the init script
# only CREATEs the empty database). `alembic upgrade head` is a no-op
# when the schema is already at head, so the target is idempotent and
# costs one version query on warm stacks.
release-test-db:
	cd backend && $(TEST_STACK_ENV) uv run alembic upgrade head

backend-unit:
	$(BACKEND_UNIT_CMD) -v

backend-integration:
	$(BACKEND_INTEGRATION_CMD) -v

backend-worker:
	$(BACKEND_WORKER_CMD) -v

# The gate's coverage-accumulating legs: the SAME suites as the bare
# targets above, plus the --cov-append chain whose accumulated
# backend/.coverage step 14's ratchet compares. release-gate runs
# coverage-clean first so a stale local .coverage (e.g. left by an
# earlier baseline run) can never flatter the floors.
backend-unit-cov:
	$(BACKEND_UNIT_CMD) $(COV_APPEND_FLAGS) -v

backend-integration-cov:
	$(BACKEND_INTEGRATION_CMD) $(COV_APPEND_FLAGS) -v

backend-worker-cov:
	$(BACKEND_WORKER_CMD) $(COV_APPEND_FLAGS) -v

backend-e2e:
	cd backend && $(TEST_STACK_ENV) CQ_E2E=1 uv run pytest tests/e2e -v

migration-verify:
	bash scripts/verify-migrations.sh

coverage-clean:
	rm -f backend/.coverage

# Coverage ratchet (owner-approved 2026-10-07). baseline: the three
# suites with --cov (branch, app/ only, appended into one .coverage) +
# REWRITE the floors (--update is a deliberate, reviewable commit).
# ratchet: compare the accumulated .coverage against the committed
# floors (the CI gate; CI accumulates via --cov-append too), then the
# frontend native-coverage ratchet, which self-runs its unit suite and
# needs no wired input.
coverage-baseline:
	$(BACKEND_UNIT_CMD) $(COV_APPEND_FLAGS) -q
	$(BACKEND_INTEGRATION_CMD) $(COV_APPEND_FLAGS) -q
	$(BACKEND_WORKER_CMD) $(COV_APPEND_FLAGS) -q
	cd backend && uv run python scripts/coverage_ratchet.py --update

coverage-ratchet:
	cd backend && uv run python scripts/coverage_ratchet.py
	cd frontend && npm run coverage:ratchet

pip-audit:
	cd backend && uv run python scripts/pip_audit_gate.py

frontend-typecheck:
	cd frontend && npm run typecheck

frontend-lint:
	cd frontend && npm run lint

frontend-css-guard:
	cd frontend && npm run check:css

frontend-unit:
	cd frontend && npm run test:unit

frontend-build:
	cd frontend && npm run build

playwright-e2e:
	cd frontend && CQ_E2E=1 npm run test:e2e
	# unexpected-skip guard (PR #6 final review P1; P1-visibility v2):
	# watches EVERY e2e spec by default — a spec absent from the report
	# or any skip not covered by e2e/noskip-exemptions.mjs (gate+reason
	# per entry, all matches logged) fails the gate. No hard-coded
	# watch list left to drift.
	cd frontend && node scripts/assert-e2e-no-skips.mjs

release-gate: coverage-clean release-test-db backend-unit-cov \
              backend-integration-cov backend-worker-cov \
              backend-e2e migration-verify frontend-typecheck frontend-lint \
              frontend-css-guard frontend-unit frontend-build playwright-e2e \
              visual-regression coverage-ratchet

# --- Plan 12 task 9: pixel visual regression --------------------------------
#
# Gated in both places (C3 promotion, plan-12 Phase C): a prerequisite of
# release-gate above and the ci.yml visual-regression job. Baselines at
# frontend/e2e/visual-regression.spec.ts-snapshots/
# are Linux-authoritative and committed (reviewed like code); regenerate
# deliberately with CQ_VISUAL_UPDATE=1 and re-review every changed PNG.
# Needs the compose dependency stack; the Playwright config orchestrates
# both dev servers (override CQ_E2E_BASE_URL / CQ_E2E_API_URL together
# when 3000/8000 are occupied — same rule as playwright-e2e).
.PHONY: visual-regression
visual-regression:
	cd frontend && CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1 npm run test:e2e:visual
