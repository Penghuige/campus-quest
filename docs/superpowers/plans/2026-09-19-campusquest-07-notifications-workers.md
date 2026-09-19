# CampusQuest 07 Notifications & Scheduled Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement event-driven in-app/SMS/email notifications, exact DDL scheduling, idempotent delivery/retries, Claim expiry, and file-retention cleanup workers without duplicating domain logic.

**Architecture:** Domain services emit durable notification/domain events. Notification services create one logical Notification and per-channel NotificationDelivery records; Celery workers dispatch due deliveries. Scheduled workers discover candidates, then call Task/Submission services that re-check state transactionally.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Redis/Celery, SMS/Email adapters, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

**Required quality references:** `AGENTS.md`, `docs/quality/backend-engineering.md`, `docs/quality/quality-gates.md`, `docs/quality/agent-tooling.md`.

## Global Constraints

- Channels: SMS, verified EMAIL, IN_APP.
- Task config controls 24h/4h reminders and channels.
- Do not backfill reminders whose scheduled instant already passed at claim time.
- Valid submission/validation/review state suppresses ordinary remaining DDL reminders.
- `UNIQUE(event_key, user_id, channel)` prevents duplicate deliveries.
- External delivery failure never rolls back a valid business state.
- Retry policy is bounded; permanent failure is observable.
- Expiry worker must not release a Claim with a valid in-window Submission under review.
- File cleanup is idempotent and respects retention/legal hold.

## Review Focus

1. Same Celery delivery job running twice must send only once per event/user/channel.
2. A student finalizing a valid submission while expiry scans the Claim must not lose the Assignment.
3. Missing/unverified email must be SKIPPED, not FAILED.
4. Provider timeout after uncertain send must not blindly duplicate without idempotency/provider-key handling.
5. File cleanup encountering an already-missing object must reconcile safely without deleting business metadata.

---

### Task 1: Add Notification Models

**Files:**
- Create: `backend/app/modules/notifications/enums.py`
- Create: `backend/app/modules/notifications/models.py`
- Create: `backend/alembic/versions/0008_notifications.py`
- Create: `backend/tests/integration/notifications/test_notification_constraints.py`

**Interfaces:**
- Produces `Notification`, `NotificationDelivery`, `NotificationTemplate`; enums `NotificationChannel`, `DeliveryStatus`, `NotificationEventType`.

- [ ] **Step 1: Write unique-delivery test**

Insert two deliveries with identical `event_key,user_id,channel`; second must fail.

- [ ] **Step 2: Implement models/migration**

Delivery fields include scheduled_at, attempts, sent_at, provider_message_id, last_error, status.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules/notifications backend/alembic/versions/0008_notifications.py backend/tests/integration/notifications
git commit -m "feat: add notification persistence"
```

### Task 2: Implement Template Rendering and Channel Eligibility

**Files:**
- Create: `backend/app/modules/notifications/templates.py`
- Create: `backend/app/modules/notifications/service.py`
- Create: `backend/tests/unit/notifications/test_templates.py`
- Create: `backend/tests/unit/notifications/test_channel_eligibility.py`

**Interfaces:**
- Produces `render_template(event_type, channel, variables) -> RenderedMessage` and `eligible_channels(user, task_policy, event_type)`.

- [ ] **Step 1: Write safe-template tests**

Unknown variables fail; template text cannot execute Python/Jinja arbitrary expressions. Use a constrained placeholder formatter.

- [ ] **Step 2: Write email eligibility tests**

Verified email -> eligible if Task email on. Missing or unverified email -> channel result SKIPPED with reason, not failure.

- [ ] **Step 3: Implement defaults**

Critical events `REVISION_REQUIRED`, account security, redemption result always create IN_APP when the account can receive notifications; SMS/email follow configured policy.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/notifications backend/tests/unit/notifications
git commit -m "feat: render and route notification events"
```

### Task 3: Implement DDL Reminder Scheduling

**Files:**
- Create: `backend/app/modules/notifications/deadline_scheduler.py`
- Create: `backend/tests/unit/notifications/test_deadline_scheduler.py`

