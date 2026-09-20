# CampusQuest Cross-Module Interface Contracts

Frozen contract for all CampusQuest V1 implementation plans (docs/superpowers/plans/2026-09-19-campusquest-01 through 10).

Authority and precedence:

1. `docs/superpowers/specs/2026-09-19-campusquest-design.md` is authoritative for semantics.
2. This document is canonical for shared names: identifiers, enum members, event names, error codes, service names, and adapter port signatures. Child plans code against the names below.
3. If a child plan diverges from a name here, either fix the plan or raise the conflict to the controller before implementing. Do not silently rename in code.

All enums are string-valued with `value == member name`; database columns and API payloads persist the exact string.

## Core Primitives

Canonical Python signatures (verbatim from the implementation plan):

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

Rules:

- `Clock` is the only source of business time. Production uses `SystemClock` (UTC-aware); tests use `FrozenClock(current: datetime)`, which rejects naive datetimes (Plan 01, `backend/app/core/clock.py`).
- `Actor` is the authenticated context passed to service calls. Other modules consume `Actor` and RBAC helpers (`require_role(*roles)`, `require_active_actor()`, `get_actor()`); they never inspect tokens or passwords directly (Plan 02).
- `DomainEvent.occurred_at` is UTC-aware; `payload` must be JSON-serializable.

## Canonical Enums

Every member value below is taken verbatim from the spec section cited.

### Role (spec §4)

```python
class Role(StrEnum):
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    ADMIN = "ADMIN"
```

### UserStatus (spec §5.7)

```python
class UserStatus(StrEnum):
    PENDING_PHONE = "PENDING_PHONE"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    BANNED = "BANNED"
```

Transitions: `PENDING_PHONE -> ACTIVE`, `ACTIVE -> SUSPENDED`, `ACTIVE -> BANNED`, `SUSPENDED -> ACTIVE`, `BANNED -> ACTIVE` (Admin unban only).

### TaskStatus (spec §6.2)

```python
class TaskStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"
```

Transitions: `DRAFT -> PUBLISHED`, `PUBLISHED <-> PAUSED`, `PUBLISHED/PAUSED -> CLOSED`, `CLOSED -> ARCHIVED`.

### DeadlineMode (spec §6, §9)

```python
class DeadlineMode(StrEnum):
    FIXED = "FIXED"
    RELATIVE = "RELATIVE"
```

### AssignmentAvailability (spec §7)

```python
class AssignmentAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    OCCUPIED = "OCCUPIED"
    COMPLETED = "COMPLETED"
    RETIRED = "RETIRED"
```

`ABANDONED` / `EXPIRED` are Claim terminal states, not Assignment states; after them the Assignment returns to `AVAILABLE`.

### ClaimStatus (spec §8.1)

```python
class ClaimStatus(StrEnum):
    CLAIMED = "CLAIMED"
    VALIDATING = "VALIDATING"
    UNDER_REVIEW = "UNDER_REVIEW"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    EXPIRED = "EXPIRED"
```

Terminal states: `COMPLETED`, `ABANDONED`, `EXPIRED`. `CLAIMED` and `REVISION_REQUIRED` count toward the per-student limit of 3 active-actionable Claims; `VALIDATING` and `UNDER_REVIEW` do not (spec §8.2).

### ValidationStatus (spec §11.1, machine stage, on Submission)

```python
class ValidationStatus(StrEnum):
    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
```

### ReviewStatus (spec §11.1, human stage, on Submission)

```python
class ReviewStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
```

`PENDING_REVIEW` is the initial value before human review starts. The spec names the human-stage transitions (`VALIDATED -> UNDER_REVIEW -> APPROVED | REVISION_REQUIRED`) but not the pre-review initial value; this document fixes `PENDING_REVIEW` as the canonical initial member. `APPROVED` implies Claim `COMPLETED` (spec §11.1, §14).

### RewardLockStatus (spec §11.2)

```python
class RewardLockStatus(StrEnum):
    NONE = "NONE"
    PROVISIONAL = "PROVISIONAL"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
```

### RedemptionStatus (spec §16.1)

```python
class RedemptionStatus(StrEnum):
    REQUESTED = "REQUESTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    FULFILLED = "FULFILLED"
    REJECTED = "REJECTED"
```

`REQUESTED`, `UNDER_REVIEW`, `APPROVED`, and `FULFILLED` occupy reward stock and per-term quota; `REJECTED` releases them (spec §16.1).

