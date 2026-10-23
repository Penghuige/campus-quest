# backend/app/modules/identity/unified_login_service.py
"""The unified login challenge facade (2026-10-19 spec §3, PR-1).

A routing layer, nothing more: every arm delegates to an existing
challenge service — storage, cooldown, lockout, HMAC, GETDEL, and
device-credential craft are untouched. What this module owns:

- identifier resolution (E.164 phone → email shape → student number);
- the arms' dispatch (phone by the users row: unregistered/STUDENT →
  the student mobile-code service, TEACHER/ADMIN → the staff one;
  email → the staff email-code service);
- the ONE-code error envelope: wrong / expired / attempts-exhausted
  codes answer ``CHALLENGE_CODE_INVALID`` on every unified arm (the
  branch reason stays in server logs — §5.1 anti-enumeration);
- the discriminated-union outcomes (``status`` is the only judgment
  member);
- the totp endpoint's two-domain service (an email-domain GETDEL miss
  burns nothing, so trying email first then staff-mobile is safe);
- the password form's identifier reach (student phone-first, staff
  email) and the ADMIN pending mint for a proven password with the
  second factor owed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.identity import device_credentials
from app.modules.identity.email_login_service import (
    EmailCodeLoginOutcome,
    EmailLoginService,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.mobile_login_service import (
    MobileLoginService,
    MobileVerifyOutcome,
    StaffMobileCodeOutcome,
)
from app.modules.identity.models import User
from app.modules.identity.otp import InvalidPhoneError, normalize_phone
from app.modules.identity.repository import UserRepository
from app.modules.identity.session_service import SessionService, SessionTokens
from app.modules.identity.staff_service import StaffService

logger = logging.getLogger(__name__)

#: The code-family members the unified arms collapse into the single
#: CHALLENGE_CODE_INVALID. Cooldown/lockout/validation codes pass
#: through untouched — they are not code-guessing branches.
_CODE_FAMILY: frozenset[str] = frozenset(
    {
        ErrorCode.MOBILE_CODE_INVALID.value,
        ErrorCode.EMAIL_CODE_INVALID.value,
        ErrorCode.OTP_CODE_INVALID.value,
        ErrorCode.OTP_CHALLENGE_EXPIRED.value,
        ErrorCode.OTP_CHALLENGE_CONSUMED.value,
        ErrorCode.OTP_CHALLENGE_INVALID.value,
        ErrorCode.OTP_TOKEN_INVALID.value,
        ErrorCode.OTP_TOO_MANY_ATTEMPTS.value,
    }
)

_CHALLENGE_CODE_INVALID_MESSAGE = "验证码错误或已过期"
_IDENTIFIER_SHAPE_MESSAGE = "标识符须为手机号或邮箱"
#: The code door's refusal for a STUDENT NUMBER that the phone-shape
#: classifier swallows (10 digits parse as a valid CN landline — the
#: owner-account bug, 2026-10-23): students log in by password there.
_STUDENT_NUMBER_CODE_DOOR_MESSAGE = "学号请使用密码登录"


@dataclass(slots=True)
class UnifiedOutcome:
    """One unified-arm answer: ``status`` is the judgment member; the
    payloads ride their named fields (tokens for authenticated,
    pending_token for totp_required, register_token for unregistered)."""

    status: str
    role: str | None = None
    user_id: UUID | None = None
    tokens: SessionTokens | None = None
    device_cookie: str | None = None
    pending_token: str | None = None
    register_token: str | None = None


def _challenge_code_invalid() -> BusinessError:
    return BusinessError(
        ErrorCode.CHALLENGE_CODE_INVALID,
        _CHALLENGE_CODE_INVALID_MESSAGE,
        status_code=400,
    )


def _code_of(exc: BusinessError) -> str:
    code = exc.code
    return code.value if isinstance(code, ErrorCode) else str(code)


class UnifiedLoginService:
    """The spec §3 facade over the existing challenge services."""

    def __init__(
        self,
        *,
        clock: Clock,
        email_login: EmailLoginService,
        mobile_login: MobileLoginService,
        staff: StaffService,
        sessions: SessionService,
    ) -> None:
        self._clock = clock
        self._email_login = email_login
        self._mobile_login = mobile_login
        self._staff = staff
        self._sessions = sessions
        self._users = UserRepository()

    # --- identifier resolution -------------------------------------------

    async def _resolve(
        self, db: AsyncSession, identifier: str
    ) -> tuple[str, str, User | None]:
        """Classify the identifier: ('phone', e164, row|None) /
        ('email', normalized, None) / ('student_number', raw, None).

        Phone-first by the normalization SHAPE (an E.164 CN number),
        never by account lookup — an unregistered phone is a valid
        request arm, not an unknown account."""
        value = identifier.strip()
        try:
            phone = normalize_phone(value, "CN")
        except InvalidPhoneError:
            phone = None
        if phone is not None:
            row = await self._users.find_by_phone_e164(db, phone)
            return "phone", phone, row
        if "@" in value:
            return "email", value.lower(), None
        return "student_number", value, None

    # --- request ----------------------------------------------------------

    async def request(self, db: AsyncSession, identifier: str) -> None:
        """Dispatch one code request; always the arm's own 202 semantics
        (or its cooldown 429) — the facade never invents answers."""
        kind, value, row = await self._resolve(db, identifier)
        if kind == "student_number":
            # Format-layer refusal: no account query, no enumeration.
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _IDENTIFIER_SHAPE_MESSAGE,
                status_code=422,
            )
        if kind == "email":
            await self._email_login.request_code(db, value)
            return
        if row is None:
            # A 10-digit student number classifies as a phone (valid CN
            # landline shape). Never text a landline: a registered
            # username behind the number shape answers the student
            # guidance; an unregistered number keeps the entry flow.
            if await self._users.find_by_username(db, identifier.strip()) is not None:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _STUDENT_NUMBER_CODE_DOOR_MESSAGE,
                    status_code=422,
                )
            await self._mobile_login.request_code(db, value)
        elif row.role == Role.STUDENT.value:
            await self._mobile_login.request_code(db, value)
        else:
            await self._mobile_login.request_staff_code(db, value)

    # --- verify -----------------------------------------------------------

    async def verify(
        self,
        db: AsyncSession,
        *,
        identifier: str | None,
        code: str | None,
        device_cookie: str | None,
        remember_device: bool,
        audit_context: AuditContext | None,
    ) -> UnifiedOutcome:
        if identifier is None and (code is None or code.strip() == ""):
            return await self._verify_on_cookie_alone(db, device_cookie=device_cookie)
        if identifier is None or code is None or code.strip() == "":
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                "标识符与验证码须成对提供，或均缺省走记住设备",
                status_code=422,
            )

        kind, value, row = await self._resolve(db, identifier)
        if kind == "student_number":
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _IDENTIFIER_SHAPE_MESSAGE,
                status_code=422,
            )
        try:
            if kind == "email":
                email_outcome = await self._email_login.verify_code(
                    db,
                    email=value,
                    code=code,
                    device_cookie=device_cookie,
                    remember_device=remember_device,
                    audit_context=audit_context,
                )
                return self._from_email(email_outcome)
            if row is None or row.role == Role.STUDENT.value:
                if row is None and (
                    await self._users.find_by_username(db, identifier.strip())
                    is not None
                ):
                    # Same-shape defense as the request door: a student
                    # number behind the phone shape must never enter
                    # the entry flow, even on a stale challenge minted
                    # before the fix. Deterministic, code-family shape.
                    raise _challenge_code_invalid()
                mobile_outcome = await self._mobile_login.verify_code(
                    db,
                    phone=value,
                    code=code,
                    device_cookie=device_cookie,
                    remember_device=remember_device,
                    audit_context=audit_context,
                )
                return self._from_mobile(mobile_outcome, role=row.role if row else None)
            staff_outcome = await self._mobile_login.verify_staff_code(
                db,
                phone=value,
                code=code,
                device_cookie=device_cookie,
                remember_device=remember_device,
                audit_context=audit_context,
            )
            return self._from_staff_mobile(staff_outcome)
        except BusinessError as exc:
            raise self._map_code_family(exc) from exc

    async def _verify_on_cookie_alone(
        self, db: AsyncSession, *, device_cookie: str | None
    ) -> UnifiedOutcome:
        """The empty-body leg: the remembered cookie is the single
        factor. A STUDENT/TEACHER cookie re-opens its session; an ADMIN
        still owes the totp leg — the cookie never exempts a factor on
        the code legs (the free-TOTP semantics live on the password
        leg's honoring only)."""
        user_id: UUID | None = None
        if device_cookie is not None:
            user_id = await device_credentials.validate(
                db, device_cookie, clock=self._clock
            )
        if user_id is None:
            raise _challenge_code_invalid()
        user = await db.get(User, user_id)
        # The SAME audience the probe names (#87): ACTIVE and
        # PENDING_APPROVAL students alike — a PENDING student logs in
        # and browses the frozen home (RA3), so a banner that invites
        # and then refuses is a dead end (the #115 runtime finding).
        if user is None or user.status not in (
            UserStatus.ACTIVE.value,
            UserStatus.PENDING_APPROVAL.value,
        ):
            raise _challenge_code_invalid()
        if user.role == Role.ADMIN.value:
            pending = await self._mobile_login.mint_staff_pending(user.id)
            return UnifiedOutcome(
                status="totp_required", user_id=user.id, pending_token=pending
            )
        _, tokens = await self._sessions.issue_session(
            db, user=user, now=self._clock.now()
        )
        await db.commit()
        return UnifiedOutcome(
            status="authenticated", role=user.role, user_id=user.id, tokens=tokens
        )

    # --- totp -------------------------------------------------------------

    async def verify_totp(
        self,
        db: AsyncSession,
        *,
        pending_token: str,
        code: str,
        remember_device: bool,
        audit_context: AuditContext | None,
    ) -> UnifiedOutcome:
        """One endpoint, two pending domains: try the email-login
        domain first — its GETDEL miss burns nothing — then the
        staff-mobile domain. Both complete through their own services
        (craft untouched); anything else is the single invalid
        envelope."""
        tokens: SessionTokens | None = None
        device_cookie: str | None = None
        user_id: UUID | None = None
        try:
            outcome = await self._email_login.verify_totp(
                db,
                pending_token=pending_token,
                code=code,
                remember_device=remember_device,
                audit_context=audit_context,
            )
            assert outcome.tokens is not None
            tokens, device_cookie, user_id = (
                outcome.tokens,
                outcome.device_cookie,
                outcome.user_id,
            )
        except BusinessError as exc:
            # The email-domain miss answers its uniform 401
            # AUTHENTICATION_REQUIRED (unknown/expired/consumed pending)
            # or a code-family envelope — either way the credential is
            # NOT this domain's, or the code was wrong; the staff-mobile
            # probe below burns nothing on a miss.
            if _code_of(exc) not in _CODE_FAMILY and exc.code is not (
                ErrorCode.AUTHENTICATION_REQUIRED
            ):
                raise
            try:
                (
                    staff_tokens,
                    staff_cookie,
                ) = await self._mobile_login.complete_staff_totp(
                    db,
                    pending_token=pending_token,
                    code=code,
                    remember_device=remember_device,
                    audit_context=audit_context,
                )
                tokens, device_cookie = staff_tokens, staff_cookie
            except BusinessError as mobile_exc:
                raise self._map_code_family(mobile_exc) from mobile_exc
        assert tokens is not None
        return UnifiedOutcome(
            status="authenticated",
            role=tokens.role,
            user_id=user_id,
            tokens=tokens,
            device_cookie=device_cookie,
        )

    # --- the password form --------------------------------------------------

    async def login(
        self,
        db: AsyncSession,
        *,
        identifier: str,
        password: str,
        device_cookie: str | None,
        remember_device: bool,
    ) -> UnifiedOutcome:
        kind, value, row = await self._resolve(db, identifier)
        if kind == "phone" and row is None:
            # #73's phone-first-THEN-number fallback (the owner-account
            # bug, 2026-10-23: 10-digit student numbers parse as valid
            # CN landlines): the row-less phone arm re-tries the raw
            # value as a student number. login_student's own uniform
            # failure covers an actually-unregistered number — no new
            # oracle, no role leak (staff usernames are emails).
            tokens = await self._sessions.login_student(
                db, identifier.strip(), password
            )
            return UnifiedOutcome(
                status="authenticated",
                role=Role.STUDENT.value,
                tokens=tokens,
            )
        if kind == "phone":
            # The row-less arm returned through the #73 fallback above.
            assert row is not None
            if row.role == Role.STUDENT.value:
                tokens = await self._sessions.login_student(db, row.username, password)
                return UnifiedOutcome(
                    status="authenticated",
                    role=Role.STUDENT.value,
                    user_id=row.id,
                    tokens=tokens,
                )
            # A staff-bound phone reaches the staff password flow by
            # its account identifier.
            value = row.email_normalized or row.username
        elif kind == "student_number":
            tokens = await self._sessions.login_student(db, value, password)
            # The student flow by definition (spec §3.2: 学号=学生专属
            # 标识) — login_student's pair carries no role claim.
            return UnifiedOutcome(
                status="authenticated", role=Role.STUDENT.value, tokens=tokens
            )

        staff_tokens, cookie = await self._staff.authenticate_staff(
            db,
            value,
            password,
            "",
            device_cookie=device_cookie,
            remember_device=remember_device,
            second_factor_policy="unified",
        )
        if staff_tokens is None:
            # Password proven, ADMIN second factor owed: mint the
            # pending credential, answer the totp_required leg.
            row = await self._users.find_by_username(db, value.strip().lower())
            assert row is not None
            pending = await self._mobile_login.mint_staff_pending(row.id)
            return UnifiedOutcome(
                status="totp_required", user_id=row.id, pending_token=pending
            )
        return UnifiedOutcome(
            status="authenticated",
            role=staff_tokens.role,
            tokens=staff_tokens,
            device_cookie=cookie,
        )

    # --- helpers -----------------------------------------------------------

    def _map_code_family(self, exc: BusinessError) -> BusinessError:
        if _code_of(exc) in _CODE_FAMILY:
            return _challenge_code_invalid()
        return exc

    def _from_email(self, outcome: EmailCodeLoginOutcome) -> UnifiedOutcome:
        if outcome.pending_token is not None:
            return UnifiedOutcome(
                status="totp_required",
                user_id=outcome.user_id,
                pending_token=outcome.pending_token,
            )
        assert outcome.tokens is not None
        return UnifiedOutcome(
            status="authenticated",
            role=outcome.role.value,
            user_id=outcome.user_id,
            tokens=outcome.tokens,
            device_cookie=outcome.device_cookie,
        )

    def _from_mobile(
        self, outcome: MobileVerifyOutcome, *, role: str | None
    ) -> UnifiedOutcome:
        if outcome.unregistered:
            assert outcome.register_token is not None
            return UnifiedOutcome(
                status="unregistered", register_token=outcome.register_token
            )
        assert outcome.tokens is not None
        return UnifiedOutcome(
            status="authenticated",
            role=role if role is not None else outcome.tokens.role,
            tokens=outcome.tokens,
            device_cookie=outcome.device_cookie,
        )

    def _from_staff_mobile(self, outcome: StaffMobileCodeOutcome) -> UnifiedOutcome:
        if outcome.pending_token is not None:
            return UnifiedOutcome(
                status="totp_required", pending_token=outcome.pending_token
            )
        assert outcome.tokens is not None
        return UnifiedOutcome(
            status="authenticated",
            role=outcome.tokens.role,
            tokens=outcome.tokens,
            device_cookie=outcome.device_cookie,
        )