**Interfaces:**
- Produces `schedule_claim_deadline_notifications(claim_id) -> list[NotificationDelivery]`.

- [ ] **Step 1: Write FrozenClock cases**

- 30h left -> schedule 24h and 4h.
- 10h left -> schedule only 4h.
- 3h left -> schedule neither past reminder.
- exactly 24h left -> 24h reminder may schedule for now once.
- exactly 4h left -> 4h reminder may schedule for now once.

- [ ] **Step 2: Implement deterministic event keys**

Use `claim:{claim_id}:deadline_24h` and `claim:{claim_id}:deadline_4h`.

- [ ] **Step 3: Add cancellation/suppression predicate**

At dispatch time re-check Claim status. CLAIMED/REVISION_REQUIRED may receive applicable reminder; VALIDATING/UNDER_REVIEW/COMPLETED/ABANDONED/EXPIRED suppress ordinary deadline reminder.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/notifications/deadline_scheduler.py backend/tests/unit/notifications/test_deadline_scheduler.py
git commit -m "feat: schedule deadline reminders exactly once"
```

### Task 4: Implement Idempotent Delivery Worker

**Files:**
- Create: `backend/app/workers/jobs/send_notification.py`
- Create: `backend/tests/workers/test_notification_delivery.py`

**Interfaces:**
- Produces Celery job `send_notification_delivery(delivery_id)`.

- [ ] **Step 1: Write duplicate-job test**

Queue/execute same delivery twice with FakeSmsSender. Assert fake recorded exactly one message and delivery status SENT.

- [ ] **Step 2: Write bounded retry test**

Fake sender fails with timeout three times. Assert attempts=3 and final FAILED. Verify retry delay configuration corresponds to ~1m, ~5m, ~20m sequence.

- [ ] **Step 3: Implement claim-before-send state transition**

Lock delivery. Move due PENDING/RETRYABLE into SENDING with attempt increment. Use provider idempotency key equal to event/channel identity when provider supports it.

- [ ] **Step 4: Handle uncertain timeout explicitly**

Record provider result/timeout; retry using same provider idempotency key. Never create a new Delivery row for retry.

- [ ] **Step 5: Run and commit**

```bash
git add backend/app/workers/jobs/send_notification.py backend/tests/workers/test_notification_delivery.py
git commit -m "feat: deliver notifications idempotently"
```

### Task 5: Wire Durable Business Events to Notifications

**Files:**
- Create: `backend/app/modules/notifications/event_handlers.py`
- Create: `backend/app/modules/notifications/port.py`
- Create: `backend/tests/integration/notifications/test_event_notifications.py`

**Interfaces:**
- Provides `NotificationPort.record_event(session, event_key, event_type, user_id, payload, task_policy=None) -> None` for domain services.
- Consumes events from Task/Submission/Points/Identity.
- Produces Notification/Delivery rows for Claim-created deadline scheduling, `REVISION_REQUIRED`, `SUBMISSION_VALIDATION_FAILED`, `SUBMISSION_APPROVED`, redemption approved/rejected, and account security.

- [ ] **Step 1: Write transaction-durability test**

Inside a domain transaction call `NotificationPort.record_event`, then force the transaction to roll back; assert no Notification rows remain. Commit the same operation and assert the Notification rows exist before Celery delivery. This prevents an ephemeral in-memory event from being lost between DB commit and enqueue.

- [ ] **Step 2: Write event idempotency test**

Process the same `REVISION_REQUIRED` event key twice; unique event key results in one logical set of deliveries.

- [ ] **Step 3: Wire Claim creation and validation failure**

Successful Claim creation records/schedules DDL notifications in the same business transaction. When validation fails and the Claim again requires student action, call the scheduler to create only future, not-yet-fired 24h/4h deliveries. A future delivery already present remains unique.

- [ ] **Step 4: Implement handlers**

No unrelated domain state mutation belongs here. The port persists notification intent; Celery dispatches only after commit.

- [ ] **Step 5: Run and commit**

```bash
git add backend/app/modules/notifications/event_handlers.py backend/tests/integration/notifications/test_event_notifications.py
git commit -m "feat: translate domain events into notifications"
```

### Task 6: Implement Claim Expiry Worker

**Files:**
- Create: `backend/app/workers/jobs/expire_claims.py`
- Modify: `backend/app/modules/tasks/claim_service.py`
- Create: `backend/tests/workers/test_expire_claims.py`
- Create: `backend/tests/integration/tasks/test_submit_expire_race.py`

**Interfaces:**
- Produces `ClaimService.expire_claim_if_due(claim_id, now) -> ExpireResult`.

- [ ] **Step 1: Write candidate-selection tests**

Worker scans only statuses that still require student action and where effective deadline is due.

- [ ] **Step 2: Write under-review protection test**

Claim has valid Submission UNDER_REVIEW after grace; expiry job leaves it unchanged and Assignment OCCUPIED.

- [ ] **Step 3: Write submit-vs-expire race test**

Use two transactions/barrier. A valid finalize before grace racing with expiry must result in Submission-owned Claim, never Assignment AVAILABLE.

- [ ] **Step 4: Implement service transaction**

Worker discovers IDs only. Service locks Claim, re-checks Submission/reward lock/status/deadline, then marks EXPIRED and Assignment AVAILABLE if still eligible.

- [ ] **Step 5: Run and commit**

```bash
git add backend/app/workers/jobs/expire_claims.py backend/app/modules/tasks/claim_service.py backend/tests
git commit -m "feat: expire claims without racing valid submissions"
```

### Task 7: Implement File Retention Cleanup

**Files:**
- Create: `backend/app/workers/jobs/cleanup_files.py`
- Create: `backend/tests/workers/test_file_cleanup.py`

**Interfaces:**
- Produces `cleanup_expired_files(now) -> CleanupSummary`.

- [ ] **Step 1: Write retention cases**

Delete expired 30/90/180-day object, retain future object, retain permanent object, retain legal-hold object, retain object referenced by active review when policy says protected.

- [ ] **Step 2: Write missing-object reconciliation test**

Object storage returns 404 for DB row marked present; cleanup marks object deleted/reconciled with warning, preserves Submission metadata/report.

- [ ] **Step 3: Implement idempotent delete**

Second cleanup run makes no additional external delete side effect for already deleted record.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/workers/jobs/cleanup_files.py backend/tests/workers/test_file_cleanup.py
git commit -m "feat: enforce task file retention safely"
```

