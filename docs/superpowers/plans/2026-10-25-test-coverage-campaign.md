# Test Coverage Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six test-governance improvements approved by the owner (8-domain matrix reconciliation, checker citation granularity, property tests for G-3, spec↔matrix linkage, teardown audit refresh, backend mutation campaign) and finish with fresh full gates (`make verify` + `make release-gate`).

**Architecture:** All work lands on branch `test-coverage-campaign` (worktree, base = origin/main @ 68eba1b), one commit per task. Analysis artifacts (matrices, audit) are produced by parallel subagents with exclusive file ownership; code changes (checker, property tests) are TDD-native. The full-gate run borrows the e2e stack per the borrow protocol (notify campus reviewer → wait → run → return).

**Tech Stack:** Python 3.12 / pytest / Hypothesis (already a dev dep), node:test, importlinter; mutmut via `uv run --with` (not added to pyproject unless it proves out).

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md` — sections §8.1 (claim states), §9, §13, §16, §18, §19, §20, §27, §30 are the reconciliation targets.

## Global Constraints

- 缺口列出不补：matrices record gaps (G-N ids); discovered bugs/mismatches are REPORTED, never fixed in this campaign.
- checker vocabulary is closed: row ids `^[A-Z]+-?\d+$|^[EG]\d+$`; statuses 已覆盖/缺口/不匹配/流程行; covered rows must cite `tests/...` or `frontend/(e2e|src)/...` paths; gap rows need G-N registered in the file's 缺口明细 section.
- Matrix discipline: expectations derived from spec text BEFORE test search (预期先行); 每域一文件; template = `docs/quality/test-matrix/points-ledger.md`.
- Any test run touching `campusquest_test@15432` (integration, e2e, coverage) requires the borrow protocol: notify `campus reviewer` session, wait for stack release, run, announce return. Unit-only runs (fakes, no PG) are exempt.
- New integration test files MUST carry pytestmark (repo trap); unit files follow `tests/unit/tasks/test_task_lifecycle.py` conventions (FakeSession pattern, no PG).
- Commits: no AI attribution trailers; stage only task-related files.
- No push in this campaign — local commits only; push/PR is a separate owner decision.

## Review Focus

1. Checker name-validation false positives (cited `::name` that exists but isn't `def name(` — e.g. class methods, parametrized ids) — validation must match the citation convention actually used across the 11 existing matrices.
2. Matrix agents duplicating rows already owned by neighboring matrices (§20 vs community.md, §16 vs points-ledger.md, §26 vs claims-lifecycle.md) — prompts must force reading neighbors first.
3. Property test pinning implementation behavior instead of spec §8.1 (tautology) — the spec's legal transition set must be transcribed in the test from §8.1 text, not read from production code.
4. Mutation survivors misread as gaps when integration tests actually catch them — survivor cross-check against integration suite happens inside the borrow window before any "assertion gap" claim.
5. pre-push spec-warning blocking pushes on unrelated spec edits — the warning must be advisory (echo), only section-existence validation is blocking.

---

### Task 1: Checker validates cited test names (not just files)

**Files:**
- Modify: `backend/scripts/check_test_matrix.py`
- Create: `backend/tests/unit/scripts/test_check_test_matrix.py`
- Modify: any of the 11 matrices whose citations turn out stale (fix citations only)

**Interfaces:**
- Produces: `_cite_name_ok(cited: str) -> bool` — resolves `path[::name]`; backend `.py` requires `def {name}(` or `async def {name}(` in file text; frontend files require the name as a substring; bare paths (no `::`) keep file-existence-only semantics.

- [ ] **Step 1: Write failing tests** in `test_check_test_matrix.py`: (a) covered row citing `tests/unit/scripts/_fixture_matrix_helper.py::test_exists` where the def exists → no violation; (b) same citation where only the file exists (no such def) → violation "cites missing test"; (c) bare-path citation (no `::`) with existing file → no violation; (d) frontend citation `frontend/e2e/x.spec.ts::does a thing` with the title string present → no violation. Use tmp_path matrices + real tmp test files; follow the existing guard test's structure (`test_coverage_ratchet_guard.py`).
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/unit/scripts/test_check_test_matrix.py -q` — expect FAIL (name check not implemented).
- [ ] **Step 3: Implement** `_cite_name_ok` + wire into the covered-row loop (`check_test_matrix.py:101-112` region). Keep docstring checks-list updated (add check 2b).
- [ ] **Step 4: Run the new tests → PASS; then run the checker on all 11 real matrices** (`uv run python scripts/check_test_matrix.py`). Fix any stale `::name` citations IN THE MATRIX FILES (they are documentation bugs; correct the citation to the real test name — if the test truly doesn't exist, flip the row to 缺口 with a new G-N and note it in the campaign report, do not write new tests).
- [ ] **Step 5: Commit** `feat(scripts): test-matrix checker validates cited test names, not just files`

### Task 2: Seven domain matrices (parallel subagents, 预期先行)

**Files (each agent owns exactly its files):**
- Create: `docs/quality/test-matrix/deadline.md` (§9) + `task-ratings.md` (§20) — agent A
- Create: `docs/quality/test-matrix/rewards-redemption.md` (§16) + `honors.md` (§18) — agent B
- Create: `docs/quality/test-matrix/growth-page.md` (§19) + `audit-log.md` (§30) — agent C
- Create: `docs/quality/test-matrix/file-retention.md` (§13 + §27 combined lifecycle) — agent D

**Interfaces:**
- Consumes: Task 1's stricter checker (agents must satisfy name-level citations).
- Produces: matrix files passing `uv run python scripts/check_test_matrix.py <file>`; per-agent report of gaps (G-N) / mismatches / scope overlaps found.

- [ ] **Step 1: Dispatch 4 agents in parallel.** Each prompt embeds: worktree root; the discipline block (预期先行, 缺口列出不补, 不匹配上报); template path; checker command; closed vocabulary + row-id regex; **mandatory first reads** (spec section(s) verbatim, template, and the neighbor matrices that could overlap — A reads community.md (§20/§21 boundary) & claims-lifecycle.md (§9/§26 boundary); B reads points-ledger.md (§16/§17 boundary); D reads claims-lifecycle.md (§26) and submissions-validation.md (§13 boundary)); instruction to derive+write expectations BEFORE any test grep; exclusive-write file list; report format (gaps/mismatches/overlaps with evidence).
- [ ] **Step 2: On agent completion, run the full checker** over all 18 matrices (11 old + 7 new) — zero violations required; route any violation back to the owning agent via SendMessage.
- [ ] **Step 3: Review the four reports** — collect gaps/mismatches into `docs/evidence/2026-10-25-matrix-campaign-findings.md` (list only; no fixes).
- [ ] **Step 4: Commit** `docs(test-matrix): reconcile seven remaining spec domains (deadline, ratings, redemption, honors, growth, audit-log, file retention)`

### Task 3: Property tests close G-3 (claim transition pin)

**Files:**
- Create: `backend/tests/unit/tasks/test_claim_lifecycle_property.py`
- Modify: `docs/quality/test-matrix/claims-lifecycle.md` (C3 row + summary counts 1→0 缺口)

**Interfaces:**
- Consumes: `ClaimService` transition surface in `backend/app/modules/tasks/claim_service.py` (guards at ~lines 160-200; per-action allow/deny); `ClaimStatus` enum from `app.modules.tasks.enums`; FakeSession pattern from `tests/unit/tasks/test_task_lifecycle.py`.
- Produces: `SPEC_CLAIM_TRANSITIONS: dict[ClaimStatus, set[ClaimStatus]]` transcribed in-test from spec §8.1 (the assertion oracle), and Hypothesis tests: (1) all-pairs — service accepts (current→target) iff edge in SPEC_CLAIM_TRANSITIONS; (2) legal-chain — any sequence of spec-legal edges applied through the service keeps claim status consistent.

- [ ] **Step 1: Read spec §8.1 and transcribe** the legal transition set into the test file's oracle dict FIRST (before reading production guards).
- [ ] **Step 2: Write failing tests** — all-pairs @given(current, target sampled from ClaimStatus) asserting service allow/deny matches oracle; legal-chain @given(lists of legal edges). Run: `cd backend && uv run pytest tests/unit/tasks/test_claim_lifecycle_property.py -q` — expect FAILs wherever behavior/oracle disagree; investigate each (report mismatches per Global Constraints, do not change production code unless it is a confirmed G-3-adjacent test-harness gap).
- [ ] **Step 3: Stabilize harness** (FakeSession extension for claim rows) until the property suite is green and deterministic (`@settings(max_examples=..., deadline=None)`; Hypothesis derandomization not required for CI but keep runtime <10s).
- [ ] **Step 4: Update claims-lifecycle.md**: C3 evidence cell now cites the new property tests (name-level citation per Task 1 checker); summary 缺口 1→0; move G-3 out of 缺口明细 into a resolved note. Checker green.
- [ ] **Step 5: Commit** `test(tasks): property-based claim lifecycle pin closes matrix gap G-3`

### Task 4: Matrix↔spec section linkage

**Files:**
- Modify: `backend/scripts/check_test_matrix.py` (+ its unit test file from Task 1)
- Modify: all 18 matrix headers (add `> 覆盖章节：§N · §N` line under the 建立日期 line)
- Modify: `Makefile` (test-matrix target gains advisory spec-diff echo)

**Interfaces:**
- Produces: checker check 5 — every `覆盖章节` §N must correspond to an existing `## N.` heading in the spec file (blocking); `make test-matrix` prints an advisory notice when the spec file is among commits differing from origin/main (echo only, never blocks).

- [ ] **Step 1: Write failing unit tests** for section-existence validation (matrix claiming §99 → violation; §9 → ok) + header-line parsing.
- [ ] **Step 2: Implement** in checker: parse `覆盖章节：` line, map §N → spec heading regex `^## N\.`, report violation `matrix X claims missing spec section §N`.
- [ ] **Step 3: Add the line to all 18 matrix headers** (sections sourced from each file's 规约来源 line; agents' new files from Task 2 get theirs corrected if the campaign reports different sections). Run checker → green.
- [ ] **Step 4: Makefile**: in `test-matrix` recipe, after the checker, `git diff --name-only origin/main...HEAD | grep -q <spec path> && echo advisory notice` listing matrix files + their claimed sections.
- [ ] **Step 5: Run `make test-matrix`** (advisory notice fires — expected, spec untouched this campaign... if spec untouched, notice must NOT fire; verify both branches by testing the grep against the actual diff). Commit `feat(scripts): matrices pin their spec sections; checker + pre-push warn on drift`

### Task 5: Teardown audit re-enumeration (subagent)

**Files:**
- Modify: `docs/quality/teardown-coverage-audit.md`
- Create: append findings to `docs/evidence/2026-10-25-matrix-campaign-findings.md`

- [ ] **Step 1: Dispatch one agent**: read the audit doc's method section, `backend/tests/e2e/browser_world.py::_clean`, `backend/tests/e2e/factories.py::clean_world`, and every current `frontend/e2e/*.spec.ts`; enumerate each spec's real-surface writes (tables/objects) and judge coverage against the cleanup engine's scope parameters; update the audit doc (new spec rows, revised counts, keep the 历史前科 table intact); report new gaps as G-C/D/… (list, not fix).
- [ ] **Step 2: Verify doc consistency** (counts match rows; no orphan G-ids) and merge gaps into the findings file.
- [ ] **Step 3: Commit** `docs(quality): teardown audit re-enumerated for the full 29-spec battery`

### Task 6: Backend mutation campaign (report only)

**Files:**
- Create: `docs/evidence/2026-10-25-mutation-campaign.md`

- [ ] **Step 1: Attempt mutmut** via `uv run --with mutmut mutmut run` with runner `uv run pytest tests/unit/tasks tests/unit/points -x -q` against `app/modules/tasks/claim_service.py` + `app/modules/points/ledger_service.py` (no pyproject changes; config via CLI/env; time-box 45 min wall clock, run in background). If the tool fights, fall back to a scripted hand-mutant set (15 mutants: boundary flips, guard drops, off-by-ones) applied one-at-a-time via sed from scripts under /tmp (nothing committed but the report).
- [ ] **Step 2: Record survivors** (mutant diff + which unit tests ran). NO claims of assertion gaps yet.
- [ ] **Step 3 (inside Task 7's borrow window): survivor cross-check** — for each survivor, run the related integration tests (PG) once; classify: caught-by-integration / genuine assertion gap. Write the classified report.
- [ ] **Step 4: Commit** `docs(evidence): backend mutation campaign — unit-scope survivors classified`

### Task 7: Full gates (borrow protocol)

- [ ] **Step 1: `make verify`** from worktree root (no stack) — must be green; fix campaign-caused failures only.
- [ ] **Step 2: Announce borrow** via SendMessage to `campus reviewer` (borrow 3000/8000 + campusquest_test@15432 for release-gate + survivor cross-check); WAIT for its stack-release reply.
- [ ] **Step 3: `make release-gate`** (background, Monitor for terminal states); while it runs and the borrow is active, run Task 6 Step 3 survivor cross-check.
- [ ] **Step 4: On completion, announce return** to campus reviewer (stack may restart).
- [ ] **Step 5: Assemble delivery evidence** — fresh gate logs paths, findings list summary, matrix count 11→18, checker version note; ask owner about push/PR (out of scope here).
