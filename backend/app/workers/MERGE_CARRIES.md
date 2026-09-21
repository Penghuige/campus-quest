# Plan 07 merge carries — the concentrated checklist

Every item deferred from the S3 notifications/workers branch to the
stream merge lives HERE and only here (the in-code TODOs point back to
this file; do not scatter new ones). Work top to bottom at merge; each
item names its file, the exact action, and the verification that closes
it. Context: this branch built notifications + workers on top of the
identity/tasks stream; the submissions/review/redemption/points tables
live on the plans stream and do not exist here.

## 1. Wire the real VALIDATED-reading inspector into build_expire_service

- File: `app/workers/jobs/expire_claims.py` (`build_expire_service`,
  the production composition site) + the `ValidSubmissionInspector`
  seam in `app/modules/tasks/claim_service.py`.
- Action: construct the real inspector that reads whether the claim
  has a machine-VALIDATED submission in the protection window and pass
  it at the constructor. The default `NoValidSubmissionsInspector`
  answers False, so today an in-window-but-unvalidated submission does
  NOT block expiry (amendment-2 strict reading, spec §11.5/§26).
- Verify: the strict-reading pins
  (`test_strict_reading_unvalidated_submission_still_expires`,
  `test_inspector_true_protects_actionable_due_claim`) stay green with
  the real reader wired, plus one integration test where a VALIDATED
  submission protects a due claim end to end.

## 2. Emit NotificationPort events from the plans-stream producers

- Files: the submissions validation service, review flow, points
  redemption, and identity security producers (they live on the plans
  stream); conventions frozen in
  `app/modules/notifications/event_handlers.py` (payload whitelists in
  `templates.EVENT_VARIABLES`, `claim_id`/`deadline_at` re-arm pair,
  `task_policy` required for planning).
- Action: call the notification port with those conventions from each
  producer's transaction (outbox rule: record inside the business
  transaction). The claim path (`tasks.claim_service`) is already
  wired on this branch; every other producer is currently exercised
  only with synthetic events.
- Verify: one integration test per producer showing the logical
  Notification + per-channel delivery rows commit with the business
  state.

## 3. Replace the cleanup placeholder with the real repository + job registration

- File: `app/workers/jobs/cleanup_files.py`
  (`PlaceholderCleanupRepository` reads nothing by design).
- Action:
  1. implement the real `CleanupRepository` over the Submission query
     (due retention snapshots joined to claims, excluding in-review
     claims, legal holds, permanent rows, rows already marked deleted);
  2. register the Celery scan shell here (sample `SystemClock`, build
     repository + object-storage adapter, call
     `app.modules.files.cleanup_service.cleanup_expired_files`,
     return the JSON summary);
  3. append this module to `JOB_MODULES` in `app/workers/celery_app.py`
     AND update the pinned task list in
     `tests/workers/test_celery_wiring.py` (the CLI-style registration
     test fails on drift).
- Verify: `tests/workers/test_file_cleanup.py` re-targeted at the real
  repository + the wiring pins green.

## 4. Add the Celery beat schedule for ALL worker jobs (+ the deferred index)

- Files: `app/workers/celery_app.py` (beat config); new migration for
  the index.
- Action: schedule the scans — `workers.expire_claims_scan`,
  `workers.dispatch_due_notifications`, and the cleanup scan from
  item 3 (cadence from settings; keep batch limits as the burst
  bound). Also land the partial deadline index
  (`(grace_deadline_at) WHERE status IN (actionable) AND (revision IS
  NULL OR revision <= grace)`) deferred from the expiry scan: a new
  0011 on this branch would have collided with the plans branch's
  0011, so it becomes the next migration on the MERGED head.
- Verify: beat entries visible via `celery inspect` (or the app's
  `beat_schedule` asserted in `tests/workers/test_celery_wiring.py`);
  `alembic upgrade head` + `alembic check` clean.

## 5. Reparent 0010 onto the merged head and drop the bridge copies

- File: `backend/alembic/versions/0010_notifications.py`
  (`down_revision = "0006"`).
- Action: after the stream merge, repoint `down_revision` to the
  merged head (the plans-stream tip, e.g. `0012`), then DELETE the
  untracked `0005_submissions.py` / `0006_upload_intents.py` bridge
  copies from `backend/alembic/versions/` — they exist only so this
  branch's alembic graph reaches its head and are superseded by the
  plans stream's own 0005/0006 once merged.
- Verify: `alembic history` is a single line; `alembic upgrade head` +
  `alembic check` clean on a fresh database.

## 6. Gate-1 integration test (re-validation vs. tightened allowed_file_types)

- Lands where the submissions module lives (the merged branch): tighten
  a task's `allowed_file_types` after a submission exists and assert
  re-validation fails the declared type. NOT implementable on S3 —
  the re-validation path lives in the submissions/validation service,
  which does not exist here (no submissions table;
  `latest_submission_id` is a bare UUID).
- Verify: the new test plus the existing validation suite green on the
  merged branch.
