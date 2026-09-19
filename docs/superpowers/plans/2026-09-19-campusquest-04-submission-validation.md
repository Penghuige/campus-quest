# CampusQuest 04 Submission & File Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement presigned file upload, immutable Submission versions, safe CSV/XLSX/SQLite validation, structured reports, reward-lock creation, revision flow, malicious-lock invalidation, and human approval integration.

**Architecture:** Uploaded files live in object storage; Submission metadata and validation reports live in PostgreSQL. Worker parsers operate with resource limits and return structured validation results; service methods own Claim transitions and reward-lock semantics.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Celery, S3 adapter, Python CSV parser, openpyxl read-only mode, sqlite3 read-only mode.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

## Global Constraints

- Supported types: CSV, XLSX, SQLite/DB only when detected type matches.
- File size default 200 MB and Task-configurable.
- Machine validation must pass before human review.
- First valid machine-passed Submission locks the reward tier by `submitted_at`.
- Teacher review time must not reduce student reward.
- Normal revision preserves the first valid reward lock.
- Malicious/empty-shell lock invalidation requires reason and audit event.
- At or after grace deadline, a new ordinary Submission is not valid unless the Claim is in an explicit revision window.
- Worker retries are idempotent.

## Review Focus

1. A fake `.csv` or fake `.sqlite` file must fail by actual file detection.
2. Zip bombs, huge rows/cells, parser timeouts, and multi-table SQLite ambiguity must fail safely rather than exhaust the worker.
3. Submit-vs-expire race at the grace boundary must produce one valid state.
4. Repeated upload-complete callbacks must not create duplicate Submission versions.
5. Revision after late Teacher review must extend to at least `reviewed_at + 24h` without changing a valid original reward tier.

---

### Task 1: Add Submission and Validation Models

**Files:**
- Create: `backend/app/modules/submissions/enums.py`
- Create: `backend/app/modules/submissions/models.py`
- Create: `backend/alembic/versions/0004_submissions.py`
- Create: `backend/tests/integration/submissions/test_submission_constraints.py`

**Interfaces:**
- Produces `Submission`, `SubmissionValidation`, `SubmissionReview`, optional `RewardLockHistory`; enums `ValidationStatus`, `ReviewStatus`, `RewardLockStatus`.

- [ ] **Step 1: Write constraint tests**

Assert version is unique within Claim and monotonically created by service; duplicate `(claim_id, version)` fails.

- [ ] **Step 2: Implement models and migration**

Submission stores object key, original filename, detected type, size, submitted_at, status, validation report reference, `retention_until` or explicit permanent-retention flag, `legal_hold`, and file-deleted metadata. Claim stores latest_submission_id and current reward-lock projection.

- [ ] **Step 3: Run migration/test and commit**

```bash
git add backend/app/modules/submissions backend/alembic/versions/0004_submissions.py backend/tests/integration/submissions
git commit -m "feat: add submission persistence"
```

### Task 2: Implement Upload Intent and Finalization

**Files:**
- Create: `backend/app/modules/submissions/upload_service.py`
- Create: `backend/app/modules/submissions/schemas.py`
- Create: `backend/tests/unit/submissions/test_upload_policy.py`
- Create: `backend/tests/integration/submissions/test_upload_finalize.py`

**Interfaces:**
- Produces:
  - `create_upload_intent(actor, claim_id, filename, declared_type, size) -> UploadIntent`
  - `finalize_upload(actor, intent_id) -> Submission`

- [ ] **Step 1: Write failing policy tests**

Reject:
- wrong owner;
- terminal Claim;
- unsupported type;
- declared size > Task limit;
- ordinary upload at/after grace without revision window;
- SUSPENDED user.

- [ ] **Step 2: Write duplicate-finalize test**

Calling finalize twice on the same intent must return the same Submission or stable already-finalized result, never create version 2.

- [ ] **Step 3: Implement server-generated object keys**

Key shape may be `submissions/{claim_id}/{uuid}`; never include untrusted path fragments.

- [ ] **Step 4: Verify object metadata and snapshot retention on finalize**

