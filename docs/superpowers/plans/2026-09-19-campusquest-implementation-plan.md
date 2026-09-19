# CampusQuest V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver CampusQuest V1 as a production-ready modular monolith with a Next.js PWA, FastAPI backend, PostgreSQL source of truth, Redis/Celery workers, S3-compatible file storage, and full coverage of the concurrency, deadline, file-validation, points, ranking, community, notification, RBAC, and audit invariants in the approved design.

**Architecture:** The repository is a monorepo with `frontend/`, `backend/`, and `infra/`. The FastAPI application owns all domain state transitions; Celery workers call the same service layer rather than duplicating business rules. PostgreSQL is authoritative, Redis is a cache/queue/ranking projection, and object storage holds uploaded files only.

**Tech Stack:** Next.js + TypeScript; FastAPI + Python; SQLAlchemy 2.x; Alembic; PostgreSQL; Redis; Celery; S3-compatible object storage; pytest + pytest-asyncio; Playwright.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

**Mandatory quality references:**
- `AGENTS.md`
- `docs/quality/quality-gates.md`
- Backend/worker/database tasks: `docs/quality/backend-engineering.md`
- Frontend tasks: `docs/quality/frontend-design-system.md` and `docs/quality/frontend-patterns.md`
- Skills/tool use: `docs/quality/agent-tooling.md`
- External examples: `docs/quality/reference-projects.md`

These references are implementation constraints, not optional reading. External templates/skills never override the approved CampusQuest spec.

## Global Constraints

- Student username is the student number, ASCII digits only, stored as a string, validated against StudentWhitelist, and globally unique.
- Student phone verification is mandatory; one normalized phone number binds to one account.
- nickname is at most 16 Unicode grapheme clusters.
- Student login is student number + password; passwords use Argon2id.
- Teacher/Admin must use 2FA.
- A user may have at most 3 Claims that still require student action; within one Task there may be at most one non-terminal Claim.
- Assignment uniqueness is `UNIQUE(task_id, platform, keyword)`.
- Assignment allocation is random from the student perspective and must be concurrency-safe in PostgreSQL.
- FIXED and RELATIVE deadlines are both supported.
- Reward tiers are 100% / 80% / 50% / 20%; exactly at +4h is 50%, exactly at +12h is 20%, exactly at grace deadline is closed.
- Files support CSV, XLSX, and SQLite/DB only when the actual file type matches; validation runs with resource limits.
- Machine validation precedes human review.
- Points use immutable ledger entries; normal redemptions do not reduce historical ranking contribution.
- Rankings expose daily, monthly, all-time, and around-me views.
- Comments may be public or anonymous to ordinary users; Admin identity reveal is explicit and audited.
- Notifications support SMS, verified email, and in-app channels with idempotent deliveries.
- PostgreSQL is the business source of truth; Redis is rebuildable.
- All backend timestamps are stored as UTC instants; natural-day/month behavior uses one configurable `BUSINESS_TIMEZONE`.
- Core state-changing operations are service-layer methods and must be transactional and idempotent where retries are possible.
- Business time must use an injectable Clock; business logic must not scatter direct `datetime.now()` calls.
- Integration tests for concurrency use a real PostgreSQL instance, not SQLite.

## Review Focus

1. **Race between a student submit and the expiry worker at the grace boundary:** the transaction that sees a valid in-window submission must prevent the Assignment from being re-released.
2. **Two concurrent claim requests by the same student:** the global three-Claim quota and same-Task uniqueness must not be penetrated by concurrent requests.
3. **Two concurrent reward redemptions against the same wallet or last inventory item:** spendable points and stock must never go negative.
4. **Worker retries after partial external failure:** SMS/email/object-storage/ranking jobs must be safely retryable without duplicate business side effects.
5. **Unicode and time-zone boundaries:** 16 grapheme nicknames, business-day reset, month rollover, and deadline exact-boundary instants must behave identically in API, workers, and tests.

Each item above is pinned to explicit tests in the owning subsystem plan.

---

## Repository File Structure

The first implementation task must establish this structure and later agents must follow it unless a reviewed plan amendment changes it:

