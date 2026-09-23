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
	cd frontend && npm run typecheck && npm run lint && npm run build

# --- Plan 10 task 11: the V1 release command ------------------------------
#
# `make release-gate` runs, in this order (plan 10 task 11 step 1):
#
#   0. test-database bootstrap     (release-test-db; idempotent
#      `alembic upgrade head` on campusquest_test — the integration/e2e
#      suites need a migrated schema and nothing else creates it on
#      fresh compose volumes)
#   1. backend unit tests            (backend-unit)
#   2. backend integration tests     (backend-integration; real
#      PostgreSQL/Redis/MinIO, S3 + composition smokes ON)
#   3. backend worker tests          (backend-worker)
#   4. backend e2e tests             (backend-e2e, CQ_E2E=1)
#   5. migration verification        (migration-verify; dedicated
#      campusquest_migrate_test database: upgrade head -> schema
#      assertions -> downgrade base -> re-upgrade)
#   6. frontend typecheck            (frontend-typecheck)
#   7. frontend lint                 (frontend-lint)
#   8. frontend unit tests           (frontend-unit)
#   9. frontend production build     (frontend-build)
#  10. Playwright e2e                (playwright-e2e, CQ_E2E=1; the config
#      orchestrates both servers itself)
#  11. ranking rebuild test          — inside backend-e2e:
#      tests/e2e/test_ranking_recovery.py
#  12. concurrency gate              — inside backend-e2e:
#      tests/e2e/test_concurrency_gate.py
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

.PHONY: backend-unit backend-integration backend-worker backend-e2e \
        migration-verify frontend-typecheck frontend-lint frontend-unit \
        frontend-build playwright-e2e release-test-db release-gate

# The gate's self-bootstrapping first step (PR #6 final review P1): the
# integration and e2e suites assume a MIGRATED campusquest_test and
# nothing else creates one on fresh compose volumes (the init script
# only CREATEs the empty database). `alembic upgrade head` is a no-op
# when the schema is already at head, so the target is idempotent and
# costs one version query on warm stacks.
release-test-db:
	cd backend && $(TEST_STACK_ENV) uv run alembic upgrade head

backend-unit:
	cd backend && $(TEST_STACK_ENV) uv run pytest tests/unit -v

backend-integration:
	cd backend && $(TEST_STACK_ENV) CQ_S3_SMOKE=1 CQ_COMPOSITION_SMOKE=1 uv run pytest tests/integration -v -m integration

backend-worker:
	cd backend && $(TEST_STACK_ENV) uv run pytest tests/workers -v

backend-e2e:
	cd backend && $(TEST_STACK_ENV) CQ_E2E=1 uv run pytest tests/e2e -v

migration-verify:
	bash scripts/verify-migrations.sh

frontend-typecheck:
	cd frontend && npm run typecheck

frontend-lint:
	cd frontend && npm run lint

frontend-unit:
	cd frontend && npm run test:unit

frontend-build:
	cd frontend && npm run build

playwright-e2e:
	cd frontend && CQ_E2E=1 npm run test:e2e
	# unexpected-skip guard (PR #6 final review P1): the teacher/admin
	# suites must RUN, not silently skip — a missing world export would
	# otherwise read as a green gate with two suites absent. The JSON
	# report the run just wrote is the evidence; exit 1 on any skip.
	cd frontend && node scripts/assert-e2e-no-skips.mjs

release-gate: release-test-db backend-unit backend-integration backend-worker \
              backend-e2e migration-verify frontend-typecheck frontend-lint \
              frontend-unit frontend-build playwright-e2e
