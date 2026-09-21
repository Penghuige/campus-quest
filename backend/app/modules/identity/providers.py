# backend/app/modules/identity/providers.py
"""The identity module's composition root (backend-engineering §3, §11).

Single responsibility: the FastAPI provider dependencies that assemble
the module's services per request. Routes resolve everything through
``Depends`` on these functions — never by calling them directly — so
tests override a dependency, never service internals (§21).

- The Redis client is process-cached; services are assembled per request
  from injected clock, settings, codec, and sender dependencies. The
  SMS/EMAIL senders are resolved from `Settings.sms_provider` /
  `Settings.email_provider` (V1's only value is the logging adapter);
  production refuses the logging provider at Settings construction
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
from app.integrations.rate_limit import RateLimiter, RedisFixedWindowLimiter
from app.integrations.sms import SmsSender, build_sms_sender
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.email_verification import EmailVerificationService
from app.modules.identity.events import DomainEventPublisher, LoggingEventPublisher
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
    guard), so this wiring has no silent fallback onto a sender that
    delivers nothing. When real adapters land (the provider project),
    the Literal in config.py and `build_sms_sender` grow together.
    """
    return build_sms_sender(settings.sms_provider)


def get_email_sender(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EmailSender:
    """Resolve the email adapter from `settings.email_provider`.

    The fail-closed chain mirrors `get_sms_sender`.
    """
    return build_email_sender(settings.email_provider)


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
) -> SessionService:
    return SessionService(
        clock=clock,
        access_codec=codec,
        refresh_token_ttl_days=settings.refresh_token_ttl_days,
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
    )


def get_phone_region(
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    return settings.phone_default_region


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
OtpServiceDep = Annotated[OtpChallengeService, Depends(get_otp_service)]
SessionsDep = Annotated[SessionService, Depends(get_session_service)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
