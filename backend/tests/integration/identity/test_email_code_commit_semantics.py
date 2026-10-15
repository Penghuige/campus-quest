# backend/tests/integration/identity/test_email_code_commit_semantics.py
"""request_code's COMMIT boundary under PRODUCTION session semantics (hotfix).

The rollback-harness suites never caught the missing commit: their
savepoint-mode session makes a flushed row visible to every assertion in
the same session, and the API suites' verify leg shares that connection
(an uncommitted row reads back fine on the SAME connection). Production
is different: ``get_db_session`` never commits and rolls back in its
``finally`` — a request-code 202 that ends without a service-level
commit silently discards the challenge row, and the whole email-code
login dies against a real client (the frontend T12 e2e caught it).

These tests therefore run the service against a PRODUCTION-shaped
session (own engine, own transaction) and assert from a FRESH
connection: committed rows are visible, no-op mailboxes leave none.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.modules.identity.email_login_service import EmailLoginService
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import LoginEmailChallenge, User
from tests.fakes.integrations import FakeEmailSender

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_TEACHER_EMAIL = "commit-teacher@campus.example.edu"
_UNKNOWN_EMAIL = "commit-unknown@campus.example.edu"


class InMemoryRedis:
    """The request-leg command subset: SET NX EX / GET / DELETE."""

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


@pytest.fixture
async def prod_engine():
    """A production-shaped engine: no harness transaction anywhere."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield engine
    # Real rows land in this suite (every service commit is a true
    # commit); sweep them so nothing leaks into other suites.
    async with AsyncSession(engine) as cleaner:
        await cleaner.execute(
            delete(LoginEmailChallenge).where(
                LoginEmailChallenge.email_normalized.in_(
                    (_TEACHER_EMAIL, _UNKNOWN_EMAIL)
                )
            )
        )
        await cleaner.execute(
            delete(User).where(User.username.in_((_TEACHER_EMAIL, _UNKNOWN_EMAIL)))
        )
        await cleaner.commit()
    await engine.dispose()


async def _seed_teacher(engine) -> None:
    async with AsyncSession(engine) as session:
        session.add(
            User(
                username=_TEACHER_EMAIL,
                password_hash=None,
                nickname="commit",
                email_normalized=_TEACHER_EMAIL,
                role=Role.TEACHER,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()


def _service(sender: FakeEmailSender) -> EmailLoginService:
    return EmailLoginService(
        sender=sender,
        redis=InMemoryRedis(),
        clock=FrozenClock(_T0),
        hmac_secret=get_settings().otp_hmac_secret,
    )


async def test_request_code_row_survives_a_fresh_connection(
    prod_engine,
) -> None:
    """The 202's whole point: the challenge row must exist for the NEXT
    request (a different connection) to verify against."""
    await _seed_teacher(prod_engine)
    sender = FakeEmailSender()

    async with AsyncSession(prod_engine) as prod_session:
        await _service(sender).request_code(prod_session, _TEACHER_EMAIL)

    async with AsyncSession(prod_engine) as reader:
        row = await reader.scalar(
            select(LoginEmailChallenge).where(
                LoginEmailChallenge.email_normalized == _TEACHER_EMAIL
            )
        )
    assert row is not None, (
        "the challenge row vanished with the request session — request_code "
        "must commit before the 202 returns (get_db_session rolls back)"
    )
    assert row.attempts == 0


async def test_noop_mailbox_leaves_no_row_across_connections(
    prod_engine,
) -> None:
    """Anti-enumeration regression under the same lens: an unknown
    mailbox's no-op commits NOTHING — no row, no trace."""
    sender = FakeEmailSender()

    async with AsyncSession(prod_engine) as prod_session:
        await _service(sender).request_code(prod_session, _UNKNOWN_EMAIL)

    async with AsyncSession(prod_engine) as reader:
        rows = (
            await reader.scalars(
                select(LoginEmailChallenge).where(
                    LoginEmailChallenge.email_normalized == _UNKNOWN_EMAIL
                )
            )
        ).all()
    assert rows == []
    assert sender.messages == []


async def test_send_failure_leaves_no_row_across_connections(
    prod_engine,
) -> None:
    """The failure leg's cleanup must survive the commit fix: a provider
    failure answers 502 and leaves NOTHING committed."""
    from app.core.errors import BusinessError
    from app.integrations.errors import PermanentProviderError

    await _seed_teacher(prod_engine)
    sender = FakeEmailSender()
    sender.failures.append(PermanentProviderError("boom"))
    redis = InMemoryRedis()

    async with AsyncSession(prod_engine) as prod_session:
        with pytest.raises(BusinessError) as error_info:
            await EmailLoginService(
                sender=sender,
                redis=redis,
                clock=FrozenClock(_T0),
                hmac_secret=get_settings().otp_hmac_secret,
            ).request_code(prod_session, _TEACHER_EMAIL)
    assert error_info.value.status_code == 502

    async with AsyncSession(prod_engine) as reader:
        rows = (
            await reader.scalars(
                select(LoginEmailChallenge).where(
                    LoginEmailChallenge.email_normalized == _TEACHER_EMAIL
                )
            )
        ).all()
    assert rows == []
    # The cooldown released: the recipient's retry is clean.
    assert redis.strings == {}
