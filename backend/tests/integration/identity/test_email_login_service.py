# backend/tests/integration/identity/test_email_login_service.py
"""EmailLoginService.request_code unit-of-behavior tests (V1.1 §2.1).

Integration (real PostgreSQL rollback harness) because the challenge is
a table row; Redis is the in-memory fake from the OTP unit-test pattern
(the command subset here is SET NX EX / GET / DEL) and the sender is
the shared FakeEmailSender — no live SMTP, no real Redis, FrozenClock
for the 5-minute expiry.

Anti-enumeration is asserted at this layer too: unknown, student, and
inactive mailboxes arm the SAME cooldown key as a real teacher, so the
request endpoint's 429-vs-202 shape cannot distinguish them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import hmac_challenge_code
from app.integrations.errors import PermanentProviderError
from app.modules.identity.email_login_service import (
    CODE_TTL_MINUTES,
    COOLDOWN_SECONDS,
    EmailLoginService,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import LoginEmailChallenge, User
from tests.fakes.integrations import FakeEmailSender

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 10, 15, 9, 0, 0, tzinfo=UTC)
_TEACHER_EMAIL = "wave1-teacher@campus.example.edu"


class InMemoryRedis:
    """Minimal async fake: SET NX EX / GET / DEL."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.strings:
                deleted += 1
            self.strings.pop(key, None)
        return deleted


