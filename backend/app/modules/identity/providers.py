# backend/app/modules/identity/providers.py
"""The identity module's composition root (backend-engineering §3, §11).

Single responsibility: the FastAPI provider dependencies that assemble
the module's services per request. Routes resolve everything through
``Depends`` on these functions — never by calling them directly — so
tests override a dependency, never service internals (§21).

- The Redis client is process-cached; services are assembled per request
  from injected clock, settings, codec, and sender dependencies. The
  SMS/EMAIL senders are resolved from `Settings.sms_provider` /
  `Settings.email_provider` (SMS: logging or aliyun_dypns; EMAIL: the
  logging adapter only); production refuses the logging provider at
  Settings construction
  (config.py's production guard — fail closed), so this wiring can never
  hand a no-send adapter to a production request. The logging adapters
  record masked deliveries (never ``variables`` — the OTP code and email
  token travel there) and return "logging:"-prefixed receipts so recorded
  deliveries are distinguishable from real provider sends.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.security import AccessTokenCodec, hash_password
from app.db.session import get_db_session
from app.integrations.email import EmailSender, build_email_sender
from app.integrations.object_storage import ObjectStorage
from app.integrations.rate_limit import RateLimiter, RedisFixedWindowLimiter
from app.integrations.sms import SmsSender, build_sms_sender
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.avatar_service import AvatarService
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.email_login_service import EmailLoginService
from app.modules.identity.email_verification import EmailVerificationService
from app.modules.identity.events import DomainEventPublisher, LoggingEventPublisher
from app.modules.identity.login_failure_guard import LoginFailureGuard
from app.modules.identity.mobile_login_service import MobileLoginService
from app.modules.identity.otp import OtpChallengeService, OtpPolicy
from app.modules.identity.profile_service import ProfileService
from app.modules.identity.service import IdentityService
from app.modules.identity.session_service import SessionService
from app.modules.identity.staff_service import StaffService


@lru_cache
def get_identity_redis() -> aioredis.Redis:
    """Process-wide Redis client for OTP/email/rate-limit state.

    ``decode_responses=True`` keeps replies as ``str`` (every consumer
    decodes anyway); the connection pool is shared per process. Services
    receive the client through ``Depends(get_identity_redis)`` — a direct
    call inside a provider would dodge ``dependency_overrides``, which is
    exactly the seam integration tests use to point at the flushed test
    database.
    """
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


RedisDep = Annotated[aioredis.Redis, Depends(get_identity_redis)]


def get_rate_limiter(
    clock: Annotated[Clock, Depends(get_business_clock)],
    redis: RedisDep,
) -> RateLimiter:
    return RedisFixedWindowLimiter(redis=redis, clock=clock)


def get_sms_sender(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SmsSender:
    """Resolve the SMS adapter from `settings.sms_provider`.

    The fail-closed chain (PR #2 hardening P0-2): production refuses
    provider="logging" at Settings construction (config.py's production
    guard), and the aliyun_dypns adapter refuses incomplete credentials
    at factory time in every environment — this wiring has no silent
    fallback onto a sender that delivers nothing or half-configures a
    real one.
    """
    return build_sms_sender(settings.sms_provider, settings)


def get_email_sender(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmailSender:
    """Resolve the email adapter from `settings.email_provider`.

    The fail-closed chain mirrors `get_sms_sender` — including the
    settings hand-off: without it the factory would refuse a real smtp
    selection at composition in EVERY environment (the #69 wiring
    lesson; providers must pass the deployment settings through).
    """
    return build_email_sender(settings.email_provider, settings)


def get_login_failure_guard(
    redis: RedisDep,
) -> LoginFailureGuard:
    """The per-account lockout counter on the module's shared Redis."""
    return LoginFailureGuard(redis=redis)


def get_email_login_service(
    sender: Annotated[EmailSender, Depends(get_email_sender)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: RedisDep,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    guard: Annotated[LoginFailureGuard, Depends(get_login_failure_guard)],
) -> EmailLoginService:
    return EmailLoginService(
        sender=sender,
        redis=redis,
        clock=clock,
        sessions=sessions,
        fernet=Fernet(settings.totp_encryption_key),
        guard=guard,
        hmac_secret=settings.otp_hmac_secret,
        resend_cooldown_seconds=(
            1
            if settings.e2e_login_ratelimit
            else settings.email_code_resend_cooldown_seconds
        ),
    )


def get_event_publisher() -> DomainEventPublisher:
    """Interim adapter; the audit/outbox module wires persistent dispatch
    (see the outbox contract in docs/architecture/interfaces.md)."""
    return LoggingEventPublisher()


def get_otp_service(
    sms_sender: Annotated[SmsSender, Depends(get_sms_sender)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: RedisDep,
) -> OtpChallengeService:
    return OtpChallengeService(
        redis=redis,
        clock=clock,
        sms_sender=sms_sender,
        policy=OtpPolicy.from_settings(settings),
    )


def get_session_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    settings: Annotated[Settings, Depends(get_settings)],
    guard: Annotated[LoginFailureGuard, Depends(get_login_failure_guard)],
) -> SessionService:
    return SessionService(
        clock=clock,
        access_codec=codec,
        refresh_token_ttl_days=settings.refresh_token_ttl_days,
        refresh_grace_seconds=settings.refresh_grace_seconds,
        # Dedicated key (settings validates it is a real Fernet key
        # whenever the window is on) — never the TOTP key, so either
        # secret can rotate without touching the other's ciphertexts.
        replay_crypt=Fernet(settings.refresh_replay_encryption_key.encode())
        if settings.refresh_grace_seconds > 0
        else None,
        # V1.1 §6: the per-account lockout rides the student login path.
        guard=guard,
    )


def get_identity_service(
    otp: Annotated[OtpChallengeService, Depends(get_otp_service)],
) -> IdentityService:
    return IdentityService(password_hasher=hash_password, phone_verification=otp)


def get_profile_service(
    otp: Annotated[OtpChallengeService, Depends(get_otp_service)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProfileService:
    return ProfileService(
        clock=clock,
        otp=otp,
        otp_policy=OtpPolicy.from_settings(settings),
        sessions=sessions,
        phone_default_region=settings.phone_default_region,
    )


def get_email_verification_service(
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: RedisDep,
) -> EmailVerificationService:
    return EmailVerificationService(
        clock=clock,
        email_sender=email_sender,
        redis=redis,
        token_ttl_hours=settings.email_verification_token_ttl_hours,
    )


def get_staff_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    events: Annotated[DomainEventPublisher, Depends(get_event_publisher)],
    guard: Annotated[LoginFailureGuard, Depends(get_login_failure_guard)],
) -> StaffService:
    # The notification recorder is the MERGE_CARRIES item 2 production
    # wiring for the identity-security producer: NotificationPort joins
    # the confirm-totp transaction so the ACCOUNT_SECURITY intent
    # commits with the credential flip or not at all (the outbox rule).
    # This composition root is the one identity layer allowed to see
    # the notifications module (the tasks-router precedent — the
    # dependency direction notifications -> identity holds for services,
    # not for the DI wiring).
    from app.modules.notifications.port import NotificationPort

    return StaffService(
        clock=clock,
        sessions=sessions,
        fernet=Fernet(settings.totp_encryption_key),
        events=events,
        invitation_ttl_hours=settings.staff_invitation_ttl_hours,
        notification_recorder=NotificationPort(clock=clock),
        # Explicit for the family style (the points/community/system
        # composition roots): the durable G12 writer for the staff
        # lifecycle actions (Plan 08 T2) — identical to the constructor
        # default, spelled out so the dependency is visible here.
        audit=AuditLogWriter(),
        # V1.1 §6: the staff password+TOTP path shares the lockout
        # counter (guard=None in the constructor would silently disable
        # it — the #69 wiring lesson).
        guard=guard,
    )


def get_phone_region(
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    return settings.phone_default_region


def get_object_storage() -> ObjectStorage:
    """Object-storage adapter factory — identity's own composition seam.

    The same production binding and rationale as the submissions
    factory (``submissions.router.get_object_storage``): S3 adapter
    from ``Settings``, fail-closed on misconfiguration, tests override
    with the in-memory fake through ``dependency_overrides``. Duplicated
    here rather than imported because identity is the lower-level
    module — importing submissions would invert the module arrows for
    one provider function.
    """
    from app.integrations.object_storage_s3 import S3ObjectStorage

    return S3ObjectStorage(get_settings())


def get_avatar_service(
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> AvatarService:
    # The stateless G12 writer, defaulted in the constructor like every
    # audited identity service (the account_admin_service wiring rule).
    return AvatarService(storage=storage)


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
OtpServiceDep = Annotated[OtpChallengeService, Depends(get_otp_service)]
SessionsDep = Annotated[SessionService, Depends(get_session_service)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
StorageDep = Annotated[ObjectStorage, Depends(get_object_storage)]


def get_mobile_login_service(
    sms_sender: Annotated[SmsSender, Depends(get_sms_sender)],
    clock: Annotated[Clock, Depends(get_business_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: RedisDep,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    guard: Annotated[LoginFailureGuard, Depends(get_login_failure_guard)],
) -> MobileLoginService:
    return MobileLoginService(
        sms_sender=sms_sender,
        redis=redis,
        clock=clock,
        sessions=sessions,
        guard=guard,
        hmac_secret=settings.otp_hmac_secret,
        # The staff channel's ADMIN totp leg decrypts the credential
        # (V1.1 login iteration item 1) — the same key the staff service
        # and the email login use.
        fernet=Fernet(settings.totp_encryption_key),
    )
