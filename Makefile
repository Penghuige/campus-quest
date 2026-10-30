# CampusQuest top-level verification commands.
# Backend tool versions are committed in backend/pyproject.toml (dev group),
# so `uv run` never drifts; frontend tooling is committed in frontend/package.json.

.PHONY: test-backend test-integration lint-backend verify \
        arch-lint-backend arch-lint-frontend arch-lint arch-map
# Plan 10 task 11's release steps are individually runnable and composed
# by `release-gate` below; .NOTPARALLEL keeps prerequisite order
# deterministic even under `make -j`.
.NOTPARALLEL:

test-backend:
	cd backend && uv run pytest tests/unit tests/workers -v

test-integration:
	cd backend && uv run pytest tests/integration -v -m integration

lint-backend:
	cd backend && uv run ruff format --check app tests alembic loadtest
	cd backend && uv run ruff check app tests alembic loadtest
	cd backend && uv run mypy

# Architecture contract gate (owner-approved addition 2026-10-16): the
# written layering rules (docs/quality/backend-engineering.md §1-§2 and
# the cross-module port rule in docs/architecture/interfaces.md) as
# machine-checked import contracts in backend/pyproject.toml
# [tool.importlinter], plus the frontend boundary rules in
# frontend/.dependency-cruiser.cjs. Known drift is waived in-config
# (rental-style); warn-level findings (cross-feature imports) do not
# fail the gate.
arch-lint-backend:
	cd backend && uv run lint-imports

arch-lint-frontend:
	cd frontend && npm run arch:lint

arch-lint: arch-lint-backend arch-lint-frontend

# Derived-only as-built map: regenerates docs/architecture/as-built.md
# (module graphs + boundary-debt tables) from the tree. Commit the
# regenerated file with any change that drifts it; `--check` is the
# freshness mode, `--self-test` proves the scanners.
arch-map:
	cd backend && uv run python ../scripts/generate_as_built.py

# The freshness gate over the derived map: the check compares scanned
# CONTENT with the informational HEAD stamp normalized out — it fails
# only on real tree drift, so it can ride pre-push and CI without
# noising on every commit.
arch-map-check:
	cd backend && uv run python ../scripts/generate_as_built.py --check

# Owner-facing 3D "code city" (optional visual layer; local-only tools):
# churn metrics from git history + code metrics from tree-sitter parsing,
# merged into one CodeCharta map. Height/complexity + color/commits =
# hotspots (the CodeScene-style signal). JVM via npx; first run downloads.
# Open the result with: npx codecharta-visualization -p build/code-city.cc.json
# (launches the local Electron app; see docs/architecture/visual-tools.md).
.PHONY: arch-city
arch-city:
	mkdir -p build
	npx -y codecharta-analysis@latest gitlogparser repo-scan --repo-path=. -nc -o build/cc-churn.cc.json
	npx -y codecharta-analysis@latest unifiedparser backend/app -nc -o build/cc-be.cc.json
	npx -y codecharta-analysis@latest unifiedparser frontend/src -nc -o build/cc-fe.cc.json
	npx -y codecharta-analysis@latest merge -f -nc build/cc-churn.cc.json build/cc-be.cc.json build/cc-fe.cc.json -o build/code-city.cc.json
	@echo "open: npx codecharta-visualization -p build/code-city.cc.json"

# Browser-based viewer for the code city (no Electron, no DISPLAY):
# serves the visualization web assets locally and loads the map via the
# ?file= URL parameter. Ctrl+C to stop; port defaults to 8190.
.PHONY: arch-city-view
arch-city-view:
	bash scripts/view_code_city.sh

verify: lint-backend arch-lint
	cd backend && uv run pytest -v
	cd frontend && npm run typecheck && npm run lint && npm run check:css && npm run build