async def _seed_user(
    db: AsyncSession,
    username: str,
    *,
    role: Role,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        username=username,
        password_hash=None,
        nickname=username.split("@")[0],
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    return user


def _service(
    sender: FakeEmailSender, redis: InMemoryRedis, clock: FrozenClock
) -> EmailLoginService:
    # request_code needs no verify-leg dependencies; they stay unset.
    # The keyed-digest secret mirrors the composition root (settings).
    return EmailLoginService(
        sender=sender,
        redis=redis,
        clock=clock,
        hmac_secret=get_settings().otp_hmac_secret,
    )


async def test_request_code_stores_hashed_challenge_and_sends(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session, _TEACHER_EMAIL, role=Role.TEACHER)
    sender = FakeEmailSender()
    clock = FrozenClock(_T0)
    service = _service(sender, InMemoryRedis(), clock)

    await service.request_code(db_session, _TEACHER_EMAIL)

    challenge = (await db_session.execute(select(LoginEmailChallenge))).scalar_one()
    sent = sender.messages[0]
    assert sent.to == _TEACHER_EMAIL
    assert sent.template == "staff_login_code"
    assert sent.variables["ttl_minutes"] == str(CODE_TTL_MINUTES)
    # A KEYED digest, never a bare SHA-256: a database dump must not be
    # brute-forceable over the 10^6 code space (the reviewer's ruling —
    # the SMS OTP discipline, shared hmac_challenge_code primitive).
    secret = get_settings().otp_hmac_secret
    assert challenge.code_hash == hmac_challenge_code(
        secret, str(sent.variables["code"])
    )
    assert (
        challenge.code_hash != sha256(str(sent.variables["code"]).encode()).hexdigest()
    )
    assert challenge.attempts == 0
    assert challenge.expires_at == _T0 + timedelta(minutes=CODE_TTL_MINUTES)
    # Six digits, zero-padded.
    assert str(sent.variables["code"]).isdigit()
    assert len(str(sent.variables["code"])) == 6


async def test_request_code_cooldown_raises_429(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session, _TEACHER_EMAIL, role=Role.TEACHER)
    sender = FakeEmailSender()
    service = _service(sender, InMemoryRedis(), FrozenClock(_T0))
    await service.request_code(db_session, _TEACHER_EMAIL)
    assert len(sender.messages) == 1

    with pytest.raises(BusinessError) as error_info:
        await service.request_code(db_session, _TEACHER_EMAIL)

    assert error_info.value.code == ErrorCode.RATE_LIMITED
    assert error_info.value.status_code == 429
    # The rejected retry neither re-sent nor stored a second challenge.
    assert len(sender.messages) == 1


async def test_request_code_cooldown_is_armed_for_any_email(
    db_session: AsyncSession,
) -> None:
    """Anti-enumeration: unknown/student mailboxes trip the SAME 60s
    cooldown — the second request for them 429s exactly like a real
    teacher's would, so the endpoint's observable shape does not reveal
    which mailbox is staff."""
    sender = FakeEmailSender()
    service = _service(sender, InMemoryRedis(), FrozenClock(_T0))
    unknown = "nobody@campus.example.edu"

    await service.request_code(db_session, unknown)  # no-op, no raise
    with pytest.raises(BusinessError) as error_info:
        await service.request_code(db_session, unknown)

    assert error_info.value.status_code == 429


@pytest.mark.parametrize(
    ("seed", "role", "status"),
    [
        (False, Role.TEACHER, UserStatus.ACTIVE),  # unknown mailbox
        (True, Role.STUDENT, UserStatus.ACTIVE),
        (True, Role.TEACHER, UserStatus.SUSPENDED),
    ],
)
async def test_request_code_noop_for_non_staff_or_inactive_mailboxes(
    db_session: AsyncSession,
    seed: bool,
    role: Role,
    status: UserStatus,
) -> None:
    username = "probe@campus.example.edu"
    if seed:
        await _seed_user(db_session, username, role=role, status=status)
    sender = FakeEmailSender()
    service = _service(sender, InMemoryRedis(), FrozenClock(_T0))

    await service.request_code(db_session, username)

    assert sender.messages == []
    assert (
        await db_session.execute(select(LoginEmailChallenge))
    ).scalar_one_or_none() is None


async def test_request_code_send_failure_raises_502_and_cleans_up(
    db_session: AsyncSession,
) -> None:
    """A provider failure surfaces 502 with this request's traces gone:
    the challenge row is removed and the cooldown is released, so the
    user's immediate retry is a clean re-request (the code never reached
    any mailbox — keeping a dead challenge and a 60s penalty would only
    punish the recipient for the provider's failure)."""
    await _seed_user(db_session, _TEACHER_EMAIL, role=Role.TEACHER)
    sender = FakeEmailSender()
    sender.failures.append(PermanentProviderError("boom"))
    redis = InMemoryRedis()
    service = _service(sender, redis, FrozenClock(_T0))

    with pytest.raises(BusinessError) as error_info:
        await service.request_code(db_session, _TEACHER_EMAIL)

    assert error_info.value.code == ErrorCode.EMAIL_DELIVERY_FAILED
    assert error_info.value.status_code == 502
    assert (
        await db_session.execute(select(LoginEmailChallenge))
    ).scalar_one_or_none() is None
    assert redis.strings == {}


async def test_request_code_normalizes_case_and_whitespace(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session, _TEACHER_EMAIL, role=Role.TEACHER)
    sender = FakeEmailSender()
    service = _service(sender, InMemoryRedis(), FrozenClock(_T0))

    await service.request_code(db_session, f"  {_TEACHER_EMAIL.upper()}  ")

    assert sender.messages[0].to == _TEACHER_EMAIL
    challenge = (await db_session.execute(select(LoginEmailChallenge))).scalar_one()
    assert challenge.email_normalized == _TEACHER_EMAIL


async def test_request_code_replaces_the_previous_challenge(
    db_session: AsyncSession,
) -> None:
    """Cooldown elapsed: a new request supersedes the old row — one live
    challenge per mailbox, so verify never has to pick between them."""
    await _seed_user(db_session, _TEACHER_EMAIL, role=Role.TEACHER)
    sender = FakeEmailSender()
    service = _service(sender, InMemoryRedis(), FrozenClock(_T0))
    await service.request_code(db_session, _TEACHER_EMAIL)

    # 61s later the cooldown has lapsed; a fresh request is accepted.
    clock = FrozenClock(_T0 + timedelta(seconds=COOLDOWN_SECONDS + 1))
    service = _service(sender, InMemoryRedis(), clock)
    await service.request_code(db_session, _TEACHER_EMAIL)

    rows = (await db_session.execute(select(LoginEmailChallenge))).scalars().all()
    assert len(rows) == 1
    assert rows[0].created_at is not None


async def test_email_code_probe_recovers_the_mailed_code(
    db_session: AsyncSession,
) -> None:
    """The e2e recovery channel (tests/e2e/email_code_probe.py): the
    probe rediscovers the code from the stored digest alone — the same
    honesty contract as otp_probe, proven against a real issued row."""
    from tests.e2e.email_code_probe import recover_email_code

    await _seed_user(db_session, _TEACHER_EMAIL, role=Role.TEACHER)
    sender = FakeEmailSender()
    service = _service(sender, InMemoryRedis(), FrozenClock(_T0))
    await service.request_code(db_session, _TEACHER_EMAIL)
    mailed = str(sender.messages[0].variables["code"])

    challenge_id, recovered = await recover_email_code(db_session, _TEACHER_EMAIL)

    assert recovered == mailed
    assert challenge_id is not None

    with pytest.raises(LookupError):
        await recover_email_code(db_session, "never-requested@campus.example.edu")
