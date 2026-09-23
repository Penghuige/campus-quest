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
- Domain-event publication follows the outbox direction. `LoggingEventPublisher` (the interim production adapter) may log, but must NOT be replaced with direct Celery/SMS/email side effects inside the DB transaction. When Plan 07/08 lands:
  - event/audit/notification intent persists in the SAME PostgreSQL transaction as the domain change;
  - dispatch to external channels happens asynchronously after commit;
  - delivery is idempotent (a redelivered or replayed intent produces one observable effect).
  This rules out "event sent but DB commit failed" dual-write bugs. The Plan 07/08 integration tests must include an intentional post-intent DB-transaction failure proving no externally visible side effect escapes.

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

### ReviewAction (spec §11.3, one row per SubmissionReview)

```python
class ReviewAction(StrEnum):
    APPROVE = "APPROVE"
    REQUIRE_REVISION = "REQUIRE_REVISION"
    INVALIDATE_LOCK = "INVALIDATE_LOCK"
```

`INVALIDATE_LOCK` is the database-stable spelling of the spec's INVALIDATE_REWARD_LOCK review outcome; `SubmissionReview.action` persists the exact string. (Frozen here per the Plan 04 T1 memo before the review service's first cross-module use.)

### RewardLockStatus (spec §11.2)

```python
class RewardLockStatus(StrEnum):
    NONE = "NONE"
    PROVISIONAL = "PROVISIONAL"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
```

Re-lock clamp (spec §11.3, controller ruling on the §11.3 x §11.4 collision): a revision-window submission whose `submitted_at` is at/after `grace_deadline_at` re-locks at the lowest defined tier, 20% — the §9.3 ladder's `>= grace` arm rejects only FIRST locks (NONE -> PROVISIONAL); the INVALIDATED -> PROVISIONAL re-lock path clamps instead of rejecting.

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