# Pre-push fast lane (~2 min, no docker stack): the CI gates that fail
# most often and run fully locally — coverage-floor direction, backend
# lint/types/architecture, spec-test matrix coherence, G3 placeholder
# guard, both contract-freshness checks (OpenAPI snapshot + generated
# TS types), frontend typecheck/lint/CSS/unit-coverage ratchet. Wired
# as the git pre-push hook via .githooks/ (core.hooksPath); skip a
# single push with `git push --no-verify`. Backend coverage ratchet,
# integration, e2e, and the pixel/axe suites need the compose stack —
# those stay in `make pre-push-full`, `make release-gate`, and CI.
# Gotcha: tsconfig includes .next/types/**, so after pulling commits
# that delete or rename a page, a stale .next cache fails typecheck
# with phantom page.js errors — `rm -rf frontend/.next/types
# frontend/.next/dev/types` and re-run (next dev/build regenerates).
.PHONY: pre-push ratchet-direction test-matrix placeholder-guard arch-map-check
pre-push: ratchet-direction lint-backend arch-lint arch-map-check test-matrix placeholder-guard
	cd backend && $(TEST_STACK_ENV) uv run python scripts/dump_openapi.py ../frontend/openapi.snapshot.json
	git diff --exit-code frontend/openapi.snapshot.json
	cd frontend && npm run api:types:snapshot
	git diff --exit-code frontend/src/lib/api/schema.d.ts
	cd frontend && npm run typecheck && npm run lint && npm run check:css && npm run coverage:ratchet

# Coverage-floor direction gate (2026-10-19): ratchet floors only move
# up — scripts/check_ratchet_direction.py compares both
# coverage-ratchet.json files against the merge base with origin/main
# and fails on any lowered/removed floor, closing the "--update can
# quietly lower the bar" hole. Deliberate decreases carry the
# [floor-adjust] marker (CI wires it to the PR body); offline clones
# skip with a warning.
ratchet-direction:
	python3 scripts/check_ratchet_direction.py