### Task 8: Add Due-Delivery Dispatcher, Notification Inbox APIs, and Worker Gate

**Files:**
- Create: `backend/app/workers/jobs/dispatch_due_notifications.py`
- Create: `backend/app/modules/notifications/router.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/workers/test_due_notification_dispatch.py`
- Create: `backend/tests/integration/notifications/test_notification_api.py`

**Interfaces:**
- Produces `GET /api/v1/notifications`, mark-read endpoint, Admin failed-delivery query later consumed by Plan 08.

- [ ] **Step 1: Write due-dispatch test**

Seed two due PENDING deliveries and one future delivery. Dispatcher enqueues exactly the two due IDs; running dispatcher twice does not create new Delivery rows and send jobs remain idempotent.

- [ ] **Step 2: Implement bounded due scan**

Query indexed `status + scheduled_at` in batches, enqueue delivery IDs, and leave sending/idempotency decisions to `send_notification_delivery`.

- [ ] **Step 3: Write ownership test**

Student can list/mark own notifications only.

- [ ] **Step 4: Implement paginated inbox**

Do not expose provider internals or another user's delivery data.

- [ ] **Step 5: Run module gate**

```bash
cd backend
pytest tests/unit/notifications tests/integration/notifications tests/workers/test_notification_delivery.py tests/workers/test_expire_claims.py tests/workers/test_file_cleanup.py -v
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/modules/notifications/router.py backend/app/main.py backend/tests
git commit -m "feat: expose notification inbox and worker flows"
```