Call `head_object`, compare size/content metadata, and persist `submitted_at` from server Clock at accepted finalize time. Snapshot the Task file-retention policy into file metadata: for 30/90/180-day policies persist `retention_until = submitted_at + configured duration`; for permanent retention persist an explicit permanent flag/null expiry. Later Task policy edits must not rewrite this snapshot.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/submissions/upload_service.py backend/app/modules/submissions/schemas.py backend/tests
git commit -m "feat: create and finalize secure uploads"
```

### Task 3: Implement Submission Schema Parser

**Files:**
- Create: `backend/app/modules/submissions/schema.py`
- Create: `backend/tests/unit/submissions/test_schema.py`

**Interfaces:**
- Produces `SubmissionSchema.parse(json) -> SubmissionSchema` and typed column rules.

- [ ] **Step 1: Write failing schema tests**

Cover required/optional columns, `allow_extra_columns`, min/max rows, nullable, unique, max_null_ratio, table/sheet selector, and invalid types.

- [ ] **Step 2: Implement fixed DSL**

Allowed field types: string, integer, number, boolean, datetime. Reject unknown keys/types instead of ignoring them.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules/submissions/schema.py backend/tests/unit/submissions/test_schema.py
git commit -m "feat: define submission validation schema"
```

### Task 4: Implement CSV Validator

**Files:**
- Create: `backend/app/modules/submissions/validators/csv_validator.py`
- Create: `backend/app/modules/submissions/validators/common.py`
- Create: `backend/tests/unit/submissions/test_csv_validator.py`

**Interfaces:**
- Produces `validate_csv(stream, schema, limits) -> ValidationReport`.

- [ ] **Step 1: Write fixture tests**

Cases:
- valid UTF-8 CSV;
- UTF-8 BOM;
- empty file;
- header only;
- missing field;
- duplicate unique URL;
- min_rows-1 and exactly min_rows;
- max_rows+1;
- invalid datetime;
- extremely long cell;
- binary masquerading as CSV.

- [ ] **Step 2: Implement streaming validation**

Do not load entire file into memory. Track counts and bounded samples only. Set explicit max columns, max cell length, row cap, and timeout cooperative checks.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules/submissions/validators backend/tests/unit/submissions/test_csv_validator.py
git commit -m "feat: validate CSV submissions safely"
```

### Task 5: Implement XLSX Validator

**Files:**
- Create: `backend/app/modules/submissions/validators/xlsx_validator.py`
- Create: `backend/tests/unit/submissions/test_xlsx_validator.py`

**Interfaces:**
- Produces `validate_xlsx(path, schema, limits) -> ValidationReport`.

- [ ] **Step 1: Write fixtures**

Create:
- valid workbook;
- target sheet missing;
- multiple sheets with specified selector;
- huge empty dimension;
- formula cell;
- archive with suspicious compression ratio.

- [ ] **Step 2: Implement preflight zip checks**

Before openpyxl, inspect ZIP entry count, total uncompressed size, compression ratio, and file names. Reject suspicious archives with stable validation code.

- [ ] **Step 3: Open workbook read-only/data-only without executing formulas**

Treat formula cells as data strings or warnings according to schema; never evaluate them.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/submissions/validators/xlsx_validator.py backend/tests/unit/submissions/test_xlsx_validator.py
git commit -m "feat: validate XLSX submissions with resource guards"
```

### Task 6: Implement SQLite Validator

**Files:**
- Create: `backend/app/modules/submissions/validators/sqlite_validator.py`
- Create: `backend/tests/unit/submissions/test_sqlite_validator.py`

**Interfaces:**
- Produces `validate_sqlite(path, schema, limits) -> ValidationReport`.

- [ ] **Step 1: Write tests**

Cover:
- valid SQLite header and one user table;
- fake DB file;
- multiple user tables without selector -> fail;
- selected table missing -> fail;
- schema mismatch;
- row-limit enforcement.

- [ ] **Step 2: Implement read-only connection**

Use URI `mode=ro`; set `PRAGMA query_only=ON`; disable extension loading; never execute uploaded SQL. Generate only service-owned `SELECT`/schema introspection statements with properly quoted identifiers.

- [ ] **Step 3: Add timeout/progress handler**