### NotificationChannel (spec §25)

```python
class NotificationChannel(StrEnum):
    SMS = "SMS"
    EMAIL = "EMAIL"
    IN_APP = "IN_APP"
```

### Supplementary shared enums

Referenced by child plans; values verbatim from the cited spec sections.

```python
class TaskType(StrEnum):        # spec §6: V1 at least DATA_CRAWL
    DATA_CRAWL = "DATA_CRAWL"

class TaskRarity(StrEnum):      # spec §6.1: visual label only, no business effect
    NORMAL = "NORMAL"
    RARE = "RARE"
    EPIC = "EPIC"
    LEGENDARY = "LEGENDARY"

class LedgerType(StrEnum):      # spec §15 typical types
    ASSIGNMENT_REWARD = "ASSIGNMENT_REWARD"
    ASSIGNMENT_REWARD_REVERSAL = "ASSIGNMENT_REWARD_REVERSAL"
    REWARD_REDEMPTION = "REWARD_REDEMPTION"
    REWARD_REDEMPTION_REFUND = "REWARD_REDEMPTION_REFUND"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"

class DeliveryStatus(StrEnum):  # spec §25.3/§25.4 via Plan 07; NotificationDelivery.status
    PENDING = "PENDING"
    RETRYABLE = "RETRYABLE"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
```

## Domain Events

Business logic emits events; the Notification module owns delivery (spec §25). Canonical event names:

- `ASSIGNMENT_DEADLINE_24H`
- `ASSIGNMENT_DEADLINE_4H`
- `REVISION_REQUIRED`
- `SUBMISSION_APPROVED`
- `SUBMISSION_VALIDATION_FAILED`
- `REWARD_REDEMPTION_APPROVED`
- `REWARD_REDEMPTION_REJECTED`
- `ACCOUNT_SECURITY`

The spec §25 list is "at least"; `SUBMISSION_VALIDATION_FAILED` is the Plan 07 addition and is part of the frozen set. New events require updating this document first.

Delivery de-duplication key: `UNIQUE(event_key, user_id, channel)` on `NotificationDelivery` (spec §25.3). Event-key convention: `<aggregate>:<id>:<event_suffix>`, e.g. `claim:123:deadline_4h`. Celery retries must never cause double delivery. Critical events (`REVISION_REQUIRED`, `ACCOUNT_SECURITY`, redemption results) always create an `IN_APP` delivery when the account can receive notifications.

## Error-Code Registry

All business errors use the stable envelope (spec §29):

```json
{
  "error": {
    "code": "ASSIGNMENT_LIMIT_REACHED",
    "message": "当前进行中的任务已达到上限",
    "details": { "limit": 3 },
    "request_id": "..."
  }
}
```

Canonical codes (exact strings; the frontend must branch on `code`, never parse `message`):

| Code | Spec source |
| --- | --- |
| `VALIDATION_ERROR` | §29 |
| `AUTHENTICATION_REQUIRED` | §29 |
| `PERMISSION_DENIED` | §29 |
| `ACCOUNT_NOT_ACTIVE` | §29, §8.4 |
| `STUDENT_NOT_WHITELISTED` | §29 |
| `PHONE_ALREADY_BOUND` | §29 |
| `USERNAME_ALREADY_EXISTS` | §5.2, §31.1 |
| `TASK_NOT_CLAIMABLE` | §29, §8.4 |
| `NO_ASSIGNMENT_AVAILABLE` | §29, §8.4 |
| `ASSIGNMENT_LIMIT_REACHED` | §29, §8.4 |
| `TASK_ACTIVE_CLAIM_EXISTS` | §29, §8.4 |
| `CLAIM_CUTOFF_REACHED` | §29, §8.4 |
| `ABANDON_LIMIT_REACHED` | §8.5 |
| `CLAIM_NOT_ABANDONABLE` | §8.5 |
| `CLAIM_NOT_SUBMITTABLE` | §29 |
| `SUBMISSION_WINDOW_CLOSED` | §29 |
| `FILE_TOO_LARGE` | §29 |
| `FILE_TYPE_NOT_ALLOWED` | §29 |
| `SUBMISSION_VALIDATION_FAILED` | §29 |
| `ALREADY_REVIEWED` | §29, §14 |
| `INSUFFICIENT_POINTS` | §29 |
| `REWARD_OUT_OF_STOCK` | §29 |
| `REDEMPTION_LIMIT_REACHED` | §29 |
| `RATING_NOT_ELIGIBLE` | §29 |
| `TOTP_SETUP_REQUIRED` | §5.8, §33.4 |
| `EMAIL_ALREADY_BOUND` | §5.5 |
| `INVALID_EMAIL_TOKEN` | §5.5 |
| `OTP_CODE_INVALID` | §33.2 |
| `OTP_TOO_MANY_ATTEMPTS` | §33.2 |
| `OTP_CHALLENGE_EXPIRED` | §33.2 |
| `OTP_CHALLENGE_CONSUMED` | §33.2 |
| `OTP_CHALLENGE_INVALID` | §33.2 |
| `OTP_TOKEN_INVALID` | §33.2 |
| `OTP_RESEND_COOLDOWN` | §33.2 |
| `RATE_LIMITED` | §33.1 |

