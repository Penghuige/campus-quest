# CampusQuest Backend Engineering Standard

> This document defines maintainability and correctness expectations for FastAPI, SQLAlchemy, Celery, PostgreSQL, and integration code.

## 1. Architecture goal

CampusQuest is a **modular monolith**, not a collection of mini-services.

Each domain module should be understandable in isolation:

router -> application/domain service -> repository/query + ports -> PostgreSQL/Redis/external adapters

The important boundary is ownership, not a mandatory class count.

A small use case does not need ceremonial layers. A high-risk multi-table transition does need one clear service transaction.

## 2. Module shape

Preferred pattern:

~~~text
app/modules/tasks/
├── enums.py
├── models.py
├── schemas.py
├── repository.py        # when persistence queries justify it
├── query_service.py     # read models and aggregates
├── service.py           # lifecycle and use cases
├── claim_service.py     # focused high-risk behavior
├── router.py
└── ports.py             # only when this module owns an abstraction
~~~

Do not force every module to have every file.

Split by behavior when a file becomes difficult to reason about. File length is a signal, not a hard metric.

## 3. Router standard

Routers own HTTP concerns:

- request parsing;
- dependency resolution;
- authorization dependency call;
- service invocation;
- response serialization;
- HTTP and business error translation where centralized middleware is insufficient.

Routers do not own:

- multi-table transactions;
- reward calculation;
- claim allocation;
- ledger posting;
- deadline state transitions;
- direct Celery business decisions.

Target shape:

