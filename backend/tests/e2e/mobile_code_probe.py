# backend/tests/e2e/mobile_code_probe.py
"""Test-side mobile login-code recovery (V1.1 auth-extension II §3).

The `email_code_probe` twin for the student SMS channel: the production
``MobileLoginService`` stores only the keyed digest
(``hmac_challenge_code`` over the settings secret) on the
``login_mobile_challenges`` row; the plaintext code exists solely inside
the ``SmsSender.send`` call. An e2e flow that must actually ANSWER the
SMS prompt recovers the code the honest way — from the challenge row the
service itself persisted, brute-forcing the closed 6-digit space against
the keyed digest with the same settings secret (the secret is test-stack
configuration). Production behavior is untouched; only the handset is
replaced by arithmetic.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hmac_challenge_code
from app.modules.identity.models import LoginMobileChallenge

_CODE_SPACE = 1_000_000


async def recover_mobile_code(db: AsyncSession, phone_e164: str) -> tuple[UUID, str]:
    """Recover (challenge_id, code) for the phone's NEWEST challenge.

    Raises ``LookupError`` when no row exists (the request leg never
    stored one — unknown phone, non-student, or the cooldown refused).
    """
    challenge = (
        await db.scalars(
            select(LoginMobileChallenge)
            .where(LoginMobileChallenge.phone_e164 == phone_e164)
            .order_by(
                LoginMobileChallenge.created_at.desc(),
                LoginMobileChallenge.id,
            )
            .limit(1)
        )
    ).first()
    if challenge is None:
        raise LookupError(f"no login mobile challenge for {phone_e164!r}")

    secret = get_settings().otp_hmac_secret
    for candidate in range(_CODE_SPACE):
        code = f"{candidate:06d}"
        if hmac_challenge_code(secret, code) == challenge.code_hash:
            return challenge.id, code
    # Unreachable for rows this service wrote (closed space).
    raise LookupError(f"challenge {challenge.id} digest matches no 6-digit code")