```text
campus-quest/
├── frontend/
│   ├── package.json
│   ├── next.config.ts
│   ├── playwright.config.ts
│   └── src/
│       ├── app/
│       ├── components/
│       ├── features/
│       │   ├── auth/
│       │   ├── tasks/
│       │   ├── submissions/
│       │   ├── rewards/
│       │   ├── rankings/
│       │   ├── community/
│       │   └── admin/
│       └── lib/
│           ├── api.ts
│           ├── auth.ts
│           └── time.ts
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── clock.py
│   │   │   ├── errors.py
│   │   │   ├── security.py
│   │   │   ├── rbac.py
│   │   │   └── observability.py
│   │   ├── db/
│   │   │   ├── base.py
│   │   │   ├── session.py
│   │   │   └── types.py
│   │   ├── modules/
│   │   │   ├── identity/
│   │   │   ├── tasks/
│   │   │   ├── submissions/
│   │   │   ├── points/
│   │   │   ├── rankings/
│   │   │   ├── community/
│   │   │   ├── notifications/
│   │   │   └── audit/
│   │   ├── integrations/
│   │   │   ├── object_storage.py
│   │   │   ├── sms.py
│   │   │   └── email.py
│   │   └── workers/
│   │       ├── celery_app.py
│   │       └── jobs/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── workers/
│       └── e2e/
├── infra/
│   ├── docker-compose.yml
│   └── env.example
└── docs/superpowers/
    ├── specs/
    └── plans/
```

Rules:

- Domain files live together under their module; do not create one giant `models.py` or `services.py` for the whole application.
- Cross-domain imports go through explicitly documented service/query interfaces.
- Integration adapters are interfaces with fakes for tests.
- Frontend feature code mirrors business modules rather than technical layers.

## Plan Set and Dependency Graph

Execute plans in this order. Plans marked “parallel after gate” may be delegated simultaneously only after the listed prerequisite gate is merged.

1. **Foundation & Contracts**  
   `2026-09-19-campusquest-01-foundation.md`  
   Establish monorepo, dependency setup, config, DB/session, test harness, error contract, Clock, auth primitives, Docker services, CI commands.

2. **Identity & Access**  
   `2026-09-19-campusquest-02-identity-access.md`  
   Depends on 01.

3. **Task, Assignment & Deadline Core**  
   `2026-09-19-campusquest-03-task-assignment.md`  
   Depends on 01 and the User/RBAC interfaces from 02.

4. **Submission & File Validation**  
   `2026-09-19-campusquest-04-submission-validation.md`  
   Depends on 01 and Claim interfaces from 03.

5. **Points, Rewards, Rankings & Honors**  
   `2026-09-19-campusquest-05-points-ranking.md`  
   Depends on 01, 02, 03; approval reward wiring integrates with 04.

6. **Community & Ratings**  
   `2026-09-19-campusquest-06-community.md`  
   Parallel after 02 + 03 interfaces are stable.

7. **Notifications & Scheduled Workers**  
   `2026-09-19-campusquest-07-notifications-workers.md`  
   Depends on 01 + 03; integrates with 04 and 05 events.

8. **Admin, Audit & Operations**  
   `2026-09-19-campusquest-08-admin-operations.md`  
   Depends on service interfaces from 02–07.

9. **Frontend Product Flows**  
   `2026-09-19-campusquest-09-frontend.md`  
   Can begin after API contracts from 02–07 are frozen; API mocking is allowed until backend endpoints land.

10. **End-to-End Hardening & Release Gate**  
    `2026-09-19-campusquest-10-e2e-hardening.md`  
    Depends on all previous plans.

Recommended parallelization after Plan 03 is merged:

```text
                 01 Foundation
                      |
                 02 Identity
                      |
              03 Task/Assignment
              /       |        \
             /        |         \
    04 Submission  06 Community  07 Notifications
         |                          |
         +--------- 05 Points ------+
                        |
                 08 Admin/Ops
                        |
                 09 Frontend
                        |
                 10 E2E/Release
```

The actual execution coordinator may overlap 04/05/06/07/09 more aggressively when their exact interfaces are already merged and stable.

---

### Task 1: Freeze Cross-Plan Interfaces

**Files:**
- Create: `docs/architecture/interfaces.md`
- Test: human review against the spec and child-plan interface blocks

**Interfaces:**
- Consumes: approved design spec.
- Produces: canonical names for shared enums, Clock, actor context, domain events, error envelope, storage adapters, and service calls used by all child plans.

- [ ] **Step 1: Create the interface contract document**

Write exact signatures for:

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

@dataclass(frozen=True)
class Actor:
    user_id: UUID
    role: Role

@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    occurred_at: datetime
    payload: Mapping[str, Any]
