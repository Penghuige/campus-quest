# backend/app/core/config.py
"""Typed deployment settings (docs/quality/backend-engineering.md §17).

Required deployment configuration is read from the environment and validated
at startup so a misconfigured deployment fails fast with a readable error.
"""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Development-only secrets (backend-engineering §17 fail-fast): all are
# KNOWN values committed to the repository, so production must override them.
# - OTP HMAC key: with it, anyone who can read Redis brute-forces the 10^6
#   code space offline instantly (spec §33.2).
# - Access-token signing key: with it, anyone can forge valid JWTs (§5.6).
# - TOTP encryption key: with it, anyone who can read the database decrypts
#   every stored staff TOTP secret (§5.6/§5.8).
# Production settings reject all three — see `_reject_insecure_secrets_in_production`.
_INSECURE_OTP_HMAC_SECRET = "dev-only-insecure-otp-hmac-secret"
_INSECURE_TOKEN_SECRET = "dev-only-insecure-access-token-secret"
# A Fernet key is 32 url-safe base64 bytes; this sentinel decodes to the
# ASCII marker "dev-only-insecure-totp-key------" so the committed value is
# both structurally valid and self-describing.
_INSECURE_TOTP_ENCRYPTION_KEY = "ZGV2LW9ubHktaW5zZWN1cmUtdG90cC1rZXktLS0tLS0="
_INSECURE_SECRET_SENTINELS = (
    _INSECURE_OTP_HMAC_SECRET,
    _INSECURE_TOKEN_SECRET,
    _INSECURE_TOTP_ENCRYPTION_KEY,
)

# (field, env var) pairs the production guard checks against their dev-only
# sentinel defaults; extend this table when a new committed-secret default
# lands.
_INSECURE_PRODUCTION_SENTINELS: tuple[tuple[str, str], ...] = (
    ("otp_hmac_secret", "OTP_HMAC_SECRET"),
    ("token_secret", "TOKEN_SECRET"),
    ("totp_encryption_key", "TOTP_ENCRYPTION_KEY"),
)

