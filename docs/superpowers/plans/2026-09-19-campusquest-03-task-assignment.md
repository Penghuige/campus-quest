# CampusQuest 03 Task, Assignment & Deadline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Task lifecycle, Assignment import, random concurrency-safe claiming, claim quotas, abandon/release behavior, FIXED/RELATIVE deadlines, and the exact 100/80/50/20 reward-tier boundary calculator.

**Architecture:** Task configuration is snapshotted into each AssignmentClaim at claim time. Assignment is the reusable work unit; AssignmentClaim is immutable history of one user's attempt. PostgreSQL row locks plus a user-level serialization lock enforce allocation invariants.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, PostgreSQL, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

## Global Constraints

- Only Teacher/Admin create Tasks.
- Students cannot choose a specific Assignment.
- `UNIQUE(task_id, platform, keyword)`.
- At most one active Claim per Assignment.
- At most one non-terminal Claim per user per Task.
- At most three Claims requiring student action globally.
- CLAIMED and REVISION_REQUIRED consume quota; VALIDATING/UNDER_REVIEW do not.
- FIXED and RELATIVE deadlines are supported.
- Reward boundaries are exact and server-authoritative.
- All time uses injected Clock and UTC-aware instants.

## Review Focus

1. Fifty concurrent users competing for ten Assignments must yield exactly ten successes and no duplicates.
2. Two concurrent claim calls by one user must not bypass the three-Claim quota or same-Task uniqueness.
3. Exact +4h, +12h, and grace boundary comparisons must match the spec.
4. A Task edit after claim creation must not mutate the Claim's reward/deadline/schema snapshot.
5. Abandon and expiry must preserve Claim history while returning only eligible Assignments to AVAILABLE.

---

### Task 1: Add Task and Assignment Models

**Files:**
- Create: `backend/app/modules/tasks/enums.py`
- Create: `backend/app/modules/tasks/models.py`
- Create: `backend/alembic/versions/0003_tasks_assignments.py`
- Create: `backend/tests/integration/tasks/test_task_constraints.py`

**Interfaces:**
- Produces `Task`, `TaskCollaborator`, `Assignment`, `AssignmentClaim`; enums `TaskStatus`, `DeadlineMode`, `AssignmentAvailability`, `ClaimStatus`, `TaskRarity`.

- [ ] **Step 1: Write failing database-constraint tests**

Assert:
- duplicate `(task_id, platform, keyword)` fails;
- duplicate active Claim for one Assignment fails;
- duplicate non-terminal Claim for one user/task fails;
- completed Assignment cannot be selected by AVAILABLE query.

- [ ] **Step 2: Run tests**

Run: `cd backend && pytest tests/integration/tasks/test_task_constraints.py -v -m integration`. Expected: FAIL.

- [ ] **Step 3: Implement models and partial indexes**

Use PostgreSQL partial unique indexes for active Claim uniqueness and same-user/task non-terminal uniqueness. Store task reward/schema/deadline snapshots on Claim.

- [ ] **Step 4: Add migration and verify from empty DB**

Run `alembic downgrade base && alembic upgrade head`.

- [ ] **Step 5: Re-run tests and commit**

```bash
git add backend/app/modules/tasks backend/alembic/versions/0003_tasks_assignments.py backend/tests/integration/tasks
git commit -m "feat: add task assignment and claim persistence"
```

### Task 2: Implement Task Lifecycle and Publish Validation

**Files:**
- Create: `backend/app/modules/tasks/schemas.py`
- Create: `backend/app/modules/tasks/service.py`
- Create: `backend/tests/unit/tasks/test_task_lifecycle.py`

**Interfaces:**
- Produces `TaskService.create_task`, `publish_task`, `pause_task`, `resume_task`, `close_task`.

- [ ] **Step 1: Write failing lifecycle tests**

