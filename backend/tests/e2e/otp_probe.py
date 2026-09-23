# backend/tests/e2e/otp_probe.py
"""Test-side OTP recovery for real-Redis OTP flows (plan 10 task 2).

The production ``OtpChallengeService`` stores only
``HMAC-SHA256(hmac_secret, "{salt}:{code}")`` per challenge (spec §33.2):
the plaintext code exists solely inside the ``SmsSender.send`` call, and
the dev-stack ``LoggingSmsSender`` deliberately never records it. An e2e
flow that must actually ANSWER the SMS prompt therefore recovers the code
the honest way — from the challenge record the service itself persisted:

    read the real Redis hash -> brute-force the closed 6-digit space
    against the stored digest with the SAME settings hmac secret.

This is the "测试侧直读 OTP" option the task brief ruled on (the more
end-to-end of the two): the challenge creation, the at-rest hashing, the
Redis transport, and the verify call all stay production behavior; only
the mailbox is replaced by arithmetic. ~10^6 HMAC-SHA256 digests run in
about a second, and the search is scoped to ONE challenge record (the
latest for the phone), never the whole keyspace of the database.
"""

from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

import redis.asyncio as aioredis

from app.core.config import get_settings

_CODE_DIGITS = 6
_CHALLENGE_SCAN_PATTERN = "otp:challenge:*"


def _hmac_hex(secret: str, salt: str, value: str) -> str:
    return hmac.new(
        secret.encode(), f"{salt}:{value}".encode(), hashlib.sha256
    ).hexdigest()


def _decode(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def recover_otp_code(
    redis: aioredis.Redis, phone_e164: str, *, purpose: str = "REGISTER"
) -> tuple[UUID, str]:
    """Recover (challenge_id, code) for the LATEST live challenge of
    ``phone_e164`` with the given purpose.

    Raises ``LookupError`` when no challenge matches (the caller raced the
    send, or the record already expired) — a clear, fixable harness error
    rather than a silent wrong code.
    """
    secret = get_settings().otp_hmac_secret
    latest: tuple[float, UUID, str, str] | None = None
    async for key in redis.scan_iter(match=_CHALLENGE_SCAN_PATTERN, count=500):
        record = await redis.hgetall(key)
        if _decode(record.get("phone", "")) != phone_e164:
            continue
        if _decode(record.get("purpose", "")) != purpose:
            continue
        # The service stores created_at as a unix timestamp float; only
        # the ORDERING matters here (pick the newest challenge).
        created_ts = float(_decode(record["created_at"]))
        challenge_id = UUID(_decode(key).rsplit(":", 1)[-1])
        candidate = (
            created_ts,
            challenge_id,
            _decode(record["salt"]),
            _decode(record["code_hash"]),
        )
        if latest is None or candidate[0] > latest[0]:
            latest = candidate
    if latest is None:
        raise LookupError(f"no live OTP challenge for {phone_e164} (purpose {purpose})")
    _, challenge_id, salt, code_hash = latest
    for value in range(10**_CODE_DIGITS):
        code = f"{value:0{_CODE_DIGITS}d}"
        if hmac.compare_digest(_hmac_hex(secret, salt, code), code_hash):
            return challenge_id, code
    raise LookupError(
        f"no 6-digit code matches the stored digest for challenge "
        f"{challenge_id} — OTP_HMAC_SECRET differs between the API and "
        f"this probe"
    )


__all__ = ["recover_otp_code"]
