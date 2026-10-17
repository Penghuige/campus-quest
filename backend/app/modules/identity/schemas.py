# backend/app/modules/identity/schemas.py
"""Identity command and response schemas (spec §5, §40).

Three deliberately different shapes (backend-engineering §9):

- ``RegisterStudent`` is an internal command dataclass, not a Pydantic
  request model: the HTTP request schema (untrusted transport input) lives
  alongside it below and constructs this command after its own parsing.
- The ``*Request`` models are the untrusted transport input of the identity
  API: plain Pydantic models carrying raw caller strings only —
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

from pydantic import BaseModel, ConfigDict, Field

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User


@dataclass(frozen=True, slots=True)
class RegisterStudent:
    """Command for ``IdentityService.register_student`` (spec §5.1-5.4).

    ``student_number`` and ``nickname`` are raw caller input; the service
    validates/normalizes them through `app.modules.identity.validation`
    before any persistence. ``phone_token`` references an already-verified
    phone challenge (the OTP service owns the lifecycle). ``password`` is
    plaintext in memory only and is hashed through the ``PasswordHasher``
    port before the User row exists.
    """

    student_number: str
    phone_token: str
    nickname: str | None = None
    password: str | None = None


# --- Transport request models (raw caller input, validated by services)


class PhoneChallengeRequest(BaseModel):
    """Request one registration OTP for ``phone`` (spec §5.4, §33.2)."""

    phone: str


class PhoneChallengeVerifyRequest(BaseModel):
    """Submit the SMS code for one challenge (spec §5.4)."""

    code: str


class RegisterRequest(BaseModel):
    """Register one whitelisted student (spec §5.1-5.4)."""

    student_number: str
    phone_token: str
    nickname: str | None = None
    password: str | None = None


class LoginRequest(BaseModel):
    """Student login: student number + password (spec §5.6)."""

    username: str
    password: str


class StaffLoginRequest(BaseModel):
    """Staff login: verified email + password + TOTP/recovery code (§5.8).

    ``totp_code`` may be EMPTY (or omitted) when a valid remembered-
    device cookie accompanies the request — the cookie is the second
    factor for the 30-day re-login (V1.1 login iteration item 2); empty
    without one is the uniform authentication failure. ``remember_device``
    mints that cookie on a successful full login.
    """

    email: str
    password: str
    totp_code: str = ""
    remember_device: bool = False


class StaffEmailCodeRequestRequest(BaseModel):
    """Ask for a 6-digit login code by email (V1.1 §2.1).

    Always answered 202 with an empty body — staff existence is not an
    answer this endpoint gives (anti-enumeration).
    """

    email: str


class StaffEmailCodeVerifyRequest(BaseModel):
    """Trade the emailed code for a login (V1.1 §2.1/§2.2).

    TEACHER completes in one step; ADMIN gets ``totp_required`` (unless a
    valid remembered-device cookie accompanies the request).
    """

    email: str
    code: str
    remember_device: bool = False


class StaffEmailCodeTotpRequest(BaseModel):
    """Finish an ADMIN email-code login with the TOTP second leg."""

    pending_token: str
    code: str
    remember_device: bool = False


class StaffEmailCodePendingResponse(BaseModel):
    """The ADMIN mid-login state: a short-lived session token that may
    finish the login via the totp leg (or TOTP onboarding endpoints),
    and nothing else."""

    status: str
    pending_token: str


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
    """Trade the single-use invitation token for a staff account (§5.8).

    ``password`` is OPTIONAL for TEACHER (V1.1 §2.3: blank = an
    email-code-only account) and effectively mandatory for ADMIN (the
    service rejects a blank one after the invitation lookup).
    """

    token: str
    password: str | None = None


class StaffSessionResponse(BaseModel):
    """A staff session plus the onboarding branch flag (V1.1 §2.3).

    ``totp_required`` tells the client which screen follows: the ADMIN
    pending-TOTP onboarding (True — the tokens are a real session
    confined to finishing 2FA setup) or a complete login (False).
    """

    access_token: str
    csrf_token: str
    totp_required: bool


class StaffInvitationCreateRequest(BaseModel):
    """Issue one staff invitation (§5.8; Plan 08 T9's Admin surface).

    Raw caller input: the email's normalization and the role's
    TEACHER/ADMIN narrowing belong to ``StaffService
    .create_staff_invitation``.
    """

    model_config = ConfigDict(extra="forbid")

    email: str
    role: Role


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
    #: Whether the account holds a stored avatar (spec amendment D4).
    #: A boolean, never the object key: the display path is the
    #: ``/users/{id}/avatar`` byte proxy, and the key itself is
    #: storage plumbing that must not surface in any DTO.
    has_avatar: bool

    @classmethod
    def from_user(cls, user: User) -> UserPublic:
        """Project an ORM ``User`` into the public contract, field by field."""

        return cls(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            role=Role(user.role),
            status=UserStatus(user.status),
            has_avatar=user.avatar_object_key is not None,
        )


class RememberedDevicePublic(BaseModel):
    """One remembered device of the caller (V1.1 §2.2 self-service).

    Explicitly enumerated fields: the stored token hash is unrepresentable.
    """

    id: UUID
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime


class OwnDevicesRevokedResponse(BaseModel):
    """DELETE /me/devices result: how many credentials were dropped."""

    revoked: int


class MePublic(UserPublic):
    """The owner's own account page (spec §40): contacts included.

    Phone and email are the owner's own data (they typed them); the DTO
    still enumerates fields explicitly, so the Argon2id verifier and any
    other internal column stay unrepresentable.
    """

    phone_e164: str | None
    email_normalized: str | None
    email_verified_at: datetime | None
    #: When the account was created (spec §40 owner's own data; the
    #: teacher info page renders it as the registration date, #17).
    #: ISO-8601 UTC via the JSON encoder — read-only, owner-only.
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> MePublic:
        return cls(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            role=Role(user.role),
            status=UserStatus(user.status),
            has_avatar=user.avatar_object_key is not None,
            phone_e164=user.phone_e164,
            email_normalized=user.email_normalized,
            email_verified_at=user.email_verified_at,
            created_at=user.created_at,
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

    The long-lived refresh token NEVER rides in the body:
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


class RegisterResponse(BaseModel):
    """Registration ends logged in (V1.1 login iteration item 4): the
    account view beside the freshly minted session — the same body the
    login path answers, so the client lands directly in the frozen-home
    browsing mode instead of bouncing through a second login (and the
    60s SMS cooldown) right after proving the phone."""

    user: UserPublic
    tokens: TokenPairResponse


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


class MobileCodeRequestRequest(BaseModel):
    """Ask for a 6-digit SMS login code (V1.1 2026-10-16 §3) — always
    202, staff/role existence is never an answer here."""

    phone: str


class MobileCodeVerifyRequest(BaseModel):
    """Trade the SMS code (or the remembered-device cookie) for a student
    session.

    Three shapes: ``phone``+``code`` (the SMS leg), ``phone`` with
    ``code`` omitted (the cookie must match that phone's user), or an
    EMPTY body (the login-banner click: the device row alone resolves
    the user — the banner only ever has the masked phone).
    """

    phone: str | None = None
    code: str | None = None
    remember_device: bool = False