Claim-failure codes return 4xx, never 500 (spec §8.4). New codes require updating this table first.

### Identity typed-exception mapping (Plan 02)

The identity domain modules raise typed module exceptions where the frozen
registry had no code at the time the service landed; the identity API routes
map each to exactly one envelope code above (one exception type, one code —
never shared, never reinterpreted):

| Typed exception (module) | Envelope code | HTTP |
| --- | --- | --- |
| `staff_service.TotpSetupRequiredError` | `TOTP_SETUP_REQUIRED` | 403 |
| `email_verification.EmailAlreadyBoundError` | `EMAIL_ALREADY_BOUND` | 409 |
| `email_verification.InvalidEmailTokenError` | `INVALID_EMAIL_TOKEN` | 400 |
| `otp.InvalidPhoneError` | `VALIDATION_ERROR` | 400 |
| `otp.WrongCodeError` | `OTP_CODE_INVALID` | 400 |
| `otp.TooManyAttemptsError` | `OTP_TOO_MANY_ATTEMPTS` | 429 |
| `otp.ChallengeExpiredError` | `OTP_CHALLENGE_EXPIRED` | 400 |
| `otp.ChallengeAlreadyConsumedError` | `OTP_CHALLENGE_CONSUMED` | 400 |
| `otp.UnknownChallengeError` | `OTP_CHALLENGE_INVALID` | 400 |
| `otp.InvalidTokenError` | `OTP_TOKEN_INVALID` | 400 |
| `otp.ResendCooldownError` | `OTP_RESEND_COOLDOWN` | 429 |
| `otp.OtpRateLimitError` | `RATE_LIMITED` | 429 |
| `rate_limit.RateLimitExceededError` | `RATE_LIMITED` | 429 |

### System / framework codes

Framework-level failures reuse the same §29 envelope (same shape, same `request_id` threading) with these codes. They are SYSTEM codes, not business codes: business logic never raises them, and the frontend treats them as transport/framework errors, never as domain branches.

| Code | HTTP | Trigger |
| --- | --- | --- |
| `INTERNAL_ERROR` | 500 | unhandled exception; safe generic message, no internals leaked |
| `NOT_FOUND` | 404 | unknown route (framework 404) |
| `METHOD_NOT_ALLOWED` | 405 | route exists, method does not |
| `HTTP_ERROR` | other | any other framework `HTTPException` status |

`VALIDATION_ERROR` is the one code in both worlds: it stays a business code above, and the framework's 422 request-schema handler reuses it. The importable registry (`backend/app/core/error_codes.py`) mirrors both tables; membership is frozen by `backend/tests/unit/core/test_error_codes.py`. New codes require updating this document first (same rule as above).

## Service Boundaries

Canonical service/use-case names (spec §36). Business rules live in these services; API handlers and Celery jobs must not duplicate them.

- Identity: `register_student`, `verify_phone`, `authenticate`, `rotate_refresh_session`
- Task: `create_task`, `publish_task`, `pause_task`, `import_assignments`
- Assignment: `claim_random_assignment`, `abandon_claim`, `expire_claim`
- Submission: `create_upload_intent`, `finalize_upload`, `validate_submission`, `require_revision`, `invalidate_reward_lock`, `approve_submission`
- Points / Reward: `grant_assignment_reward`, `reverse_assignment_reward`, `request_redemption`, `approve_redemption`, `reject_redemption`, `fulfill_redemption`
- Community: `create_comment`, `edit_comment`, `delete_comment`, `vote_comment`, `react_comment`, `report_comment`, `rate_task`
- Notification: `schedule_due_notifications`, `dispatch_notification`