Open product decision (PR #2 follow-up ruling): the approved spec does NOT define auto-expiry for `REQUESTED`/`UNDER_REVIEW` redemptions. No agent may add an auto-cancel/timeout rule without a new owner ruling — implementing one requires first defining timeout duration, state transitions, points-reservation release, stock occupancy, and term-quota release semantics.

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

class ReportStatus(StrEnum):    # spec §23; CommentReport.status
    OPEN = "OPEN"
    HANDLED = "HANDLED"
    DISMISSED = "DISMISSED"
```

Report-closure ownership (PR #2 follow-up ruling, supersedes the initial deferral): the OPEN -> HANDLED/DISMISSED closure path lands IN PR #2's hardening (updated checklist step 10) as audited moderation endpoints over the queue (same standing as `list_task_reports`: task owner / MODERATE_COMMUNITY / Admin, reason-mandatory where the spec demands context). `report_comment` only files; closure is never automatic. Plan 08's AuditLog consumes the emitted audit events once durable audit lands.

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
| `CONFLICT` | §29 envelope（HTTP 409） |

Claim-failure codes return 4xx, never 500 (spec §8.4). New codes require updating this table first.

`CONFLICT` (registered Plan 08 T9; not a §29-listed name): the generic business
code for 409 state/conflict refusals — concurrent ownership (an unfinished
cleanup deletion claim), replay-integrity failures (whitelist confirm digest
mismatch, whitelist import collisions), and illegal state transitions (account
status §5.7). It never replaces a more specific code that exists above.

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

Plan-sanctioned refinements (keep the §36 name as the use-case verb; the service class may expose a due-guarded variant): Plan 07 exposes `ClaimService.expire_claim_if_due(claim_id, now)`; Plan 03 additionally exposes `resume_task` / `close_task` / `archive_task` (the CLOSED→ARCHIVED edge of the §6.2 table) / `update_task` (the published-task edit rule) alongside the §36 task verbs. Plan 06 exposes `set_vote` / `toggle_reaction` (the toggle semantics under the §22 `vote_comment` / `react_comment` verbs) and `delete_own_comment` + `moderate_delete_comment` (the §21.3 `delete_comment` verb split by authority: owner soft delete vs the reason-mandatory moderation path) alongside the §36 community verbs. Plan 07 T7 additionally exposes the §13/§27 retention-cleanup scan `cleanup_expired_files(now, repo, storage) -> CleanupSummary` (`app.modules.files.cleanup_service`), consumed through the `CleanupRepository` port over `FileRecord` snapshots.

Worker registry (PR #2 hardening step 4, post-carry): JOB_MODULES also registers `app.workers.jobs.cleanup_expired_files` (§13/§27 retention over submission keys) and `app.workers.jobs.requeue_stale_validating` (stale-VALIDATING sweep: requeues submissions stuck in VALIDATING past `stale_validating_requeue_seconds` — closes the retry-exhaustion stranding found in closure review sub-F2). Celery beat schedules: due-notification dispatch, claim expiry, retention cleanup, stale-VALIDATING requeue (intervals are the four scan-interval settings). NotificationPort producer composition points: the submissions validation and review flows, the points redemption flow, and the identity security (TOTP) flow. New migration 0013 adds the actionable-predicate expiry scan index (`ix_assignment_claims_expiry_due`). Known carry (Plan 08): retention over never-finalized upload intents requires an intents retention column + CleanupRepository contract extension.

Durable audit (PR #2 hardening step 7 + §30 completion pass, migrations 0014/0016): `audit_logs` is append-only (the sole insert path is `AuditLogWriter.append`, flush-only into the caller's transaction; no UPDATE/DELETE anywhere; actor has no FK — rows outlive user deletion) and carries the §30 field set: `before_snapshot`/`after_snapshot` (sanitized structured JSON — PII never enters snapshots, pinned by negative assertions), `ip_address`, `request_id` (both threaded from the transport via `AuditContext.from_request`: safe X-Request-ID + direct client IP, proxy trust boundary documented), alongside `details` as additional context. Audited actions: `COMMUNITY_IDENTITY_REVEAL`, `REDEMPTION_APPROVE`/`_REJECT`/`_FULFILL`, `SUBMISSION_APPROVED`, `SUBMISSION_REVISION_REQUIRED`, `REWARD_LOCK_INVALIDATED`, `COMMENT_MODERATE_DELETED`, `COMMENT_ADMIN_HARD_HIDDEN`, `REWARD_REVERSAL`, `REPORT_DISMISSED`/`_HANDLED`, `SYSTEM_SETTING_UPDATED`, `STAFF_INVITATION_CREATED`, `STAFF_INVITATION_ACCEPTED` (Plan 08: staff roles are assigned exactly once at invitation acceptance — there is no separate promotion write point), `WHITELIST_IMPORT_CONFIRMED`, `WHITELIST_ENTRY_TOGGLED`, `USER_SUSPENDED`, `USER_BANNED`, `USER_REACTIVATED` — actual state transitions only; idempotent replays write nothing; DomainEvent streams are unaffected. Every snapshot/details payload passes the recursive `redact()` layer on append (`SENSITIVE_KEYS`: password/OTP/refresh-token/TOTP-secret/recovery-code/access-token spellings at any dict/list depth → `"[REDACTED]"`) — caller-side sanitization is defense-in-depth, never the trusted mechanism.

Whitelist administration (Plan 08 T3): `preview_whitelist_import` is a pure per-row classification (no writes); `confirm_whitelist_import` replays the preview digest against current state and resolves concurrent overlapping imports through the unique constraint as an all-or-nothing conflict with a per-row list (the assignment-importer precedent — never a silent partial import). Account governance (T8): `suspend_user`/`ban_user`/`reactivate_user` follow the spec §5.7 transition table with mandatory reasons; `require_management_network` is the optional CIDR allowlist dependency over `request.client.host` (X-Forwarded-For never trusted without an explicit trusted-proxy deployment; env placeholders `management_network_enabled`/`management_network_cidrs` migrate into the system store at T5 — the loader is the single seam). `reward_redemptions.rejection_reason` persists the reject reason (closure review pts-F1) and surfaces on the Admin review DTO only; the student catalogue DTO no longer carries the dormant `requires_manual_review` flag (V1 reviews every redemption manually — G13 option 1; auto-timeout remains the recorded open product decision).

Cleanup deletion claims (PR #2 hardening passes 4b/5a + final-pass fencing + post-merge P0, migrations 0017-0019): retention deletion is a declarative claim — one conditional UPDATE re-evaluates every retain guard against CURRENT PostgreSQL state (not the scan snapshot) and RETURNs the winner under the same lock order the validation path takes (submissions → assignment_claims), committing the claim BEFORE any external S3 I/O. Claims carry a lease and an ownership token (`cleanup_claim_token`, rewritten on every takeover); `release_cleanup_claim` and `mark_deleted` CAS on the token, so a stale worker resuming after lease expiry can never clear or complete another worker's claim. **Claim-ownership semantics (owner rulings, final + post-merge reviews — correctness never depends on a wall-clock provider bound): an UNFINISHED deletion claim blocks protection transitions even after its lease expires, and a submission-file provider failure KEEPS the claim (no release-to-unclaimed). Lease expiry authorizes cleanup TAKEOVER only — the takeover rewrites the token and resolves the deletion; protection re-opens only after the cleanup state truly settles (mark_deleted or the §27 missing-object reconcile). Socket-wait timeouts (`s3_connect/read_timeout_seconds`, `s3_delete_total_attempts`) are availability controls, not a safety proof: connect+read caps are not a request lifetime deadline, so no takeover-plus-release window may lean on them.** Orphan UploadIntent objects join the same cleanup job with the same fencing discipline (intent-side provider failures may release: no protection surface exists there and the delete converges idempotently), deleted only after `intent.expires_at` (URL TTL < intent TTL, enforced by an UploadService constructor invariant); finalized rows are untouched and missing objects are idempotent success. The claim conflict answers the dedicated `CONFLICT` code (registered Plan 08 T9; previously `VALIDATION_ERROR`).

Report closure (PR #2 hardening step 10): `POST /api/v1/tasks/{task_id}/reports/{report_id}/dismiss` (reason mandatory) and `/handle` (optional note) move a report OPEN -> DISMISSED/HANDLED exactly once under the moderation standing of `list_task_reports`; same-terminal replays are idempotent and write no second audit row. Audit actions `REPORT_DISMISSED` (reason) / `REPORT_HANDLED` (note in details), target_type `comment_report`. Typed errors on frozen codes: 403 standing, 400 blank dismiss reason, 404 unknown/cross-task report, 409 other-terminal.

System settings (PR #2 hardening step 8, migration 0015): `system_settings` is a current-value store (key pk, value, updated_by without FK — UPDATE is legal here, unlike append-only `audit_logs`); `SystemSettingService.set` writes a `SYSTEM_SETTING_UPDATED` audit row (target_type `system_setting`, target_id the key, details carry value and old_value) in the same transaction. V1 key: CURRENT_ACADEMIC_TERM via `GET/PUT /api/v1/admin/settings/current-academic-term` (Admin-only, typed validation). `SystemAcademicTermProvider` gives the settings row priority over the `Settings.current_academic_term` env seed (G7: the row is the fact, the env only bootstraps) and fails loud on a corrupt row; redemption keeps its creation-time term snapshot.

Lock order contract: users row -> tasks row -> assignments/claims rows; all new transactions must preserve it.

## Adapter Ports

External systems are consumed only through these Protocols; domain modules never contain provider-specific logic. Each has a deterministic in-memory fake used by tests (Plan 01, `backend/app/integrations/`, `backend/tests/fakes/`).

### Object storage (S3/MinIO)

```python
class ObjectStorage(Protocol):
    def create_upload_url(...) -> ...: ...   # short-lived presigned PUT/POST
    def head_object(...) -> ...: ...         # existence + size/content metadata
    def create_download_url(...) -> ...: ... # short-lived signed download URL
    def delete_object(...) -> None: ...      # retention cleanup (§13/§27); FileNotFoundError when absent
```

Object keys are server-generated (`submissions/{claim_id}/{uuid}`); original filenames are display metadata only.

Upload-PUT client contract (PR #2 hardening, adapter-owned since pass 4c): every presigned upload URL signs `If-None-Match: *` (write-once: exactly one successful PUT per key; replay and post-finalize overwrites answer 412 — proven against the pinned MinIO), plus the declared `Content-Length` and `Content-Type` (mismatches answer 403). `UploadUrl` carries the signing contract — `client_headers` (the signed headers the client MUST echo verbatim: If-None-Match, Content-Type) and `pinned_content_length` (the exact byte count the body must carry). Content-Length is deliberately NOT a client header: browsers cannot set it (forbidden request header) — a browser satisfies the pin with a Blob of exactly that size. The adapter owns this contract end to end; the application passes it through and never reconstructs it. MinIO CORS is configured through `MINIO_API_CORS_ALLOW_ORIGIN` (the pinned MinIO has no bucket-CORS API — verified).

### SMS

```python
class SmsSender(Protocol):
    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> str | None: ...
```

`idempotency_key` (optional) lets a provider collapse repeated sends onto one message: notification delivery always passes `"{event_key}:{channel}:{user_id}"` (spec §25.3) so an `UnknownOutcomeError` retry — whose first attempt may have succeeded — cannot double-send; single-shot callers (identity OTP) omit it.

The optional `str` return (PR #2 hardening step 5) is the provider receipt: adapters that surface one land it on the delivery row's `provider_message_id`; the development-only logging adapters return a `"logging:"`-prefixed id so a recorded SENT from a simulated send is distinguishable at a glance. Provider selection is typed settings (`sms_provider`/`email_provider`, `Literal["logging"]` in V1); the production sentinel validator refuses a logging provider under `environment="production"` — unconfigured real providers fail startup, never fake success (G4/G5).

Fake: `FakeSmsSender` records `SentSms(to, template, variables, idempotency_key=None)` for exact-delivery assertions.

### Email

```python
class EmailSender(Protocol):
    def send(
        self,
        *,
        to: str,
        template: str,
        variables: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> str | None: ...
```

Same receipt contract as SMS above (PR #2 hardening step 5). Fake: `FakeEmailSender`, same recording contract as SMS (including `idempotency_key`).

### Ranking projection

Rankings are a derived, rebuildable projection (spec §17.3; Plan 05). PostgreSQL ledger aggregation is authoritative; Redis sorted sets are the fast path:

- Keys: `ranking:daily:<YYYY-MM-DD>`, `ranking:monthly:<YYYY-MM>`, `ranking:all` (business timezone periods).
- Contract: on a changed ranking-affecting ledger entry, enqueue the affected user/period and recompute that user's authoritative period score from PostgreSQL, then `ZADD` the absolute score. Never `ZINCRBY` from a retryable event; repeated projection jobs must converge, and a full Redis loss is recoverable via `rebuild_all_rankings()`.
- Read interface: `RankingService.top(period, limit)`, `RankingService.around_me(user_id, period, radius)`.
- Public entry shape (spec §17/§40 privacy pin): a ranking entry exposes EXACTLY `nickname`, `display_honor`, `score`, `rank`. `user_id` is internal machinery — the ZSET member and `around_me`'s lookup key — and never appears in a public DTO (no student number, phone, or email can leak through a field the shape does not have). Nickname/honor enrichment goes through `UserDirectory.get_display_profile`, never identity ORM models.
- Period semantics (backend-engineering §11): a "day"/"month" is a BUSINESS_TIMEZONE natural day/month. Period boundaries are computed Python-side with `zoneinfo` from `settings.business_timezone` (no hardcoded hour offsets, no SQL-side tz math — the aggregation only filters UTC instant ranges), so DST and any IANA zone stay correct.
- Trigger surface (outbox direction, Core Primitives): the ledger-writing service calls `RankingUpdateDispatcher.enqueue_ranking_update(user_id, ranking_effective_at)` AFTER its transaction commits; the projection itself is a Celery job over that port. A failed or missed enqueue is healed by `rebuild_all_rankings()`, never by compensating business writes.
- Rebuild semantics: `rebuild_all_rankings()` recomputes every business day/month present in the ledger plus all-time from PostgreSQL and rewrites each key wholesale (`DELETE` + batched `ZADD` in one pipeline), which also evicts stale members a converged incremental update could not remove.

### Cross-module ports (module boundaries as interfaces)

Later modules are reached through ports so earlier plans ship with fakes:

- `NotificationPort.record_event(session, event_key, event_type, user_id, payload, task_policy=None) -> None` — persists notification intent inside the domain transaction (Plan 07).
- `PointsRewardPort.grant_assignment_reward(*, user_id: UUID, claim_id: UUID, base_points: int, locked_points: int, idempotency_key: str) -> GrantResult` — frozen signature (Plan 04 review-approve is the first caller; Plan 05 provides the concrete points implementation). `GrantResult` is the frozen dataclass `(user_id, claim_id, points_granted)`; `locked_points` is the reward lock's `locked_reward_points` (the fraction-adjusted amount — the "fraction or locked points" grant basis, already floored per §31.1); `idempotency_key` is stable per claim (`assignment_reward:<claim_id>`; UNIQUE(claim) ledger semantics are enforced by Plan 05's concrete adapter).
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

@dataclass(frozen=True)
class DisplayProfile:  # ranking-safe display facts (Plan 05 Tasks 6-7)
    nickname: str
    display_honor_title: str | None   # the chosen display honor's name; None while unset

class UserDirectory(Protocol):
    async def find_by_username(self, session, username: str) -> UserSummary | None: ...
    async def find_by_email(self, session, email: str) -> UserSummary | None: ...
    async def get_role(self, session, user_id: UUID) -> Role | None: ...
    async def get_display_profile(self, session, user_id: UUID) -> DisplayProfile | None: ...
```

`find_by_email` normalizes its argument (strip + lowercase) exactly once;
lookups join the caller's transaction (`session` in, answer out, no inner
commit), same shape as the other cross-module ports.

`get_display_profile` is the ranking module's enrichment read (spec §17:
leaderboards show nickname + display honor only). It deliberately reuses
the frozen directory rather than letting rankings import identity models.
Nickname and user existence delegate to the repository (`find_by_id`);
`display_honor_title` is the name of the ONE honor the user chose to
display (`users.display_honor_id`, Plan 05 Task 7), resolved through a
constructor-injectable honor-title read whose production default is one
fresh Core SELECT over the rankings-owned `honors` table (never a
rankings ORM import). The read is fresh on purpose: the pointer is
written by rankings' cross-module Core UPDATE
(`honor_service.set_display_honor`), which an identity-mapped ORM read
would mask until expiry. `None` while the choice is unset.

Locking-read seam: `UserDirectory` will gain a documented locking read
(e.g. `lock_user_status(session, user_id) FOR UPDATE`) when a new module
needs one; until then, the lightweight typed Core reads over the `users`
table — the three `_USERS_LOCK` twins in the claim, abandon, and upload
(`id` + `status` + `role`, through the frozen `UserStatus`/`Role`
vocabularies) — are the sanctioned interim, and all three queries move
behind the port unchanged when it registers one.
