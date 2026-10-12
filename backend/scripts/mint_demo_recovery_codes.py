"""Mint one-time demo recovery codes for the seeded demo staff accounts.

Usage (from backend/, with the dev-stack environment exported —
DATABASE_URL pointing at the compose PostgreSQL, REDIS_URL, S3_*,
BUSINESS_TIMEZONE):
  uv run python scripts/mint_demo_recovery_codes.py

Why: staff login (spec §5.8) always requires a second factor — a current
TOTP code OR one unused recovery code (the documented lockout path,
`staff_service.authenticate_staff` accepts both in the same field, and
consuming a recovery code has no TOTP-rotation side effects). For
hands-on demoing without an authenticator app this script stocks static
one-time codes for the two seeded staff accounts.

Idempotent top-up: an account already holding >= MIN_UNUSED unused
codes is left alone (its remaining count is reported); otherwise codes
are added up to TARGET_UNUSED. Plaintext is printed exactly once —
only Argon2id hashes are stored.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.modules.identity.models import RecoveryCode, User
from app.modules.identity.totp import (
    generate_recovery_codes,
    hash_recovery_code,
)

STAFF_EMAILS = [
    "admin@campus.example.edu",
    "teacher@campus.example.edu",
]
MIN_UNUSED = 3
TARGET_UNUSED = 8


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        for email in STAFF_EMAILS:
            user = await session.scalar(
                select(User).where(User.email_normalized == email)
            )
            if user is None:
                raise SystemExit(f"missing demo staff account: {email}")
            unused = await session.scalar(
                select(func.count())
                .select_from(RecoveryCode)
                .where(
                    RecoveryCode.user_id == user.id,
                    RecoveryCode.used_at.is_(None),
                )
            )
            if unused >= MIN_UNUSED:
                print(f"{email}: {unused} unused code(s) remain — top-up skipped")
                continue
            codes = generate_recovery_codes(TARGET_UNUSED - unused)
            for code in codes:
                session.add(
                    RecoveryCode(
                        user_id=user.id,
                        code_hash=hash_recovery_code(code),
                    )
                )
            await session.flush()
            print(f"{email}: minted {len(codes)} one-time code(s)")
            for code in codes:
                print(f"  {code}")
        await session.commit()
    await engine.dispose()

    print(
        "\nEach code works exactly once, typed into the same field as the\n"
        "TOTP code at /staff/login. Re-run this script when they run low."
    )


if __name__ == "__main__":
    asyncio.run(main())