Cover allowed transitions `DRAFT->PUBLISHED->PAUSED->PUBLISHED->CLOSED->ARCHIVED` and reject direct `DRAFT->ARCHIVED`. Verify FIXED requires `fixed_deadline_at`; RELATIVE requires positive `duration_minutes`. Also assert PAUSE and default CLOSE stop new claims without cancelling or rewriting already-existing Claims.

- [ ] **Step 2: Run tests and verify failure**

- [ ] **Step 3: Implement transition table**

```python
ALLOWED_TASK_TRANSITIONS = {
    TaskStatus.DRAFT: {TaskStatus.PUBLISHED},
    TaskStatus.PUBLISHED: {TaskStatus.PAUSED, TaskStatus.CLOSED},
    TaskStatus.PAUSED: {TaskStatus.PUBLISHED, TaskStatus.CLOSED},
    TaskStatus.CLOSED: {TaskStatus.ARCHIVED},
    TaskStatus.ARCHIVED: set(),
}
```

Publishing must validate reward points >0, file policy, deadline policy, and submission schema presence.

- [ ] **Step 4: Run tests and commit**

```bash
git add backend/app/modules/tasks/schemas.py backend/app/modules/tasks/service.py backend/tests/unit/tasks/test_task_lifecycle.py
git commit -m "feat: enforce task lifecycle"
```

### Task 3: Implement Task Collaborators and Task Statistics

**Files:**
- Create: `backend/app/modules/tasks/collaborator_service.py`
- Create: `backend/app/modules/tasks/query_service.py`
- Create: `backend/tests/integration/tasks/test_collaborators.py`
- Create: `backend/tests/integration/tasks/test_task_statistics.py`

**Interfaces:**
- Produces:
  - `add_collaborator(owner_actor, task_id, teacher_id, permissions) -> TaskCollaborator`
  - `remove_collaborator(owner_actor, task_id, teacher_id) -> None`
  - `get_task_statistics(actor, task_id, rating_port: RatingSummaryPort) -> TaskStatistics`
  - `RatingSummaryPort.summary(task_id) -> RatingSummary | None`

Plan 03 ships a null/fake `RatingSummaryPort` so it does not import the later Community module. Plan 06 supplies the concrete adapter backed by TaskRating.

- [ ] **Step 1: Write collaborator permission tests**

Task owner may add/remove a Teacher collaborator. A collaborator cannot grant permissions beyond those they possess, and an unrelated Teacher cannot modify collaborators. Review permission is a distinct capability consumed by Submission review.

- [ ] **Step 2: Implement explicit permission set**

Use named booleans/enum capabilities such as `VIEW_TASK`, `MANAGE_ASSIGNMENTS`, `REVIEW_SUBMISSIONS`, `MODERATE_COMMUNITY`; do not represent authorization as an unchecked free-form JSON blob.

- [ ] **Step 3: Write task-statistics tests**

For seeded Claims/Submissions, return available/occupied/completed Assignment counts, active Claim counts, submission status counts, completion rate, and rating summary when supplied by `RatingSummaryPort`. Teacher may query only owned/collaborating Tasks; Admin may query all.

- [ ] **Step 4: Implement aggregate query without exposing hidden Assignment payloads to unauthorized callers**

- [ ] **Step 5: Run and commit**

```bash
git add backend/app/modules/tasks/collaborator_service.py backend/app/modules/tasks/query_service.py backend/tests/integration/tasks
git commit -m "feat: manage task collaborators and statistics"
```

### Task 4: Implement Assignment Import Preview and Confirm

**Files:**
- Create: `backend/app/modules/tasks/importer.py`
- Create: `backend/tests/unit/tasks/test_assignment_importer.py`
- Create: `backend/tests/integration/tasks/test_assignment_import.py`

**Interfaces:**
- Produces:
  - `preview_assignments(task_id, file) -> AssignmentImportPreview`
  - `confirm_assignments(task_id, preview_token) -> AssignmentImportResult`

