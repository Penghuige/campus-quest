# backend/tests/e2e/email_code_probe.py
"""Test-side email login-code recovery (V1.1 Wave 1; the otp_probe twin).

The production ``EmailLoginService`` stores only a KEYED digest —
``HMAC(otp_hmac_secret, code)`` via the shared ``hmac_challenge_code``
primitive (core/security) — on the ``login_email_challenges`` row; the
plaintext code exists solely inside the ``EmailSender.send`` call, and
the dev-stack ``LoggingEmailSender`` deliberately never records it
(G11: never ``variables``). An e2e flow that must actually ANSWER the
emailed-code prompt therefore recovers the code the honest way — from
the challenge row the service itself persisted:

    read the newest challenge row for the mailbox -> brute-force the
    closed 6-digit space against the stored keyed digest, with the
    same settings secret (the exact arithmetic the otp_probe performs
    for the SMS HMAC; the secret is test-stack configuration, not a
    production credential).

Production behavior is untouched end to end (challenge insert, at-rest
keyed hashing, verify); only the mailbox is replaced by arithmetic.
The search is scoped to ONE mailbox's newest row, never the table.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hmac_challenge_code
from app.modules.identity.models import LoginEmailChallenge

_CODE_SPACE = 1_000_000


async def recover_email_code(
    db: AsyncSession, email_normalized: str
) -> tuple[UUID, str]:
    """Recover (challenge_id, code) for the mailbox's NEWEST challenge.

    Raises ``LookupError`` when no live row exists (the request leg
    never stored one, or the mailbox is not staff) — the caller treats
    that as a fixture bug, not a login failure.
    """
    challenge = (
        await db.scalars(
            select(LoginEmailChallenge)
            .where(LoginEmailChallenge.email_normalized == email_normalized)
            .order_by(LoginEmailChallenge.created_at.desc(), LoginEmailChallenge.id)
            .limit(1)
        )
    ).first()
    if challenge is None:
        raise LookupError(f"no login email challenge for {email_normalized!r}")

    secret = get_settings().otp_hmac_secret
    for candidate in range(_CODE_SPACE):
        code = f"{candidate:06d}"
        if hmac_challenge_code(secret, code) == challenge.code_hash:
            return challenge.id, code
    # Unreachable for rows this service wrote (closed space).
    raise LookupError(f"challenge {challenge.id} digest matches no 6-digit code")
