# Test Gap Closures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax for tracking.

**Goal:** Close the twelve test-side light gaps the coverage campaign listed (owner authorization 2026-10-26: "还没做的内容现在可以做了") — each is a pin test plus its matrix row flip; product-level gaps (honors producer, display_honor endpoint, UNDER_REVIEW semantics, audit G-1/G-2) stay listed, and 9b's G-6/G-7 stay in the innovation queue.

**Architecture:** Three parallel domain agents (points/submissions/M10; audit/retention; ratings/rewards) each write tests + flip their matrix rows; findings-doc update and the integration-suite borrow window are centralized in the coordinating session. Branch `test-gap-closures` off origin/main @ bbbb941.

**Spec:** `docs/evidence/2026-10-25-matrix-campaign-findings.md` (gap list) + the owning matrix files.

## Global Constraints

- Gap closure = NEW TESTS + matrix-row updates only. No production-code changes. If a gap turns out to be a product hole (e.g. no length cap exists), REPORT it — do not invent behavior.
- New integration test files MUST carry the pytestmark marker (repo trap). Integration tests are WRITTEN but not run by agents (no stack borrow) — unit-runnable parts must be run green by the agent.
- Matrix rows flip 缺口→已覆盖 with name-level citations; gap sections note "已补（本批）"; summary counts updated; checker zero violations.
- Findings doc (`docs/evidence/2026-10-25-matrix-campaign-findings.md`) is updated ONLY by the coordinator (shared file).
- ruff check + format --check + mypy on every new/edited file.

## Review Focus

1. Freeze tests must assert the SET (model_fields / column types), not just an example — the campaign's G-1 wording.
2. Audit-row tests must assert action + actor + target, not row-count only.
3. Matrix citations validated name-level (checker enforces).
4. No exemption entries needed (no new skips).
5. Unit/integration placement follows the neighboring tests in the same domain.

---

### Task 1 (agent B1): points + submissions + M10
Close points-ledger G-1 (RankingEntryResponse field-set freeze), G-2 (points columns all-Integer freeze), submissions-validation G-4 (original_filename length/clean assertions), M10 (task-reward amount==0 rejection pin). Update points-ledger.md, submissions-validation.md.

### Task 2 (agent B2): audit-log + file-retention
Close audit-log G-3 (EMAIL_LOGIN_SUCCEEDED/TOTP_REQUIRED audit-row assertions), G-4 (MOBILE_LOGIN_SUCCEEDED), file-retention G-1 (retention enum closed-set DB rejection), G-2 (default retention policy assertion), G-3 (audit rows survive cleanup). Update audit-log.md, file-retention.md.

### Task 3 (agent B3): task-ratings + rewards
Close task-ratings G-5 (TaskRating field set + updated_at forward), rewards-redemption G-1 (advisory fields persisted/returned), G-3 (FULFILLED converts reservation to permanent spend). Update task-ratings.md, rewards-redemption.md.

### Task 4 (coordinator): merge agent outputs
Run checker (28 files) + unit suites + lint trio; update findings doc with closure list; single borrow window for integration legs; pre-push; PR.
