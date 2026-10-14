# backend/tests/unit/identity/test_login_failure_guard.py
"""Unit tests for the per-account login failure lockout guard (V1.1 §6).

Redis-fake choice mirrors tests/unit/identity/test_otp.py: a minimal
in-memory async fake of the exact command subset (INCR / EXPIRE / GET /
TTL / DEL), zero new dependencies. Key expiry is not emulated — the
guard's window discipline is asserted on the fake's recorded TTL book
(the expire call count), and TTL lapse is simulated by dropping the key
the way real Redis would after 900s.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.identity.login_failure_guard import (
    LOCK_THRESHOLD,
    LOCK_TTL_SECONDS,
    LoginFailureGuard,
)


class InMemoryRedis:
    """Minimal async fake: INCR/EXPIRE/GET/TTL/DEL with call recording."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def incr(self, key: str) -> int:
        current = int(self.strings.get(key, "0")) + 1
        self.strings[key] = str(current)
        return current

    async def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        if key not in self.strings:
            return False
        self.expire_calls.append((key, seconds))
        self.ttls[key] = seconds
        return True

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def ttl(self, key: str) -> int:
        if key not in self.strings:
            return -2
        return self.ttls.get(key, -1)

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.strings:
                deleted += 1
            self.strings.pop(key, None)
            self.ttls.pop(key, None)
        return deleted


async def test_four_failures_do_not_lock() -> None:
    guard = LoginFailureGuard(redis=InMemoryRedis())
    user_id = uuid4()

    for _ in range(LOCK_THRESHOLD - 1):
        await guard.record_failure(user_id)

    assert await guard.is_locked(user_id) is False


async def test_fifth_failure_locks_with_retry_window() -> None:
    guard = LoginFailureGuard(redis=InMemoryRedis())
    user_id = uuid4()

    for _ in range(LOCK_THRESHOLD):
        await guard.record_failure(user_id)

    assert await guard.is_locked(user_id) is True
    retry_after = await guard.retry_after_seconds(user_id)
    assert retry_after is not None
    assert 0 < retry_after <= LOCK_TTL_SECONDS


async def test_lock_lapses_after_ttl() -> None:
    redis = InMemoryRedis()
    guard = LoginFailureGuard(redis=redis)
    user_id = uuid4()

    for _ in range(LOCK_THRESHOLD):
        await guard.record_failure(user_id)
    assert await guard.is_locked(user_id) is True

    # Simulate real Redis deleting the key when its 900s TTL fires.
    redis.strings.pop(f"login:fail:{user_id}", None)
    redis.ttls.pop(f"login:fail:{user_id}", None)

    assert await guard.is_locked(user_id) is False


async def test_expire_set_once_on_first_failure_only() -> None:
    """The window is anchored at the FIRST failure: a slow drip must not
    be able to extend the TTL forever by failing again inside the window."""
    redis = InMemoryRedis()
    guard = LoginFailureGuard(redis=redis)
    user_id = uuid4()

    for _ in range(LOCK_THRESHOLD + 2):
        await guard.record_failure(user_id)

    assert len(redis.expire_calls) == 1
    assert redis.expire_calls[0] == (f"login:fail:{user_id}", LOCK_TTL_SECONDS)


async def test_reset_clears_the_count_early() -> None:
    redis = InMemoryRedis()
    guard = LoginFailureGuard(redis=redis)
    user_id = uuid4()

    for _ in range(LOCK_THRESHOLD - 1):
        await guard.record_failure(user_id)
    await guard.reset(user_id)
    # A fresh window: two more failures stay below the threshold.
    for _ in range(2):
        await guard.record_failure(user_id)

    assert await guard.is_locked(user_id) is False
    # reset on a never-failing account is a no-op, not an error.
    await guard.reset(uuid4())


async def test_accounts_are_independent() -> None:
    guard = LoginFailureGuard(redis=InMemoryRedis())
    locked_user, other_user = uuid4(), uuid4()

    for _ in range(LOCK_THRESHOLD):
        await guard.record_failure(locked_user)
    await guard.record_failure(other_user)

    assert await guard.is_locked(locked_user) is True
    assert await guard.is_locked(other_user) is False


async def test_retry_after_seconds_none_when_not_locked() -> None:
    guard = LoginFailureGuard(redis=InMemoryRedis())
    user_id = uuid4()

    assert await guard.retry_after_seconds(user_id) is None


@pytest.mark.parametrize("count", [0, 1, 4])
async def test_is_locked_boundaries(count: int) -> None:
    guard = LoginFailureGuard(redis=InMemoryRedis())
    user_id = uuid4()

    for _ in range(count):
        await guard.record_failure(user_id)

    assert await guard.is_locked(user_id) is (count >= LOCK_THRESHOLD)