# (field, env var) pairs the SAME production guard refuses on the logging
# provider: "logging" records the send and delivers nothing, so running it
# in production would mark deliveries SENT that were never sent (G4
# external side effects must not fake success). V1 has no alternative
# value — real SMS/EMAIL adapters are a separate later project — so until
# they land a production deployment fails closed at startup by design.
_LOGGING_ONLY_PROVIDER_FIELDS: tuple[tuple[str, str], ...] = (
    ("sms_provider", "SMS_PROVIDER"),
    ("email_provider", "EMAIL_PROVIDER"),
)


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    s3_endpoint_url: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    # Signing region for the S3 client. "us-east-1" matches MinIO, which
    # ignores the region in signatures; a real AWS deployment must set the
    # bucket's actual region (a wrong region breaks presigned URLs against
    # AWS). Consumed by `S3ObjectStorage`.
    s3_region: str = "us-east-1"
    # Bounded provider calls — AVAILABILITY CONTROLS ONLY (post-merge
    # P0 ruling): per-attempt connect/read caps and the TOTAL attempts
    # per API call (botocore `total_max_attempts` semantics — the
    # initial request INCLUDED). They shorten failure recovery, bound
    # the common provider hang, and keep worker availability up. They
    # are NOT a correctness proof: connect+read timeouts bound socket
    # waits, not a request's total lifetime (DNS/streaming/retry gaps
    # are not a deadline), so no wall-clock arithmetic may claim "an
    # already-sent DELETE has surely finished by X". Cleanup correctness
    # comes from claim ownership instead (Option B): provider failures
    # keep the unfinished claim; the lease authorizes takeover only.
    s3_connect_timeout_seconds: int = 10
    s3_read_timeout_seconds: int = 30
    s3_delete_total_attempts: int = 2
    business_timezone: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    max_upload_bytes_default: int = 200 * 1024 * 1024
    # Presigned upload grant lifetimes (spec §10: 短时): the presigned
    # URL must expire STRICTLY BEFORE the single-use intent — the
    # orphan-intent cleanup (files/cleanup_service) deletes expired
    # intents' objects on the assumption that no legal PUT can land
    # past expiry, which holds only under this ordering. Consumed by
    # `get_upload_service` (submissions/router.py), which converts the
    # seconds into the `UploadService` TTLs; the service's constructor
    # raises on a violating pair, so a misconfigured deployment fails
    # loudly at the wiring point instead of arming a cleanup that
    # deletes objects a live URL can still write to. Defaults: 10m
    # URL, 15m intent.
    upload_url_ttl_seconds: int = 600
    upload_intent_ttl_seconds: int = 900

    # Deployment profile: "production" turns insecure development defaults
    # into startup failures (the OTP HMAC, access-token, and TOTP-encryption
    # sentinels, plus the logging-only SMS/EMAIL providers below).
    environment: Literal["development", "production"] = "development"

    # Outbound channel provider selection (PR #2 hardening P0-2,
    # fail-closed): V1 ships exactly one implementation per channel — the
    # logging adapter that records the send and delivers nothing. Real
    # providers (Twilio/SMTP) are a separate later project; when they
    # land, extend these Literals with the real values and the
    # composition points' factories with them. Until then "logging" is
    # the only value, and environment=production refuses it at
    # construction (see the production guard below): a deployment must
    # never mark deliveries SENT that were never actually sent.
    sms_provider: Literal["logging"] = "logging"
    email_provider: Literal["logging"] = "logging"

    # Phone OTP challenge lifecycle (spec §33.2 recommended defaults:
    # 5-minute TTL, 5 verification attempts, resend cooldown, per-phone and
    # per-IP hourly/daily request caps). Consumed by `OtpPolicy.from_settings`.
    otp_ttl_seconds: int = 300
    otp_max_verify_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_verified_token_ttl_seconds: int = 600
    otp_phone_hourly_request_limit: int = 5
    otp_phone_daily_request_limit: int = 20
    otp_ip_hourly_request_limit: int = 50
    otp_ip_daily_request_limit: int = 200
    # HMAC key for at-rest OTP/token hashing in Redis (spec §33.2: never
    # plaintext). The default exists for local development only; production
    # deployments must set OTP_HMAC_SECRET and fail fast otherwise.
    otp_hmac_secret: str = _INSECURE_OTP_HMAC_SECRET
    # HS256 signing key for short-lived access tokens (spec §5.6). Same
    # deal: a committed development default that production refuses.
    token_secret: str = _INSECURE_TOKEN_SECRET
    # Fernet key encrypting staff TOTP secrets at rest (spec §5.6, §5.8):
    # `totp_credentials.secret_encrypted` must never hold plaintext. Same
    # sentinel pattern — development default, production refuses it.
    totp_encryption_key: str = _INSECURE_TOTP_ENCRYPTION_KEY
    # Staff invitation link lifetime (spec §5.8: 一次性、短时有效); 48h is
    # the product default window.
    staff_invitation_ttl_hours: int = 48
    # Email-verification token lifetime (spec §5.5); wired into
    # `EmailVerificationService` at the composition root.
    email_verification_token_ttl_hours: int = 24
    # Assignment batch-import caps (spec §7.1; backend-engineering §14:
    # teacher-uploaded import files are untrusted, so size and row counts
    # are bounded before parsing). Consumed by `AssignmentImportService`,
    # which receives the scalars at the composition root.
    assignment_import_max_file_bytes: int = 2 * 1024 * 1024
    assignment_import_max_rows: int = 5000
    assignment_import_keyword_max_length: int = 255
    # Preview->confirm window: the server-side preview payload outlives the
    # confirm deadline only as a Redis backstop TTL (see importer.py).
    assignment_import_preview_ttl_seconds: int = 900
    # Region for parsing domestic phone input into E.164 (spec §5.4).
    phone_default_region: str = "CN"
    # Per-student daily abandon cap, counted per BUSINESS_TIMEZONE natural
    # day (spec §8.5: the default is the spec's 2, and the limit is
    # explicitly configurable). The SEED `AbandonService` falls back to —
    # the audited ABANDON_DAILY_LIMIT system-settings row is the fact a
    # deployment moves once an admin sets it (G7 row-over-seed).
    daily_abandon_limit: int = 2
    # Validation worker sandbox bounds (spec §33.3 CPU/内存/时间限制; the
    # plan-04 task-7 parked rulings): every validator executes in a
    # subprocess under these hard limits. Defaults are sized so legitimate
    # work under the 200 MB upload cap and the validators' own preflight
    # bounds always fits, while the parked ~5x total-cap decompression
    # amplification hits the memory wall first.
    validation_wall_timeout_seconds: int = 120
    validation_memory_limit_mb: int = 1024
    validation_cpu_seconds: int = 60
    # §12.4 safe preview (前 N 行安全预览): how many data rows and how
    # much of each cell value the persisted validation report carries.
    validation_preview_rows: int = 10
    validation_preview_value_chars: int = 200
    # Comment content cap (spec §21.1: 单条长度默认上限 2000 个字符，可配置
    # — the default is the spec's 2000 and deployments may tighten it).
    # Consumed by `CommentService`, which receives the scalar at the
    # composition root.
    comment_max_length: int = 2000
    # The current academic term key snapshotted onto new reward
    # redemptions (spec §16.1). The committed value is the development
    # default; deployments set CURRENT_ACADEMIC_TERM per term. Plan 08
    # moves this to the audited, admin-configurable CURRENT_ACADEMIC_TERM
    # system setting; until then this settings field is the single
    # non-code place a deployment turns the term. Consumed by
    # `SettingsAcademicTermProvider` (points/redemption_service.py).
    current_academic_term: str = "2026-fall"
    # Due-delivery dispatcher (plan 07 T8; spec §25.4: bounded retries must
    # stay observable): one scan batch's enqueue ceiling, and how long a
    # SENDING claim may sit before the scan re-enqueues the row. The same
    # seconds value is wired into the send service's claim gate, so the
    # scanner (which rows to re-enqueue) and the gate (which claims to
    # re-claim) always agree on the lease threshold — V1 lease semantics
    # are this timestamp heuristic over `updated_at`, not a lease column.
    notification_dispatch_batch_limit: int = 500
    notification_dispatch_stale_sending_seconds: int = 900
    # Stuck-SENDING automatic recovery monitor (Plan 08 W5a carry, the
    # aged-SENDING ruling): how long a delivery may sit in SENDING
    # before the ``workers.recover_stuck_sending`` beat flips it to
    # RETRYABLE for re-dispatch. Distinct from (and shorter than) the
    # dispatcher's re-enqueue heuristic above ON PURPOSE: the monitor
    # is the STATE-side path (SENDING -> RETRYABLE, one conditional
    # UPDATE) that hands the row back to the ordinary due scan, while
    # the dispatcher's branch remains the re-enqueue path; sized above
    # any healthy provider call (the send service holds no row lock
    # across provider I/O, but a slow call still owns its SENDING
    # lease), so a live sender is never scooped. The recovery's
    # per-beat batch rides ``notification_dispatch_batch_limit``.
    notification_sending_stuck_threshold_seconds: int = 600
    # Beat cadences (PR #2 hardening, MERGE_CARRIES item 4): how often the
    # Celery beat fires each scheduled scan. The dispatch scan keeps the
    # §25.4 ~1-minute retry rung honest; the expiry scan judges hour-scale
    # deadlines; file retention is day-scale, so its scan runs cold. Batch
    # limits (the *_batch_limit / scan LIMIT constants) stay the burst
    # bound — a slower cadence only delays work, never enlarges it.
    notification_dispatch_scan_interval_seconds: int = 60
    notification_sending_stuck_scan_interval_seconds: int = 60
    claim_expiry_scan_interval_seconds: int = 60
    file_cleanup_scan_interval_seconds: int = 900
    # Cleanup deletion-claim lease (PR #2 hardening pass 5a, P0-2): how
    # long a claimed deletion right stays exclusive. Sized above the
    # claim -> provider delete -> completion-mark window of a healthy
    # worker, so a live claim is never stolen; a worker that dies between
    # its claim commit and the S3 delete leaves exactly this much
    # stranded-claim exposure before the next scan takes over (re-claims
    # under the protection paths' own row locks and converges, §27).
    # Wired into SubmissionCleanupRepository by the cleanup job.
    cleanup_claim_lease_seconds: int = 300
    # Stale-VALIDATING recovery (PR #2 hardening sub-F2): a submission
    # whose validation job exhausted its bounded retries (or lost its
    # instances) stays VALIDATING forever with its claim riding along in
    # VALIDATING. The requeue scan re-dispatches rows whose newest run
    # row started longer ago than this threshold — sized above the
    # validation job's worst legitimate window (6 attempts x
    # download+sandbox up to ~2 min each + backoff ≈ 13 min), so an
    # in-flight retry ladder is never re-dispatched underneath itself;
    # a re-dispatch is always safe anyway (the service's terminal and
    # stale-rerun gates replay idempotently).
    stale_validating_requeue_seconds: int = 1800
    # How often the stale-VALIDATING scan looks for wedged rows.
    stale_validating_scan_interval_seconds: int = 300
    # Stale-UPLOADED recovery (PR #2 hardening final pass B, P1): a
    # finalize commit can outlive its validation dispatch (a broker
    # outage at the enqueue, and the client never retries), and a
    # validation job whose tx1 kept refusing (CleanupClaimConflictError
    # against a live deletion lease) exhausts Celery's bounded retries —
    # ~31s of backoff against a 300s lease — with the row never leaving
    # UPLOADED, a shape the stale-VALIDATING watchdog cannot see (no run
    # row exists). The requeue scan re-dispatches UPLOADED rows whose
    # ``submitted_at`` is older than this grace, so a freshly finalized
    # row whose dispatch simply has not landed yet is never scooped up
    # (a grace below the healthy finalize->enqueue round trip would
    # re-dispatch every fresh submission on every beat). Repeat
    # dispatch is safe by the validation service's own tx1 gates (see
    # the requeue job's docstring).
    uploaded_dispatch_grace_seconds: int = 120
    # Optional management-network restriction for staff/admin surfaces
    # (Plan 08 T8 step 4). DEPRECATED transitional fallback (Plan 08 T5):
    # the policy's home is the audited system_settings store — keys
    # MANAGEMENT_NETWORK_ENABLED / MANAGEMENT_NETWORK_CIDRS — resolved
    # store-first by app/core/admin_network_policy.py. These env fields
    # survive ONLY as the per-key fallback while no store row exists,
    # so deployments configured against them keep working through the
    # transition; they WILL BE REMOVED BEFORE THE V1 RELEASE (the
    # fields and the loader's fallbacks go together — do not build new
    # deployment configuration on them). ``management_network_cidrs``
    # is comma-separated CIDRs parsed by app/core/admin_network_policy.py
    # (standard-library ipaddress; an invalid entry fails policy
    # construction loudly). Disabled (the default) is pure pass-through
    # — 2FA/RBAC still apply.
    management_network_enabled: bool = False
    management_network_cidrs: str = ""

    @field_validator("business_timezone")
    @classmethod
    def _validate_business_timezone(cls, value: str) -> str:
        # Fail fast on an invalid IANA name at settings load, not deep inside
        # a request that first needs a business-timezone period.
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"business_timezone must be a valid IANA timezone name, got {value!r}"
            ) from exc
        return value

    @field_validator("daily_abandon_limit")
    @classmethod
    def _validate_daily_abandon_limit(cls, value: int) -> int:
        # A cap below 1 makes abandoning unreachable while still consuming
        # the claim; a deployment wanting that should disable the action, not
        # set an unusable quota. Fail at settings load, not at the first
        # abandon attempt.
        if value < 1:
            raise ValueError(f"daily_abandon_limit must be >= 1, got {value}")
        return value

    @field_validator("comment_max_length")
    @classmethod
    def _validate_comment_max_length(cls, value: int) -> int:
        # A cap below 1 rejects every comment while still consuming the
        # comment rate-limit budget (spec §33.1); a deployment wanting the
        # surface closed should disable it, not set an unusable quota. Fail
        # at settings load, not at the first comment attempt.
        if value < 1:
            raise ValueError(f"comment_max_length must be >= 1, got {value}")
        return value

    @field_validator("notification_dispatch_batch_limit")
    @classmethod
    def _validate_notification_dispatch_batch_limit(cls, value: int) -> int:
        # A ceiling below 1 disables the scan (every beat discovers
        # nothing); a deployment wanting dispatch off should not run the
        # beat, not configure a scan that silently drops every due
        # delivery.
        if value < 1:
            raise ValueError(
                f"notification_dispatch_batch_limit must be >= 1, got {value}"
            )
        return value

    @field_validator("notification_dispatch_stale_sending_seconds")
    @classmethod
    def _validate_notification_dispatch_stale_sending_seconds(cls, value: int) -> int:
        # A zero threshold would make every in-flight claim instantly
        # "stale" and re-enqueue live sends on every beat — the opposite
        # of the lease the heuristic stands in for.
        if value < 1:
            raise ValueError(
                f"notification_dispatch_stale_sending_seconds must be >= 1, got {value}"
            )
        return value

    @field_validator("notification_sending_stuck_threshold_seconds")
    @classmethod
    def _validate_notification_sending_stuck_threshold_seconds(cls, value: int) -> int:
        # A zero threshold would flip every live in-flight claim to
        # RETRYABLE on every beat — re-dispatching healthy sends and
        # racing their finalizes. The threshold must sit strictly above
        # a healthy send's whole lifetime (no upper bound: a longer one
        # only delays recovery, which the manual force-fail command
        # backstops).
        if value < 1:
            raise ValueError(
                "notification_sending_stuck_threshold_seconds must be >= 1 second"
            )
        return value

    @field_validator(
        "notification_dispatch_scan_interval_seconds",
        "notification_sending_stuck_scan_interval_seconds",
        "claim_expiry_scan_interval_seconds",
        "file_cleanup_scan_interval_seconds",
        "stale_validating_scan_interval_seconds",
    )
    @classmethod
    def _validate_scan_interval_seconds(cls, value: int) -> int:
        # A sub-second interval would spin the beat loop; a deployment
        # wanting a scan off should not schedule it, not configure a
        # cadence the broker cannot absorb.
        if value < 1:
            raise ValueError(f"beat scan intervals must be >= 1 second, got {value}")
        return value

    @field_validator("cleanup_claim_lease_seconds")
    @classmethod
    def _validate_cleanup_claim_lease_seconds(cls, value: int) -> int:
        # A sub-second lease would treat every live claim as expired and
        # re-claim in-flight deletions on every scan — the opposite of
        # the exclusivity the lease exists for. (There is deliberately no
        # upper bound: a longer lease only widens the crash-stranded
        # window, which the takeover still closes.)
        if value < 1:
            raise ValueError(
                f"cleanup_claim_lease_seconds must be >= 1 second, got {value}"
            )
        return value

    @field_validator("stale_validating_requeue_seconds")
    @classmethod
    def _validate_stale_validating_requeue_seconds(cls, value: int) -> int:
        # A threshold below the job's own retry window would re-dispatch
        # healthy in-flight work on every scan; keep it at least as long
        # as one validation attempt's sandbox wall timeout.
        if value < 1:
            raise ValueError(
                f"stale_validating_requeue_seconds must be >= 1, got {value}"
            )
        return value

    @field_validator("uploaded_dispatch_grace_seconds")
    @classmethod
    def _validate_uploaded_dispatch_grace_seconds(cls, value: int) -> int:
        # A zero grace would re-dispatch every fresh finalize before its
        # validation enqueue even lands — a hot loop over healthy rows.
        # No upper bound: a longer grace only delays recovery, and the
        # scan absorbs it (see the requeue job's docstring).
        if value < 1:
            raise ValueError(
                f"uploaded_dispatch_grace_seconds must be >= 1, got {value}"
            )
        return value

    @field_validator("token_secret")
    @classmethod
    def _validate_token_secret_length(cls, value: str) -> str:
        # RFC 7518 §3.2: an HS256 key should be at least 32 bytes; PyJWT
        # warns on every encode/decode with a shorter one. A deployment
        # that sets a short TOKEN_SECRET must fail at settings load, not
        # spam warnings (or run weakly) at runtime.
        if len(value) < 32:
            raise ValueError(
                f"token_secret must be at least 32 characters for HS256 "
                f"(got {len(value)})"
            )
        return value

    @field_validator("totp_encryption_key")
    @classmethod
    def _validate_totp_encryption_key(cls, value: str) -> str:
        # Fernet accepts exactly 32 url-safe base64-encoded bytes; anything
        # else raises at first use. A deployer pasting a passphrase must
        # fail at settings load in any environment, not at the first staff
        # login attempt.
        try:
            Fernet(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "totp_encryption_key must be a valid Fernet key "
                "(32 url-safe base64-encoded bytes, e.g. "
                "Fernet.generate_key())"
            ) from exc
        return value

    @model_validator(mode="after")
    def _reject_insecure_secrets_in_production(self) -> "Settings":
        # Known sentinels and the logging provider are development-only
        # wiring (see the constants' comments): a production deployment
        # must fail at startup, not run silently unprotected or on a
        # provider that fakes success. Every listed field is checked so
        # the one error names everything the deployer must set.
        offenders = [
            env_name
            for field_name, env_name in _INSECURE_PRODUCTION_SENTINELS
            if getattr(self, field_name) in _INSECURE_SECRET_SENTINELS
        ]
        if self.environment == "production" and offenders:
            raise ValueError(
                "environment=production refuses development-only default "
                f"secrets: set {' and '.join(offenders)} to "
                "deployment-specific values (a committed sentinel lets "
                "anyone brute-force stored OTP hashes offline, forge "
                "access tokens, or decrypt stored TOTP secrets)"
            )
        logging_providers = [
            env_name
            for field_name, env_name in _LOGGING_ONLY_PROVIDER_FIELDS
            if getattr(self, field_name) == "logging"
        ]
        if self.environment == "production" and logging_providers:
            raise ValueError(
                "environment=production refuses the logging provider: "
                f"set {' and '.join(logging_providers)} to a real sending "
                "provider (the logging adapter delivers nothing while "
                "deliveries are recorded SENT; real SMS/EMAIL adapters "
                "land with the provider project, so V1 channels cannot "
                "run in production)"
            )
        return self

    @field_validator(
        "s3_connect_timeout_seconds",
        "s3_read_timeout_seconds",
    )
    @classmethod
    def _validate_s3_timeout_bounds(cls, value: int) -> int:
        # Availability-control sanity (post-merge P0, P2 hardening): a
        # sub-second timeout would fail every call; an unbounded one
        # would reintroduce the multi-minute provider hang these exist
        # to shorten. NOT a correctness rule (see the field comments).
        if not 1 <= value <= 300:
            raise ValueError(
                f"s3 timeouts must be within [1, 300] seconds, got {value}"
            )
        return value

    @field_validator("s3_delete_total_attempts")
    @classmethod
    def _validate_s3_attempts_bounds(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError(
                f"s3_delete_total_attempts must be within [1, 10], got {value}"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
