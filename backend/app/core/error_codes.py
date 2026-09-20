# backend/app/core/error_codes.py
"""Frozen error-code registry (docs/architecture/interfaces.md).

Single importable source of truth for every §29 envelope code. Pure stdlib
on purpose — no FastAPI, no settings, no persistence — so domain modules,
workers, and the API layer share these constants without dragging web
dependencies into worker or domain imports (verified by
tests/unit/core/test_error_codes.py).

Business codes mirror the "Canonical codes" table; system codes cover
framework failures rendered through the same envelope. They are SYSTEM
codes, not business codes: business logic raises only business codes, and
the frontend never branches on system codes as domain outcomes.

Adding a code means, in order: update the interfaces.md tables FIRST, then
this enum, then the frozen membership lists in the test — so doc drift,
code drift, and test drift each fail loudly on their own.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Every §29 envelope code; `value == member name` by construction."""

    # Business codes (spec §29; interfaces.md "Canonical codes").
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ACCOUNT_NOT_ACTIVE = "ACCOUNT_NOT_ACTIVE"
    STUDENT_NOT_WHITELISTED = "STUDENT_NOT_WHITELISTED"
    PHONE_ALREADY_BOUND = "PHONE_ALREADY_BOUND"
    # Registered in interfaces.md (not a §29-listed code): a username/unique
    # violation means the account already exists — distinct from a whitelist
    # miss and from a phone conflict (spec §5.2, §31.1).
    USERNAME_ALREADY_EXISTS = "USERNAME_ALREADY_EXISTS"
    TASK_NOT_CLAIMABLE = "TASK_NOT_CLAIMABLE"
    NO_ASSIGNMENT_AVAILABLE = "NO_ASSIGNMENT_AVAILABLE"
    ASSIGNMENT_LIMIT_REACHED = "ASSIGNMENT_LIMIT_REACHED"
    TASK_ACTIVE_CLAIM_EXISTS = "TASK_ACTIVE_CLAIM_EXISTS"
    CLAIM_CUTOFF_REACHED = "CLAIM_CUTOFF_REACHED"
    # Registered in interfaces.md (not §29-listed): spec §8.5 typed outcomes
    # for abandoning a claim — the per-natural-day cap and the rejection of
    # claims in a non-student-actionable state (mid-review or terminal under
    # another reason).
    ABANDON_LIMIT_REACHED = "ABANDON_LIMIT_REACHED"
    CLAIM_NOT_ABANDONABLE = "CLAIM_NOT_ABANDONABLE"
    CLAIM_NOT_SUBMITTABLE = "CLAIM_NOT_SUBMITTABLE"
    SUBMISSION_WINDOW_CLOSED = "SUBMISSION_WINDOW_CLOSED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_TYPE_NOT_ALLOWED = "FILE_TYPE_NOT_ALLOWED"
    SUBMISSION_VALIDATION_FAILED = "SUBMISSION_VALIDATION_FAILED"
    ALREADY_REVIEWED = "ALREADY_REVIEWED"
    INSUFFICIENT_POINTS = "INSUFFICIENT_POINTS"
    REWARD_OUT_OF_STOCK = "REWARD_OUT_OF_STOCK"
    REDEMPTION_LIMIT_REACHED = "REDEMPTION_LIMIT_REACHED"
    RATING_NOT_ELIGIBLE = "RATING_NOT_ELIGIBLE"
    # Plan 02 identity-API additions (interfaces.md "Canonical codes" and the
    # "Identity typed-exception mapping" table; spec §5.5, §5.6, §5.8, §33.1,
    # §33.2, §33.4). Each maps one typed identity exception to one code.
    TOTP_SETUP_REQUIRED = "TOTP_SETUP_REQUIRED"
    EMAIL_ALREADY_BOUND = "EMAIL_ALREADY_BOUND"
    INVALID_EMAIL_TOKEN = "INVALID_EMAIL_TOKEN"
    OTP_CODE_INVALID = "OTP_CODE_INVALID"
    OTP_TOO_MANY_ATTEMPTS = "OTP_TOO_MANY_ATTEMPTS"
    OTP_CHALLENGE_EXPIRED = "OTP_CHALLENGE_EXPIRED"
    OTP_CHALLENGE_CONSUMED = "OTP_CHALLENGE_CONSUMED"
    OTP_CHALLENGE_INVALID = "OTP_CHALLENGE_INVALID"
    OTP_TOKEN_INVALID = "OTP_TOKEN_INVALID"
    OTP_RESEND_COOLDOWN = "OTP_RESEND_COOLDOWN"
    RATE_LIMITED = "RATE_LIMITED"

    # System / framework codes (interfaces.md "System / framework codes").
    # Not business codes; raised only by framework error handlers.
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    HTTP_ERROR = "HTTP_ERROR"
