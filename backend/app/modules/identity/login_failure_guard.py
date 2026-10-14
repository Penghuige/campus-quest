# backend/app/modules/identity/login_failure_guard.py
"""Per-account login failure lockout guard (V1.1 §6, Wave 1).

5 consecutive failures per account -> 15-minute cooldown, shared by the
student password path, the staff password+TOTP path, and the email-code
path (V1.1 §6: the IP-scoped limiter cannot stop a distributed online
guess against ONE account; this counter is the account-scoped answer).

Design decisions:

- **One Redis counter per user** (``login:fail:{user_id}``): INCR on
  every credential mismatch, DEL on success. The threshold compare is
  client-side in `is_locked` — exactly one honest comparison, no
  scripted atomicity needed, because reaching 5 one increment early or
  late changes nothing observable (the window is 15 minutes either
  way).
- **EXPIRE only on the first INCR** (count == 1): the 900s window is
  anchored at the FIRST failure of a burst. Re-arming on every hit
  would let a slow drip — one wrong password every few minutes — hold
  the account locked forever while never letting the counter's TTL
  lapse. With the anchor, the lock always dissolves no later than 15
  minutes after the first failure.
- **Rate-limit rejections never count.** A 429 from the IP/email
  limiter is traffic shaping, not a credential verdict (Review Focus
  4); callers record failures only on genuine password/TOTP/recovery/
  email-code mismatches.
- `retry_after_seconds` reads the key TTL so the ``ACCOUNT_LOCKED``
  envelope can carry the remaining cooldown without the caller doing
  Redis arithmetic.
"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis

LOCK_THRESHOLD = 5
LOCK_TTL_SECONDS = 900
_KEY_PREFIX = "login:fail:"


class LoginFailureGuard:
    """Redis-backed consecutive-failure counter; see module docstring."""

    def __init__(self, *, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def is_locked(self, user_id: UUID) -> bool:
        """True once the consecutive-failure count reached the threshold."""
        raw = await self._redis.get(_KEY_PREFIX + str(user_id))
        if raw is None:
            return False
        value = raw.decode() if isinstance(raw, bytes) else raw
        return int(value) >= LOCK_THRESHOLD

    async def record_failure(self, user_id: UUID) -> None:
        """Count one genuine credential mismatch (never a 429)."""
        key = _KEY_PREFIX + str(user_id)
        count = int(await self._redis.incr(key))
        if count == 1:
            await self._redis.expire(key, LOCK_TTL_SECONDS)

    async def reset(self, user_id: UUID) -> None:
        """Successful login: clear the streak (V1.1 §6 成功登录清零)."""
        await self._redis.delete(_KEY_PREFIX + str(user_id))

    async def retry_after_seconds(self, user_id: UUID) -> int | None:
        """Remaining cooldown seconds while locked, else None.

        Returns a clamped positive value: a key whose TTL already lapsed
        answers -1/-2 from Redis, which no envelope should surface as a
        retry hint.
        """
        raw = await self._redis.get(_KEY_PREFIX + str(user_id))
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes) else raw
        if int(value) < LOCK_THRESHOLD:
            return None
        ttl = await self._redis.ttl(_KEY_PREFIX + str(user_id))
        if ttl <= 0:
            # Lapsed between the GET above and now; the lock is over.
            return None
        return ttl
