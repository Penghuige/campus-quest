# backend/app/modules/identity/schemas.py
"""Identity command and response schemas (spec §5, §40).

Three deliberately different shapes (backend-engineering §9):

- ``RegisterStudent`` is an internal command dataclass, not a Pydantic
  request model: the HTTP request schema (untrusted transport input) lives
  alongside it below and constructs this command after its own parsing.
- The ``*Request`` models are the untrusted transport input of the identity
  API (Task 9): plain Pydantic models carrying raw caller strings only —
  normalization and business validation belong to the services.
- The ``*Response``/public models are the response contract. Each
  enumerates its fields and is built explicitly from the ORM object —
  never serialized from it — so ``password_hash`` and any future internal
  column are unrepresentable in a response, privacy by construction
  (spec §40).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User


@dataclass(frozen=True, slots=True)
class RegisterStudent:
    """Command for ``IdentityService.register_student`` (spec §5.1-5.4).

    ``student_number`` and ``nickname`` are raw caller input; the service
    validates/normalizes them through the Task-2 validators before any
    persistence. ``phone_token`` references an already-verified phone
    challenge (Task 4 owns the lifecycle). ``password`` is plaintext in
    memory only and is hashed through the ``PasswordHasher`` port before
    the User row exists.
    """

    student_number: str
    nickname: str
    phone_token: str
    password: str


# --- Transport request models (Task 9; raw caller input, validated by services)


class PhoneChallengeRequest(BaseModel):
    """Request one registration OTP for ``phone`` (spec §5.4, §33.2)."""

    phone: str


class PhoneChallengeVerifyRequest(BaseModel):
    """Submit the SMS code for one challenge (spec §5.4)."""

    code: str


class RegisterRequest(BaseModel):
    """Register one whitelisted student (spec §5.1-5.4)."""

    student_number: str
    nickname: str
    phone_token: str
    password: str


class LoginRequest(BaseModel):
    """Student login: student number + password (spec §5.6)."""

    username: str
    password: str


class StaffLoginRequest(BaseModel):
    """Staff login: verified email + password + TOTP/recovery code (§5.8)."""

    email: str
    password: str
    totp_code: str


class RefreshRequest(BaseModel):
    """Rotate one refresh session; the token may also arrive by cookie.

    ``refresh_token`` is omitted by cookie-authenticated clients (the
    HttpOnly refresh cookie carries it); non-browser clients pass it here.
    """

    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    """Logout one session; cookie-carried tokens need no body."""

    refresh_token: str | None = None


class NicknameUpdateRequest(BaseModel):
    """Change the account nickname (spec §5.3)."""

    nickname: str


class PhoneChangeRequest(BaseModel):
    """Start a phone change: re-auth plus the NEW phone (spec §5.4)."""

    password: str
    new_phone: str


class PhoneChangeConfirmRequest(BaseModel):
    """Confirm a phone change with the NEW phone's OTP code (spec §5.4)."""

    challenge_id: UUID
    code: str


class EmailBindRequest(BaseModel):
    """Bind (or re-bind) an email and send its verification token (§5.5)."""

    email: str


class EmailVerifyRequest(BaseModel):
    """Confirm an email binding with the token from the message (§5.5)."""

    token: str


class EmailUnbindRequest(BaseModel):
    """Unbind the email after re-authentication (spec §5.5)."""

    password: str


class PasswordChangeRequest(BaseModel):
    """Rotate the password after re-authentication (spec §5.6)."""

    current_password: str
    new_password: str


class PasswordForgotRequest(BaseModel):
    """Request a password-reset OTP on the bound phone (spec §5.6)."""

    username: str


class PasswordResetRequest(BaseModel):
    """Confirm a password reset with the OTP code and a new password."""

    challenge_id: UUID
    code: str
    new_password: str


class StaffInvitationAcceptRequest(BaseModel):
    """Trade the single-use invitation token for a staff account (§5.8)."""

    token: str
    password: str


class TotpConfirmRequest(BaseModel):
    """Confirm the pending TOTP credential with one valid code (§5.8)."""

    code: str


# --- Response models (public contract; explicit field enumeration only)


class UserPublic(BaseModel):
    """Public account view returned to the account owner (spec §40).

    The owner sees their own username/student number; no other surface
    exposes it. Status is included because ACTIVE (vs PENDING_PHONE)
    is the registration outcome clients branch on.
    """

    id: UUID
    username: str
    nickname: str
    role: Role
    status: UserStatus

    @classmethod
    def from_user(cls, user: User) -> UserPublic:
        """Project an ORM ``User`` into the public contract, field by field."""

        return cls(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            role=Role(user.role),
            status=UserStatus(user.status),
        )


class MePublic(UserPublic):
    """The owner's own account page (spec §40): contacts included.

    Phone and email are the owner's own data (they typed them); the DTO
    still enumerates fields explicitly, so the Argon2id verifier and any
    other internal column stay unrepresentable.
    """

    phone_e164: str | None
    email_normalized: str | None
    email_verified_at: datetime | None

    @classmethod
    def from_user(cls, user: User) -> MePublic:
        return cls(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            role=Role(user.role),
            status=UserStatus(user.status),
            phone_e164=user.phone_e164,
            email_normalized=user.email_normalized,
            email_verified_at=user.email_verified_at,
        )


class ChallengeResponse(BaseModel):
    """Request-side view of an OTP challenge (never the code, §33.2)."""

    challenge_id: UUID
    expires_at: datetime


class PhoneTokenResponse(BaseModel):
    """The single-use proof minted by a verified challenge (spec §5.4)."""

    phone_token: str
    expires_at: datetime


class TokenPairResponse(BaseModel):
    """One issued session (spec §5.6) — cookie-only refresh delivery.

    The long-lived refresh token NEVER rides in the body (PR review fix):
    it travels exclusively in the HttpOnly refresh cookie the response
    sets, so a JSON body (loggers, proxies, XSS-readable storage) can
    never leak a live refresh credential. The body's ``access_token`` is
    the short-lived JWT — including the ``must_setup_totp`` pending staff
    session's, which is deliberate: it is not the long-lived credential
    and confined-by-construction to the TOTP setup endpoints.
    ``csrf_token`` mirrors the non-HttpOnly CSRF cookie so scripted
    clients can echo it without parsing ``Set-Cookie``.
    """

    access_token: str
    csrf_token: str
    token_type: str = "bearer"


class TotpSetupResponse(BaseModel):
    """A freshly generated TOTP credential, displayed exactly once (§5.8)."""

    secret: str
    otpauth_uri: str


class TotpConfirmResponse(BaseModel):
    """Recovery codes from enabling 2FA, shown exactly once (spec §5.8)."""

    recovery_codes: list[str] = Field(min_length=1)


class EmailChallengeResponse(BaseModel):
    """Request-side view of one email-verification cycle (token-less)."""

    expires_at: datetime