- [ ] **Step 1: Write preview tests**

Use an in-memory CSV with:
- valid rows;
- blank keyword;
- unsupported platform;
- duplicate within file;
- duplicate against database.

Assert row-level errors include row number and stable code.

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement canonicalization**

Platform values map to controlled codes `xiaohongshu`, `douyin`, `zhihu`. Trim keyword edges without altering internal Unicode. Preview stores a short-lived immutable payload/token.

- [ ] **Step 4: Write concurrent confirm test**

Two independent confirm operations containing the same new pair race; exactly one insert succeeds, the other returns a duplicate conflict rather than 500.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/tasks/importer.py backend/tests/unit/tasks backend/tests/integration/tasks
git commit -m "feat: preview and import task assignments safely"
```

### Task 5: Implement Deadline and Reward-Tier Calculator

**Files:**
- Create: `backend/app/modules/tasks/deadlines.py`
- Create: `backend/tests/unit/tasks/test_deadlines.py`

**Interfaces:**
- Produces:
  - `compute_claim_deadlines(task, claimed_at) -> ClaimDeadlines`
  - `reward_fraction(submitted_at, deadline_at, grace_deadline_at) -> Decimal`
  - `reward_points(base_points, fraction) -> int`

- [ ] **Step 1: Write exact-boundary tests**

Create tests at:
- deadline -1ms, deadline, deadline +1ms;
- +4h -1ms, +4h, +4h +1ms;
- +12h -1ms, +12h, +12h +1ms;
- grace -1ms, grace, grace +1ms.

Expected fractions: 1.0, 0.8, 0.5, 0.2, then closed at grace.

- [ ] **Step 2: Write integer-floor tests**

Assert `reward_points(101, Decimal("0.8")) == 80`, 50% -> 50, 20% -> 20.

- [ ] **Step 3: Implement with Decimal and explicit comparisons**

Do not use binary float. Raise `SUBMISSION_WINDOW_CLOSED` at or after grace.

- [ ] **Step 4: Test FIXED vs RELATIVE snapshot**

FIXED uses task instant; RELATIVE uses `claimed_at + duration`. In both modes V1 sets `grace_deadline_at = deadline_at + 24h` exactly; no per-Task grace override is exposed. Editing the task afterward does not change persisted Claim values.

- [ ] **Step 5: Run and commit**

```bash
git add backend/app/modules/tasks/deadlines.py backend/tests/unit/tasks/test_deadlines.py
git commit -m "feat: calculate claim deadlines and reward tiers"
```

### Task 6: Implement Concurrency-Safe Random Claiming

**Files:**
- Create: `backend/app/modules/tasks/claim_service.py`
- Create: `backend/tests/integration/tasks/test_claim_concurrency.py`

**Interfaces:**
- Consumes `Actor`, `Clock`, Task/Assignment repositories.
- Produces `ClaimService.claim_random_assignment(user_id, task_id) -> AssignmentClaim`.

- [ ] **Step 1: Write the 50-for-10 failing concurrency test**

Seed 10 AVAILABLE Assignments and 50 ACTIVE Students. Launch 50 independent transactions concurrently.

Assert:
- exactly 10 successes;
- 10 unique assignment IDs;
- 40 `NO_ASSIGNMENT_AVAILABLE`;
- no 500;
- DB has exactly 10 active Claims.

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement per-user serialization**

Before quota checks, lock the User row or dedicated quota row with `FOR UPDATE` so concurrent claims by the same user serialize.

- [ ] **Step 4: Implement Assignment selection**

Use `FOR UPDATE SKIP LOCKED` on AVAILABLE Assignments and exclude any Assignment previously ABANDONED/EXPIRED by that user.

- [ ] **Step 5: Snapshot claim contract**

Persist base reward, deadlines, reward policy, submission schema version, and claim time in the same transaction.

- [ ] **Step 6: Add same-user concurrent tests**

Launch two simultaneous claims for the same user and same Task; at most one succeeds. Seed two existing CLAIMED Claims and simultaneously request two new Tasks; only one additional claim may succeed because the quota is 3.

- [ ] **Step 7: Run and commit**

```bash
git add backend/app/modules/tasks/claim_service.py backend/tests/integration/tasks/test_claim_concurrency.py
git commit -m "feat: allocate assignments safely under concurrency"
```

### Task 7: Implement Claim Cutoff and Quota Semantics

**Files:**
- Modify: `backend/app/modules/tasks/claim_service.py`
- Create: `backend/tests/unit/tasks/test_claim_eligibility.py`
- Create: `backend/tests/integration/tasks/test_claim_quota.py`

**Interfaces:**
- Produces `ClaimEligibilityService.check(...)`.

- [ ] **Step 1: Write cutoff tests with FrozenClock**

FIXED task at 4h01m remaining -> allowed; at exactly 4h -> allowed because the approved rule stops new claims only when remaining time is **less than** 4h; at 3h59m -> blocked. RELATIVE ignores global fixed cutoff.

- [ ] **Step 2: Write quota-status tests**

CLAIMED and REVISION_REQUIRED count. VALIDATING, UNDER_REVIEW, COMPLETED, ABANDONED, EXPIRED do not.

- [ ] **Step 3: Implement and run tests**

Return stable errors `ASSIGNMENT_LIMIT_REACHED`, `TASK_ACTIVE_CLAIM_EXISTS`, `CLAIM_CUTOFF_REACHED`, `TASK_NOT_CLAIMABLE`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/modules/tasks/claim_service.py backend/tests
git commit -m "feat: enforce claim cutoff and active work quotas"
```