~~~python
@router.post("/{task_id}/claim", response_model=ClaimResponse)
async def claim_task(
    task_id: UUID,
    actor: Annotated[Actor, Depends(require_active_student)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[ClaimService, Depends(get_claim_service)],
) -> ClaimResponse:
    claim = await service.claim_random_assignment(
        session=session,
        user_id=actor.user_id,
        task_id=task_id,
    )
    return ClaimResponse.from_domain(claim)
~~~

The exact current FastAPI idiom must follow the installed or current FastAPI documentation and official skill.

## 4. Service standard

A service or use case owns a meaningful business action.

Examples:

- claim_random_assignment
- approve_submission
- request_redemption
- expire_claim_if_due

A service should make its invariants visible near the transaction.

Avoid generic service classes such as CommonService, BaseCrudService, or a TaskManager with unrelated responsibilities.

Prefer explicit names.

## 5. Transaction boundaries

A transaction must cover the entire invariant.

Examples that MUST be atomic:

- quota check + Assignment lock + Claim insert + Assignment OCCUPIED;
- Submission approve + Claim COMPLETED + reward lock CONFIRMED + Ledger reward + Assignment COMPLETED;
- redemption eligibility + wallet reservation + stock reservation + Redemption creation.

Do not commit from repository helpers. The use-case or service boundary controls commit and rollback.

Repository methods may flush when they need generated IDs or constraint evaluation, but they should not independently decide transaction lifetime.

## 6. Database invariants

If an invariant can be encoded in PostgreSQL, encode it there in addition to service checks.

Examples:

- unique normalized phone;
- unique Task Assignment key;
- one active Claim per Assignment;
- one non-terminal user and Task Claim;
- one rating per user and Task;
- one vote per user and comment;
- one delivery per event, user, and channel;
- one original assignment reward per Claim.

Service checks produce friendly errors. Database constraints close races.

## 7. Concurrency

Do not solve database concurrency with in-process locks.

Use PostgreSQL:

- FOR UPDATE;
- FOR UPDATE SKIP LOCKED;
- partial unique indexes;
- advisory lock only when the plan explicitly chooses it;
- transaction isolation appropriate to the invariant.

Concurrency tests use independent sessions and connections.

When catching IntegrityError, distinguish expected constraint conflicts from unknown database failures. Do not convert every database error into conflict.

## 8. SQLAlchemy rules

Use SQLAlchemy 2.x style.

Guidelines:

- explicit typed models;
- explicit relationships only where they improve navigation;
- avoid hidden N+1 loading;
- use select and aggregate queries appropriate to read paths;
- do not serialize ORM objects directly as public API objects;
- centralize naming conventions for constraints and indexes;
- use database enums and check constraints deliberately rather than accidentally coupling Python enum internals to persisted values.

Avoid generic CRUD abstractions that make locking and query intent invisible.

## 9. Pydantic and DTO rules

Separate concerns:

- ORM model: persistence;
- request schema: untrusted transport input;
- response schema: public contract;
- internal command/result dataclass or Pydantic model where useful.

Never expose a persistence model containing private fields simply because FastAPI can serialize it.

Public DTOs should make privacy explicit.

## 10. Error handling

Expected business conflicts raise BusinessError with stable code.

Examples:

- NO_ASSIGNMENT_AVAILABLE
- ASSIGNMENT_LIMIT_REACHED
- SUBMISSION_WINDOW_CLOSED
- INSUFFICIENT_POINTS

Unexpected exceptions:

- preserve traceback in server observability;
- return safe 500 envelope with request ID;
- do not leak internal SQL, paths, secrets, or provider payloads.

Forbidden pattern:

~~~python
try:
    ...
except Exception:
    return None
~~~

If a broad exception is needed at a process boundary, log and re-raise or convert it deliberately with context.

## 11. Time

Business code receives a Clock.

Good:

~~~python
now = clock.now()
~~~

Bad:

~~~python
now = datetime.now()
~~~

Persist UTC-aware instants.

Convert to BUSINESS_TIMEZONE only for business-period semantics such as:

- daily abandon quota;
- daily and monthly rankings.

Never implement timezone logic with hardcoded hour offsets.

## 12. Worker rules

Celery jobs are orchestration shells.

Worker:

1. loads IDs and parameters;
2. constructs dependencies;
3. calls a service;
4. records or logs result;
5. retries according to policy.

Workers must not contain an alternate version of Claim expiry, reward logic, or notification eligibility.

Jobs must be idempotent or call idempotent service operations.

## 13. External systems

External side effects go through ports and adapters:

- SMS;
- email;
- object storage;
- ranking cache or projection;
- future campus systems.

Tests use fakes.

Adapter behavior should distinguish:

- temporary failure;
- permanent provider rejection;
- timeout or unknown outcome;
- already-exists or idempotent response where supported.

Never place provider SDK response objects into domain models.

## 14. File processing

Untrusted file parsing is high-risk code.

Requirements:

- bounded file size;
- bounded decompressed size;
- bounded rows, columns, and cell length;
- bounded parse time;
- streaming or read-only mode where possible;
- structured validation result;
- safe preview truncation;
- no formula execution;
- SQLite read-only, query-only, no extension loading, and no user SQL.

Parser exceptions should become validation outcomes where expected. Resource-limit or corrupted-file failures must not repeatedly kill the worker pool.

## 15. Logging and observability

Structured logs should include where available:

- request_id or correlation_id;
- actor or user id only when allowed;
- resource type and id;
- operation;
- stable error code;
- job id.

Do not log:

- passwords;
- OTP;
- refresh or access tokens;
- TOTP secrets;
- recovery codes;
- full phone or email when unnecessary;
- submitted dataset rows by default.

AuditLog is not a substitute for application logs, and application logs are not an audit trail.

## 16. Security boundary

Authorization is checked server-side at the use-case and resource level.

A Teacher role alone does not imply access to another Teacher's Task.

Do not trust client-supplied:

- role;
- owner_id;
- reward amount;
- ranking score;
- task status;
- anonymous identity;
- object key.

For signed object URLs, verify business authorization before generating the URL.

## 17. Configuration

Use typed settings.

Deployment secrets and environment:

- database URL;
- Redis URL;
- signing and encryption secrets;
- provider credentials;
- object-store credentials.

Audited runtime product settings:

- emoji whitelist;
- daily abandon limit;
- current academic term;
- optional management CIDR policy;
- notification templates.

Do not put secrets in the audited runtime settings table.

Fail fast on invalid required deployment settings.

## 18. Migrations

Every schema change requires Alembic.

Migration expectations:

- deterministic;
- explicit constraint and index names;
- tested from clean DB to head;
- complex data migration separated clearly;
- no network calls;
- no application service imports that change over time.

When a migration must backfill data, make it bounded and reviewable.

## 19. Code style and typing

Backend quality baseline:

- Ruff formatter;
- Ruff linter;
- static typing;
- mypy strict-first posture for project code, with narrow documented exceptions for third-party limitations;
- no wildcard imports;
- no unexplained type-ignore comments;
- no large commented-out code blocks.

Public and internal service functions should have useful parameter and return types.

Prefer domain-specific types and dataclasses over dict[str, Any] when shape matters.

## 20. Dependency rules

Before adding a Python dependency:

1. verify the standard library and project stack cannot solve it cleanly;
2. check maintenance, activity, and license;
3. keep provider-specific dependency at an adapter edge where possible;
4. document security-sensitive parser or crypto choices;
5. add test coverage for behavior delegated to the dependency.

Do not add a framework to avoid writing a small explicit function.

## 21. Testing shape

### Unit tests

Pure behavior:

- deadline tiers;
- validators;
- permission predicates;
- schema parsing;
- honor rules.

### Integration tests

Real PostgreSQL:

- constraints;
- transactions;
- locking;
- concurrency;
- service and repository behavior.

### Worker tests

- idempotency;
- retries;
- external fake failures;
- due scanning.

### E2E

Critical product flows.

Do not mock the database in a test intended to prove a PostgreSQL concurrency invariant.

## 22. Maintainability review questions

Before accepting backend code, ask:

- Is the business invariant visible where the transaction occurs?
- Could two concurrent requests violate it?
- Does PostgreSQL provide a final safety net?
- Does the router know too much?
- Does a worker duplicate service logic?
- Is private data leaking through a reused DTO?
- Can time be frozen in tests?
- Can external services be faked?
- Is a retry safe?
- Will a new maintainer understand why a lock or constraint exists?
- Is there a regression test for the non-obvious behavior?

If an answer is unclear, improve the code or add a short comment explaining WHY, not what the syntax does.