Plan-sanctioned refinements (keep the §36 name as the use-case verb; the service class may expose a due-guarded variant): Plan 07 exposes `ClaimService.expire_claim_if_due(claim_id, now)`; Plan 03 additionally exposes `resume_task` / `close_task` / `archive_task` (the CLOSED→ARCHIVED edge of the §6.2 table) / `update_task` (the published-task edit rule) alongside the §36 task verbs.

Lock order contract: users row -> tasks row -> assignments/claims rows; all new transactions must preserve it.

## Adapter Ports

External systems are consumed only through these Protocols; domain modules never contain provider-specific logic. Each has a deterministic in-memory fake used by tests (Plan 01, `backend/app/integrations/`, `backend/tests/fakes/`).

### Object storage (S3/MinIO)

```python
class ObjectStorage(Protocol):
    def create_upload_url(...) -> ...: ...   # short-lived presigned PUT/POST
    def head_object(...) -> ...: ...         # existence + size/content metadata
    def create_download_url(...) -> ...: ... # short-lived signed download URL
```

Object keys are server-generated (`submissions/{claim_id}/{uuid}`); original filenames are display metadata only.

### SMS

```python
class SmsSender(Protocol):
    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None: ...
```

Fake: `FakeSmsSender` records `SentSms(to, template, variables)` for exact-delivery assertions.

### Email

```python
class EmailSender(Protocol):
    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None: ...
```

Fake: `FakeEmailSender`, same recording contract as SMS.

### Ranking projection

Rankings are a derived, rebuildable projection (spec §17.3; Plan 05). PostgreSQL ledger aggregation is authoritative; Redis sorted sets are the fast path:

- Keys: `ranking:daily:<YYYY-MM-DD>`, `ranking:monthly:<YYYY-MM>`, `ranking:all` (business timezone periods).
- Contract: on a changed ranking-affecting ledger entry, enqueue the affected user/period and recompute that user's authoritative period score from PostgreSQL, then `ZADD` the absolute score. Never `ZINCRBY` from a retryable event; repeated projection jobs must converge, and a full Redis loss is recoverable via `rebuild_all_rankings()`.
- Read interface: `RankingService.top(period, limit)`, `RankingService.around_me(user_id, period, radius)`.

### Cross-module ports (module boundaries as interfaces)

Later modules are reached through ports so earlier plans ship with fakes:

- `NotificationPort.record_event(session, event_key, event_type, user_id, payload, task_policy=None) -> None` — persists notification intent inside the domain transaction (Plan 07).
- `PointsRewardPort.grant_assignment_reward(...)` — Plan 04 calls it with a fake; Plan 05 provides the concrete points implementation.
- `RatingSummaryPort.summary(task_id) -> RatingSummary | None` — Plan 03 ships a null/fake; Plan 06 supplies the concrete adapter backed by `TaskRating`.
- Audit writes go through an audit port until Plan 08 replaces it with `AuditService`.

### Identity directory (cross-module reads)

Modules outside identity read account facts ONLY through this port, never by
importing identity ORM models (Plan 02 ships the concrete adapter
`backend/app/modules/identity/directory.py` over the existing repository):

```python
@dataclass(frozen=True)
class UserSummary:      # NO contact fields: phone/email stay inside identity
    user_id: UUID
    username: str
    role: Role
    status: UserStatus

class UserDirectory(Protocol):
    async def find_by_username(self, session, username: str) -> UserSummary | None: ...
    async def find_by_email(self, session, email: str) -> UserSummary | None: ...
    async def get_role(self, session, user_id: UUID) -> Role | None: ...
```

`find_by_email` normalizes its argument (strip + lowercase) exactly once;
lookups join the caller's transaction (`session` in, answer out, no inner
commit), same shape as the other cross-module ports.

Locking-read seam: `UserDirectory` will gain a documented locking read
(e.g. `lock_user_status(session, user_id) FOR UPDATE`) when a second
module needs one; until then, the lightweight typed Core reads over the
`users` table in the claim/abandon services (`_USERS_LOCK` — `id` +
`status` only, through the frozen `UserStatus` vocabulary) are the
sanctioned interim, and both queries move behind the port unchanged when
it registers one.