Abort queries beyond configured instruction/time budget.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/submissions/validators/sqlite_validator.py backend/tests/unit/submissions/test_sqlite_validator.py
git commit -m "feat: validate SQLite submissions read only"
```

### Task 7: Implement Validation Worker and Structured Report

**Files:**
- Create: `backend/app/modules/submissions/validation_service.py`
- Create: `backend/app/workers/jobs/validate_submission.py`
- Create: `backend/tests/workers/test_submission_validation_job.py`

**Interfaces:**
- Produces `ValidationService.validate_submission(submission_id) -> ValidationReport`.

- [ ] **Step 1: Write retry/idempotency test**

Execute the same worker job twice. First run validates and persists report. Second run detects terminal validation state and returns same result without creating duplicate report/history.

- [ ] **Step 2: Implement actual type detection**

Detect XLSX ZIP structure, SQLite header, and text CSV plausibility; compare with allowed Task types. Mismatch -> `FILE_TYPE_NOT_ALLOWED` / validation fail.

- [ ] **Step 3: Persist structured report**

Include parser version, row count, columns, missing/extra columns, type errors, null ratios, duplicate counts, warnings, errors, duration, plus at most a configured small number of sanitized preview rows. Preview values are plain data only; formulas are never evaluated, large cells are truncated for preview, and sensitive parser internals/object keys are not returned to Student-facing responses.

- [ ] **Step 4: Run worker tests and commit**

```bash
git add backend/app/modules/submissions/validation_service.py backend/app/workers/jobs/validate_submission.py backend/tests/workers
git commit -m "feat: validate uploaded submissions asynchronously"
```

### Task 8: Implement Reward Lock and Claim Transition After Validation

**Files:**
- Create: `backend/app/modules/submissions/reward_lock_service.py`
- Create: `backend/tests/integration/submissions/test_reward_lock.py`

**Interfaces:**
- Produces `on_validation_passed(submission_id) -> AssignmentClaim`.

- [ ] **Step 1: Write reward-lock tests**

- first valid on-time Submission -> PROVISIONAL 100%;
- first valid at +2h -> 80%;
- later valid revision does not lower existing non-invalidated lock;
- INVALIDATED previous lock permits next valid Submission to establish new fraction.

- [ ] **Step 2: Implement transaction**

Lock Claim, ensure Submission belongs to it and is validated, compute tier from `submission.submitted_at`, persist lock and transition Claim to UNDER_REVIEW.

- [ ] **Step 3: Write submit-vs-expire race test**

Run validation-pass transaction and expiry candidate concurrently at grace boundary. If Submission was finalized before grace, the Assignment must not become available after reward-lock transaction completes.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/submissions/reward_lock_service.py backend/tests/integration/submissions/test_reward_lock.py
git commit -m "feat: lock submission rewards from valid submit time"
```

### Task 9: Implement Human Review and Revision Window

**Files:**
- Create: `backend/app/modules/submissions/review_service.py`
- Create: `backend/tests/integration/submissions/test_review_flow.py`

**Interfaces:**
- Produces:
  - `require_revision(actor, submission_id, note) -> AssignmentClaim`
  - `invalidate_reward_lock(actor, submission_id, reason) -> AssignmentClaim`
  - `approve_submission(actor, submission_id) -> ApprovalResult`

- [ ] **Step 1: Write late-review revision test**

A submission made on time is reviewed two days after grace. `revision_deadline_at == reviewed_at + 24h` if later than original grace; reward remains 100%.

- [ ] **Step 2: Write invalidation test**

Reason is mandatory. State records INVALIDATED and emits an audit event. A later valid +7h submission locks 50%.

- [ ] **Step 3: Implement permission and current-version checks**

Only Task owner/collaborator with review permission/Admin may review. Stale Submission version cannot be approved after a newer version becomes current.

- [ ] **Step 4: Keep approval reward issuance behind an interface**

Call `PointsRewardPort.grant_assignment_reward(...)`; concrete points implementation arrives in Plan 05. Use fake port in this plan's tests.

- [ ] **Step 5: Run and commit**

```bash
git add backend/app/modules/submissions/review_service.py backend/tests/integration/submissions/test_review_flow.py
git commit -m "feat: review revise and invalidate submissions"
```

### Task 10: Add Submission APIs

**Files:**
- Create: `backend/app/modules/submissions/router.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/integration/submissions/test_submission_api.py`

**Interfaces:**
- Produces upload-intent/finalize/status endpoints and Teacher review endpoints.

- [ ] **Step 1: Write API flow test**

Claim owner requests upload -> fake object uploaded -> finalize -> validation job invoked -> validation report readable. Another Student receives 403.

- [ ] **Step 2: Implement thin routes**

Do not stream the 200 MB file through FastAPI. Return presigned URL and later short-lived download URLs after authorization.

- [ ] **Step 3: Run module gate**

```bash
cd backend
pytest tests/unit/submissions tests/integration/submissions tests/workers/test_submission_validation_job.py -v
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/modules/submissions/router.py backend/app/main.py backend/tests
git commit -m "feat: expose submission and review APIs"
```
