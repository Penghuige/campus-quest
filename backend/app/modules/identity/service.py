# backend/app/modules/identity/service.py
"""Identity use cases (spec §5; backend-engineering §4-7).

``IdentityService.register_student`` owns the registration transaction:
validation, whitelist re-check, phone-uniqueness pre-check, and the User
insert all run inside one transaction whose final authority is the
database constraints — spec §5.4 并发注册最终必须由数据库约束兜底. The
pre-checks exist to produce friendly errors on the sequential path; the
unique indexes adjudicate every race that slips between check and insert
(backend-engineering §6-7).
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.ports import PasswordHasher, PhoneVerificationPort
from app.modules.identity.repository import StudentWhitelistRepository, UserRepository
from app.modules.identity.schemas import RegisterStudent
from app.modules.identity.validation import normalize_nickname, validate_student_number

_USERNAME_UNIQUE_CONSTRAINT = "uq_users_username"
_PHONE_UNIQUE_CONSTRAINT = "uq_users_phone_e164"

_VALIDATION_MESSAGE = "注册信息校验失败"
_PHONE_ALREADY_BOUND_MESSAGE = "该手机号已绑定其他账号"
_USERNAME_ALREADY_EXISTS_MESSAGE = "该学号已注册，请直接登录"


class IdentityService:
    """Registration and account lifecycle use cases (spec §5)."""

    def __init__(
        self,
        *,
        password_hasher: PasswordHasher,
        phone_verification: PhoneVerificationPort,
    ) -> None:
        # Ports are constructor-injected so Task 5 (Argon2id hashing) and
        # Task 4 (OTP challenge lifecycle) plug in without touching this
        # transaction; tests inject deterministic fakes (Task 3 seams).
        self._hash_password = password_hasher
        self._phone_verification = phone_verification
        self._whitelist = StudentWhitelistRepository()
        self._users = UserRepository()

    async def register_student(
        self, session: AsyncSession, command: RegisterStudent
    ) -> User:
        """Register one whitelisted student atomically (spec §5.1-5.4).

        Ordering is load-bearing: pure validation runs before the phone
        token is resolved, so a malformed request never burns a single-use
        token (spec §33.2). The whitelist entry is then re-checked with a
        row lock inside this transaction, the phone gets its friendly
        uniqueness check, and the insert flushes inside the transaction so
        ``uq_users_username`` / ``uq_users_phone_e164`` decide any race.

        The account is created directly in ACTIVE status: the phone was
        verified before registration by design (OTP verification precedes
        account creation), so there is no PENDING_PHONE transit.
        """
        student_number = self._validated_student_number(command.student_number)
        nickname = self._validated_nickname(command.nickname)
        self._require_usable_password(command.password)

        verified_phone = await self._phone_verification.verify_phone_token(
            command.phone_token
        )

        await self._whitelist.require_enabled(session, student_number)

        if await self._users.find_by_phone_e164(session, verified_phone.phone_e164):
            raise BusinessError(
                ErrorCode.PHONE_ALREADY_BOUND,
                _PHONE_ALREADY_BOUND_MESSAGE,
                status_code=409,
            )

        user = User(
            username=student_number,
            password_hash=self._hash_password(command.password),
            nickname=nickname,
            phone_e164=verified_phone.phone_e164,
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            # Only the two expected unique violations become business
            # conflicts; anything else is an unknown database failure and
            # must surface as itself (backend-engineering §7).
            await session.rollback()
            violation = str(exc.orig) if exc.orig is not None else str(exc)
            if _USERNAME_UNIQUE_CONSTRAINT in violation:
                raise BusinessError(
                    ErrorCode.USERNAME_ALREADY_EXISTS,
                    _USERNAME_ALREADY_EXISTS_MESSAGE,
                    status_code=409,
                ) from exc
            if _PHONE_UNIQUE_CONSTRAINT in violation:
                raise BusinessError(
                    ErrorCode.PHONE_ALREADY_BOUND,
                    _PHONE_ALREADY_BOUND_MESSAGE,
                    status_code=409,
                ) from exc
            raise
        await session.commit()
        return user

    @staticmethod
    def _validated_student_number(value: str) -> str:
        try:
            return validate_student_number(value)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "student_number", "reason": str(exc)},
            ) from exc

    @staticmethod
    def _validated_nickname(value: str) -> str:
        try:
            return normalize_nickname(value)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "nickname", "reason": str(exc)},
            ) from exc

    @staticmethod
    def _require_usable_password(password: str) -> None:
        """Reject the obviously-unusable; full policy is Task 5's (§5.6).

        Spec §5.6 recommends 10-128 with no composition rules — enforcing
        the band belongs with the Argon2id implementation so policy and
        hashing ship as one reviewed unit. Here only emptiness is guarded.
        """
        if not password:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "password", "reason": "password must not be empty"},
            )