# Spec-test matrix coherence (G19, owner-approved 2026-10-07): every
# covered row cites an existing test file (name-level since 2026-10-25),
# gap/mismatch rows are registered, and every matrix's 覆盖章节 line
# points at real spec headings. Advisory reminder follows the check:
# when the diff touches a spec file, the matrices claiming its sections
# need a re-derivation pass (non-blocking — the checker itself stays
# the gate).
test-matrix:
	cd backend && uv run python scripts/check_test_matrix.py
	@spec='docs/superpowers/specs'; \
	if git diff --name-only origin/main...HEAD 2>/dev/null | grep -q "^$$spec/"; then \
	  echo "ADVISORY: spec file(s) changed this branch:"; \
	  git diff --name-only origin/main...HEAD | grep "^$$spec/" | sed 's/^/  - /'; \
	  echo "  Matrices claiming these sections (their 覆盖章节 lines) need a"; \
	  echo "  re-derivation pass before merge:"; \
	  grep -l "覆盖章节" docs/quality/test-matrix/*.md | xargs grep -H "覆盖章节" | sed 's/^/  · /'; \
	fi

# G3 placeholder guard: no `raise NotImplementedError` ships in
# backend/app (zero today — this keeps it that way). Prose TODO/FIXME
# comments stay free; only the executable placeholder is gated.
placeholder-guard:
	! grep -rn "raise NotImplementedError" backend/app --include="*.py"

# --- Plan 10 task 11: the V1 release command ------------------------------
#
# `make release-gate` runs, in this order (plan 10 task 11 step 1):
#
#   0. architecture contract gate   (arch-lint; owner-approved addition
#      2026-10-16 — runs FIRST so boundary violations fail before the
#      expensive suites: backend import-linter contracts + frontend
#      dependency-cruiser boundaries)
#   1. coverage accumulation reset  (coverage-clean: rm backend/.coverage —
#      --cov-append must not inherit an earlier local run's data)
#      + test-database bootstrap     (release-test-db; idempotent
#      `alembic upgrade head` on campusquest_test — the integration/e2e
#      suites need a migrated schema and nothing else creates it on
#      fresh compose volumes)
#   2. backend unit+worker tests     (backend-unit-worker-cov; ONE pytest
#      process, the ci.yml shape — the ratchet's cross-talk waiver
#      fingerprints only appear in this combined run, so this is the
#      only shape whose ratchet verdict reproduces CI)
#   3. backend integration tests     (backend-integration-cov; real
#      PostgreSQL/Redis/MinIO, S3 + composition smokes ON)
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
#  14. pixel visual regression       (visual-regression; the 11-baseline
#      pixel suite — same gate as the ci.yml visual job)
#  15. dependency audit gates        (pip-audit + npm-audit; the CI
#      backend/frontend jobs gate these — wired here 2026-10-19 so the
#      local gate no longer skips them)
#  16. coverage ratchet              (coverage-ratchet; owner-approved
#      addition — backend: the .coverage accumulated by the -cov legs
#      of steps 2-3 vs coverage-ratchet.json floors; frontend: the
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

# The backend suites, single-sourced: the bare targets, the
# coverage-accumulating -cov variants the gate runs, and
# coverage-baseline below all compose these, so suite paths and stack
# env cannot drift between them. COV_APPEND_FLAGS is the
# accumulate-into-one-.coverage chain the step-16 ratchet compares.
# BACKEND_UNIT_WORKER_CMD runs unit+workers in ONE pytest process —
# the ci.yml shape; the ratchet's cross-talk waiver fingerprints only
# appear in this combined run, so every coverage-measuring leg uses it.
COV_APPEND_FLAGS = --cov=app --cov-branch --cov-report= --cov-append
BACKEND_UNIT_CMD = cd backend && $(TEST_STACK_ENV) uv run pytest tests/unit
BACKEND_INTEGRATION_CMD = cd backend && $(TEST_STACK_ENV) CQ_S3_SMOKE=1 CQ_COMPOSITION_SMOKE=1 CQ_EVIDENCE_SCAN_SMOKE=1 uv run pytest tests/integration -m integration
BACKEND_WORKER_CMD = cd backend && $(TEST_STACK_ENV) uv run pytest tests/workers
BACKEND_UNIT_WORKER_CMD = cd backend && $(TEST_STACK_ENV) uv run pytest tests/unit tests/workers

.PHONY: backend-unit backend-integration backend-worker backend-e2e \
        backend-unit-worker-cov backend-integration-cov \
        migration-verify frontend-typecheck frontend-lint frontend-css-guard \
        frontend-unit frontend-build playwright-e2e release-test-db release-gate \
        reset-test-db release-evidence-scanner innovation-visual \
        coverage-clean coverage-baseline coverage-ratchet pip-audit npm-audit \
        pre-push-full test-report report-smoke explore-round stack stack-down

# The gate's self-bootstrapping first step (PR #6 final review P1): the
# integration and e2e suites assume a MIGRATED campusquest_test and
# reset-test-db: the pristine-database precondition, CI's own procedure
# (release-checklist "Resetting the test stack"): browser-e2e debris —
# from killed runs AND from a full local battery chained ahead of the
# visual stages — leaves orphaned users that break directory-style
# first-page assertions and pixel baselines. CI starts every job with a
# pristine database; this gives the local gate the same precondition.
reset-test-db:
	docker compose -f infra/docker-compose.yml exec -T postgres psql -U campusquest -d campusquest \
	  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='campusquest_test' AND pid <> pg_backend_pid()" \
	  -c "DROP DATABASE IF EXISTS campusquest_test" \
	  -c "CREATE DATABASE campusquest_test OWNER test"
	docker compose -f infra/docker-compose.yml exec -T redis redis-cli -n 0 FLUSHDB
	cd backend && $(TEST_STACK_ENV) uv run alembic upgrade head

# The gate's opening precondition step (fresh schema for the backend
# legs); the browser legs reset again ahead of themselves below.
release-test-db: reset-test-db

backend-unit:
	$(BACKEND_UNIT_CMD) -v

backend-integration:
	$(BACKEND_INTEGRATION_CMD) -v

backend-worker:
	$(BACKEND_WORKER_CMD) -v

# The gate's coverage-accumulating legs in the CI shape: unit+workers
# in one pytest process (see BACKEND_UNIT_WORKER_CMD above), then
# integration — the --cov-append chain whose accumulated
# backend/.coverage step 16's ratchet compares. release-gate runs
# coverage-clean first so a stale local .coverage (e.g. left by an
# earlier baseline run) can never flatter the floors.
backend-unit-worker-cov:
	$(BACKEND_UNIT_WORKER_CMD) $(COV_APPEND_FLAGS) -v

backend-integration-cov:
	$(BACKEND_INTEGRATION_CMD) $(COV_APPEND_FLAGS) -v

backend-e2e:
	cd backend && $(TEST_STACK_ENV) CQ_E2E=1 uv run pytest tests/e2e -v

migration-verify:
	bash scripts/verify-migrations.sh

coverage-clean:
	rm -f backend/.coverage

# Coverage ratchet (owner-approved 2026-10-07). baseline: the CI-shaped
# legs with --cov (branch, app/ only, appended into one .coverage) +
# REWRITE the floors (--update is a deliberate, reviewable commit —
# floors may only move up; the ratchet-direction gate blocks silent
# lowering). ratchet: compare the accumulated .coverage against the
# committed floors (the CI gate; CI accumulates via --cov-append in
# the same shape), then the frontend native-coverage ratchet, which
# self-runs its unit suite and needs no wired input.
coverage-baseline: coverage-clean
	$(BACKEND_UNIT_WORKER_CMD) $(COV_APPEND_FLAGS) -q
	$(BACKEND_INTEGRATION_CMD) $(COV_APPEND_FLAGS) -q
	cd backend && uv run python scripts/coverage_ratchet.py --update

coverage-ratchet:
	cd backend && uv run python scripts/coverage_ratchet.py
	cd frontend && npm run coverage:ratchet

pip-audit:
	cd backend && uv run python scripts/pip_audit_gate.py

npm-audit:
	cd frontend && npm run audit:gate

# Pre-push full lane (~5-8 min, needs the compose stack): reproduces
# the CI backend coverage chain in the CI shape plus both ratchets —
# run before merging backend-heavy PRs. Everything faster is in the
# pre-push fast lane; everything slower is in release-gate.
pre-push-full: coverage-clean release-test-db backend-unit-worker-cov \
               backend-integration-cov coverage-ratchet

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

# --- Test report dashboard (owner-approved 2026-10-25) ----------------------
#
# Builds the offline seven-layer dashboard from reports/artifacts/. Only
# the two cheap suites are produced on demand; layers that need
# PostgreSQL/Redis/MinIO/browsers are NEVER started or borrowed here —
# they stay "未运行" until someone produces their result file (see
# docs/operations/test-report.md). Reusing an existing artifact is
# intentional: delete reports/artifacts/ to refresh.
REPORT_DIR = reports
REPORT_ARTIFACTS_DIR = $(REPORT_DIR)/artifacts
REPORT_BACKEND_UNIT_XML = $(REPORT_ARTIFACTS_DIR)/backend-unit.xml
REPORT_FRONTEND_UNIT_XML = $(REPORT_ARTIFACTS_DIR)/frontend-unit.xml
REPORT_HTML = $(REPORT_DIR)/test-report.html

.PHONY: test-report
test-report:
	@mkdir -p $(REPORT_ARTIFACTS_DIR)
	@if [ -f $(REPORT_BACKEND_UNIT_XML) ]; then \
		echo "==> backend unit+worker: reusing $(REPORT_BACKEND_UNIT_XML)"; \
	else \
		echo "==> backend unit+worker: running pytest with JUnit XML"; \
		$(BACKEND_UNIT_WORKER_CMD) -q --junitxml=$(CURDIR)/$(REPORT_BACKEND_UNIT_XML) || \
			echo "!!! backend unit+worker reported failures; the dashboard will show them"; \
		if [ ! -f "$(CURDIR)/$(REPORT_BACKEND_UNIT_XML)" ]; then \
			echo "!!! backend unit+worker produced no JUnit XML"; \
			exit 1; \
		fi; \
		echo "==> backend unit+worker: stamping provenance sidecar"; \
		(cd "$(CURDIR)/backend" && uv run python scripts/test_report.py \
			--stamp-artifact "$(CURDIR)/$(REPORT_BACKEND_UNIT_XML)" \
			--command "pytest tests/unit tests/workers --junitxml") || \
			echo "!!! provenance stamp failed; the layer will show 来源未知"; \
	fi
	@if [ ! -d frontend/node_modules ]; then \
		echo "==> frontend unit: skipped (frontend/node_modules missing; not running npm ci)"; \
	elif [ -f $(REPORT_FRONTEND_UNIT_XML) ]; then \
		echo "==> frontend unit: reusing $(REPORT_FRONTEND_UNIT_XML)"; \
	else \
		echo "==> frontend unit: running node:test with the junit reporter"; \
		(cd frontend && node --import tsx --test \
			--test-reporter=junit \
			--test-reporter-destination=$(CURDIR)/$(REPORT_FRONTEND_UNIT_XML) \
			src/__tests__/*.test.ts) || \
			echo "!!! frontend unit reported failures; the dashboard will show them"; \
		if [ ! -f "$(CURDIR)/$(REPORT_FRONTEND_UNIT_XML)" ]; then \
			echo "!!! frontend unit produced no JUnit XML"; \
			exit 1; \
		fi; \
		echo "==> frontend unit: stamping provenance sidecar"; \
		(cd "$(CURDIR)/backend" && uv run python scripts/test_report.py \
			--stamp-artifact "$(CURDIR)/$(REPORT_FRONTEND_UNIT_XML)" \
			--command "node --import tsx --test junit reporter") || \
			echo "!!! provenance stamp failed; the layer will show 来源未知"; \
	fi
	@notes=""; \
	if [ ! -d frontend/node_modules ]; then \
		notes="frontend/node_modules 不存在：未自动执行 npm ci，前端单测层显示未运行"; \
	fi; \
	commit="$$(git rev-parse HEAD)"; \
	if [ -n "$$(git status --porcelain)" ]; then \
		notes="$$notes 工作区有未提交改动；CI 记录仅对应 commit $$(printf %.7s "$$commit")"; \
	fi; \
	echo "==> CI: fetching GitHub Actions record for $$(printf %.7s "$$commit")"; \
	(cd backend && uv run python scripts/ci_record.py \
		--commit "$$commit" \
		--output "$(CURDIR)/$(REPORT_ARTIFACTS_DIR)/ci.json") || \
		echo "!!! CI record unavailable; the report will show its absence"; \
	cd backend && uv run python scripts/test_report.py \
		--artifacts-dir ../$(REPORT_ARTIFACTS_DIR) \
		--output ../$(REPORT_HTML) \
		--command "make test-report" \
		--commit "$$commit" \
		--ci-file "../$(REPORT_ARTIFACTS_DIR)/ci.json" \
		--history-dir "../$(REPORT_DIR)/history" \
		--note "$$notes"

# `make stack` / `make stack-down` are the local stack lifecycle. The report
# viewer is a compose service behind the "report" profile (campus reviewer
# ruling 2026-10-26): `make stack` enables the profile, so the viewer starts
# and stops WITH the stack; bare `docker compose up -d` (the gate
# prerequisite, other sessions, production) does not start it — that is
# documented behavior, not an accident. `make test-report` only generates
# the page; the running viewer serves the directory.
stack:
	docker compose -f infra/docker-compose.yml --profile report up -d
	$(MAKE) test-report

stack-down:
	docker compose -f infra/docker-compose.yml down

# Lifecycle proof for the report profile form (scripts/report-viewer-smoke.sh):
# touches only the report-viewer service — no stack borrow needed.
report-smoke:
	bash scripts/report-viewer-smoke.sh

# One exploration round: adversarial probes against the real stack, results
# into reports/artifacts/exploration.json (dashboard layer 7). BORROW
# PROTOCOL applies (campusquest-test-process): announce 3000/8000 + the
# test database to the campus reviewer, wait for the release, run, return.
# Cadence ruling 2026-10-26: on demand after major merges (manual form).
explore-round:
	cd frontend && CQ_E2E=1 CQ_E2E_EXPLORE=1 CQ_E2E_LOGIN_RATELIMIT=1 \
		CQ_E2E_API_URL=http://localhost:8000/api/v1 \
		npx playwright test e2e/exploratory-probe.spec.ts --project=chromium
	$(MAKE) test-report

playwright-e2e:
	# Prerequisite dedup means a reset listed twice in the chain runs once —
	# recurse so EVERY browser leg opens from the pristine database.
	@$(MAKE) --no-print-directory reset-test-db
	# CQ_E2E_LOGIN_RATELIMIT: the same battery-scale relief CI's e2e job
	# runs with (ci.yml sets it; the comment there claims the local
	# battery matches). Without it the dense login specs — unified-login's
	# brute-force chains included — blow the login family's IP bucket for
	# the browsers that run later in the battery.
	# CQ_E2E_API_URL: several specs' fallback for their direct API calls
	# is 127.0.0.1:8100 — a port nothing serves; the battery's backend is
	# the webServer on 8000. CI has been paused since 2026-10-07, so these
	# specs only ever ran green under whatever env each author exported
	# locally; pin the battery to its own webServer explicitly.
	cd frontend && CQ_E2E=1 CQ_E2E_LOGIN_RATELIMIT=1 CQ_E2E_API_URL=http://localhost:8000/api/v1 npm run test:e2e
	# unexpected-skip guard (PR #6 final review P1; P1-visibility v2):
	# watches EVERY e2e spec by default — a spec absent from the report
	# or any skip not covered by e2e/noskip-exemptions.mjs (gate+reason
	# per entry, all matches logged) fails the gate. No hard-coded
	# watch list left to drift.
	cd frontend && node scripts/assert-e2e-no-skips.mjs

# --- Innovation batch 1: the ClamAV scanner stack -----------------------------
#
# Separate project name (campusquest-evidence) so the scanner lives beside
# the compose stack without inheriting its networks or credentials; the
# talent file-completion path fails closed when this service is down, so
# the release gate requires it up before the integration suite runs.
release-evidence-scanner:
	docker compose -p campusquest-evidence -f infra/docker-compose.clamav.yml up -d --wait --wait-timeout 360

# Every browser leg opens from the pristine precondition: the battery
# seeds its own world, and its teardown cannot be trusted to leave the
# shared database pixel-exact for the NEXT stage (admin directory
# baselines are whole-table sensitive) — CI sidesteps this with isolated
# jobs; the local chain resets between stages instead.
release-gate: arch-lint coverage-clean release-test-db release-evidence-scanner \
              backend-unit-worker-cov backend-integration-cov \
              backend-e2e migration-verify frontend-typecheck frontend-lint \
              frontend-css-guard frontend-unit frontend-build \
              playwright-e2e visual-regression innovation-visual pip-audit \
              npm-audit coverage-ratchet

# The innovation visual battery runs in its own pinned-font Linux stage
# with a disposable real publication world (noskip-exempted from the
# standard battery; see frontend/e2e/noskip-exemptions.mjs for the reason
# strings). Covers the talent family (batch 1) and the campus landing +
# project workspace (batch 2). The relief+API_URL pair matches
# playwright-e2e: the gate runs its legs against ONE shared stack, so the
# login buckets the battery filled must not bleed into this stage.
innovation-visual:
	@$(MAKE) --no-print-directory reset-test-db
	cd frontend && CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1 CQ_E2E_LOGIN_RATELIMIT=1 CQ_E2E_API_URL=http://localhost:8000/api/v1 npx playwright test innovation-talents-visual.spec.ts innovation-navigation-visual.spec.ts innovation-review-visual.spec.ts innovation-recruitment-visual.spec.ts --project=chromium

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
	@$(MAKE) --no-print-directory reset-test-db
	# Same relief+API_URL pair as playwright-e2e — the gate's legs share
	# one stack, so the battery's login volume must not rate-limit the
	# visual stage out of its own logins.
	cd frontend && CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1 CQ_E2E_LOGIN_RATELIMIT=1 CQ_E2E_API_URL=http://localhost:8000/api/v1 npm run test:e2e:visual

# --- loadtest (spec: docs/superpowers/specs/2026-10-29-load-testing-design.md) --
# On-demand capacity suite: locust against the local compose stack.
# stack.sh refuses non-local DATABASE_URL — production is never the SUT
# (spec §Non-goals). Login is offline-minted by the seed script; the
# numbers describe the API, not the browser (spec §Auth).
.PHONY: loadtest-stack-up loadtest-stack-down

loadtest-stack-up:
	$(TEST_STACK_ENV) bash backend/loadtest/stack.sh up

loadtest-stack-down:
	bash backend/loadtest/stack.sh down

# Seed the load world (spec §Auth/§Scenarios): pre-seeded claims for S1,
# token-bearing racers for S2, offline-minted sessions (never the login
# route). Defaults match the spec targets; override on the command line.
.PHONY: loadtest-seed

loadtest-seed:
	@ts=$$(date -u +%Y%m%dT%H%M%SZ); \
	cd backend && $(TEST_STACK_ENV) uv run python scripts/seed_load_world.py \
		--claim-slots $${SEED_CLAIM_SLOTS:-50} \
		--race-users $${SEED_RACE_USERS:-300} \
		--race-slots $${SEED_RACE_SLOTS:-60} \
		--deadline-in-minutes $${SEED_DEADLINE_MIN:-20} \
		--out-dir loadtest/runs/$$ts $${SEED_FORCE:+--force}

# S3 read baseline (spec §Scenarios S3): 20 users, 2 minutes, human pacing.
# Locust picks the newest seeded world; summary lands in runs/<ts>/s3/.
.PHONY: loadtest-s3

loadtest-s3:
	@ts=$$(date -u +%Y%m%dT%H%M%SZ); out=backend/loadtest/runs/$$ts/s3; \
	mkdir -p $$out; \
	cd backend && $(TEST_STACK_ENV) \
		CQ_LOADTEST_OUT=../$$out CQ_LOADTEST_SCENARIO=s3 \
		uv run locust -f loadtest/s3_baseline.py --headless \
		-u 20 -r 5 -t 2m --host http://localhost:8000 \
		--csv ../$$out/locust