```

Also define canonical enum names used across plans: `Role`, `UserStatus`, `TaskStatus`, `DeadlineMode`, `AssignmentAvailability`, `ClaimStatus`, `ValidationStatus`, `ReviewStatus`, `RewardLockStatus`, `RedemptionStatus`, and `NotificationChannel`.

- [ ] **Step 2: Verify every child plan uses the same names**

Run:

```bash
rg "ClaimStatus|RewardLockStatus|RedemptionStatus|NotificationChannel" docs/superpowers/plans docs/architecture/interfaces.md
```

Expected: names are consistent; no alternative names such as `ACTIVE_CLAIM` vs `IN_PROGRESS` appear without a mapping.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/interfaces.md docs/superpowers/plans
git commit -m "docs: freeze CampusQuest cross-module interfaces"
```

### Task 2: Execute Child Plans Through Review Gates

**Files:**
- Modify: repository files listed in each child plan.
- Test: commands listed in each child plan.

**Interfaces:**
- Consumes: `docs/architecture/interfaces.md`.
- Produces: merged, independently verified domain modules.

- [ ] **Step 1: Execute Plan 01 and run its full gate**

Run exactly the verification commands in `2026-09-19-campusquest-01-foundation.md`. Do not start domain work while the test harness or database container is unreliable.

- [ ] **Step 2: Execute Plan 02 and freeze Identity interfaces**

Required outcome: test-created Student/Teacher/Admin actors, RBAC helpers, sessions, and verified identity API work before Task/Assignment depends on them.

- [ ] **Step 3: Execute Plan 03 and run the 50-request/10-assignment concurrency test**

Do not accept the module if 11 requests succeed, duplicate assignment IDs appear, or a 500 is returned for exhausted inventory.

- [ ] **Step 4: Execute Plans 04, 06, and 07 behind their prerequisite gates**

Each plan must pass its own unit/integration/worker tests before its public interfaces are consumed by later plans.

- [ ] **Step 5: Execute Plan 05 after reward-approval integration points from 04 are stable**

Verify the points ledger is immutable and redemption concurrency tests pass before ranking UI work relies on balances.

- [ ] **Step 6: Execute Plan 08 only through existing services**

Admin routes must not introduce direct database writes that bypass domain services.

- [ ] **Step 7: Execute Plan 09 against generated OpenAPI plus test fixtures**

Frontend must not duplicate deadline reward calculations; render values returned by backend APIs.

- [ ] **Step 8: Execute Plan 10 and stop release on any invariant failure**

The release gate includes full backend tests, worker tests, frontend checks, Playwright, migration-from-empty, Redis rebuild, and concurrency suites.

- [ ] **Step 9: Commit each approved task separately**

Each child task ends in its own commit. Avoid a single “implement CampusQuest” commit because it destroys reviewability and rollback boundaries.

### Task 3: Final Whole-Branch Review

**Files:**
- Review: all files changed by Plans 01–10.
- Test: commands in Plan 10.

**Interfaces:**
- Consumes: all completed modules.
- Produces: a release candidate that satisfies the approved design.

- [ ] **Step 1: Re-read the approved spec before code review**

Compare each acceptance criterion in section 45 of the design spec to a concrete automated test or explicit release check.

- [ ] **Step 2: Run the full verification suite**

The exact command is defined by Plan 10 after foundation scripts exist. It must execute backend unit/integration/worker tests, frontend type/lint/unit checks, Playwright, and migration verification.

- [ ] **Step 3: Inspect the database constraints**

Confirm migrations contain database-level uniqueness/check constraints for assignment uniqueness, active claims, votes, reactions, ratings, notification deliveries, and reward ledger idempotency.

- [ ] **Step 4: Inspect security-sensitive paths**

Review authentication cookies/tokens, 2FA enforcement, anonymous identity reveal, object-storage authorization, upload parsing isolation, and audit logging.

- [ ] **Step 5: Commit release-gate fixes separately**

```bash
git add -A
git commit -m "fix: close CampusQuest release-gate findings"
```

Only create this commit if the review produces changes.

## Execution Notes for Agent Coordinator

- Preferred execution mode is **subagent-driven** because the system has multiple independent domains and failures in concurrency/points/file handling have high correctness cost.
- Give each implementation agent only the approved spec, `docs/architecture/interfaces.md`, its own child plan, and the current repository state.
- After each implementation task, use a fresh reviewer before moving to the next task.
- Do not let parallel agents independently edit the same migration head or central interface file; assign ownership or sequence those edits.
- Database migration revisions are serialized even if application work is parallel.
- When a child plan discovers a true spec ambiguity, stop that task and amend the spec/plan rather than inventing behavior inside code.