### Task 8: Implement Abandon and Release

**Files:**
- Create: `backend/app/modules/tasks/abandon_service.py`
- Create: `backend/tests/integration/tasks/test_abandon.py`

**Interfaces:**
- Produces `abandon_claim(user_id, claim_id) -> AssignmentClaim`.

- [ ] **Step 1: Write daily-limit tests**

Using BUSINESS_TIMEZONE:
- first and second abandon in one local day succeed;
- third fails;
- after local midnight next abandon succeeds;
- two concurrent calls on the same Claim increment count once only.

- [ ] **Step 2: Write reallocation test**

After A abandons Assignment X, X is AVAILABLE and B can receive it. A cannot receive X again in this Task lifecycle.

- [ ] **Step 3: Implement atomically**

Lock Claim and user quota resource. Set Claim ABANDONED and Assignment AVAILABLE in one transaction.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/tasks/abandon_service.py backend/tests/integration/tasks/test_abandon.py
git commit -m "feat: abandon and release assignments safely"
```

### Task 9: Add Task and Claim APIs

**Files:**
- Create: `backend/app/modules/tasks/router.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/integration/tasks/test_task_api.py`

**Interfaces:**
- Produces:
  - `GET /api/v1/tasks`
  - `GET /api/v1/tasks/{id}`
  - `POST /api/v1/tasks/{id}/claim`
  - `POST /api/v1/claims/{id}/abandon`
  - Teacher task CRUD/publish/pause/import preview/confirm routes.
  - Teacher collaborator management routes.
  - `GET /api/v1/teacher/tasks/{id}/statistics`.

- [ ] **Step 1: Write API permission tests**

Student cannot create Task. Teacher cannot edit another Teacher's Task unless collaborator permission exists. Student cannot pass assignment_id in claim request to choose work.

- [ ] **Step 2: Implement thin routes and pagination**

Task list returns availability count but not the hidden full Assignment list.

- [ ] **Step 3: Run module gate**

```bash
cd backend
pytest tests/unit/tasks tests/integration/tasks -v
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/modules/tasks/router.py backend/app/main.py backend/tests
git commit -m "feat: expose task and claim APIs"
```
