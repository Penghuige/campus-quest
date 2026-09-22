"""Seed demo accounts into the running dev database.

Usage (from backend/, with the dev-stack environment exported —
DATABASE_URL pointing at the compose PostgreSQL, REDIS_URL, S3_*, BUSINESS_TIMEZONE):
  uv run python scripts/seed_demo_accounts.py

Creates:
- STUDENTS: student01..student20, password "student-demo-2026", ACTIVE
- ADMIN: admin@campus.example.edu, password "admin-demo-2026", with a
  CONFIRMED TOTP credential (fixed demo secret, printed with the
  otpauth URI and the current code) so management endpoints' 2FA
  gate can be passed immediately.
- TEACHER: teacher@campus.example.edu, same shape (staff console demo).

Idempotent: skips rows whose username/email already exist.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyotp
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.totp import (
    build_otpauth_uri,
    encrypt_totp_secret,
)

STUDENT_PASSWORD = "student-demo-2026"
STAFF_PASSWORD = "admin-demo-2026"
# A fixed demo TOTP secret: printed so an authenticator app can add it;
# NOT a production pattern (production secrets are per-account CSPRNG).
DEMO_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

STAFF_ACCOUNTS = [
    ("admin@campus.example.edu", Role.ADMIN, "管理员"),
    ("teacher@campus.example.edu", Role.TEACHER, "教师"),
]


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    fernet = Fernet(settings.totp_encryption_key.encode())
    now = datetime.now(UTC)
    created: list[str] = []

    async with maker() as session:
        for index in range(1, 21):
            username = f"student{index:02d}"
            exists = await session.scalar(
                select(User.id).where(User.username == username)
            )
            if exists is None:
                session.add(
                    User(
                        username=username,
                        password_hash=hash_password(STUDENT_PASSWORD),
                        nickname=f"同学{index:02d}",
                        phone_e164=None,
                        role=Role.STUDENT,
                        status=UserStatus.ACTIVE,
                    )
                )
                created.append(f"student {username} / {STUDENT_PASSWORD}")
        for email, role, label in STAFF_ACCOUNTS:
            exists = await session.scalar(
                select(User.id).where(User.email_normalized == email)
            )
            if exists is not None:
                continue
            staff = User(
                username=email,  # staff username mirrors the email
                password_hash=hash_password(STAFF_PASSWORD),
                nickname=label,
                phone_e164=None,
                email_normalized=email,
                email_verified_at=now,
                role=role,
                status=UserStatus.ACTIVE,
            )
            session.add(staff)
            await session.flush()
            session.add(
                TotpCredential(
                    user_id=staff.id,
                    secret_encrypted=encrypt_totp_secret(fernet, DEMO_TOTP_SECRET),
                    confirmed_at=now,
                )
            )
            created.append(f"{role.value} {email} / {STAFF_PASSWORD}")
        await session.commit()
    await engine.dispose()

    print(f"created {len(created)} account(s); existing rows skipped")
    for line in created:
        print(f"  + {line}")
    print("\nTOTP (both staff accounts share the demo secret):")
    print(f"  secret:   {DEMO_TOTP_SECRET}")
    print(
        "  otpauth:  " + build_otpauth_uri(DEMO_TOTP_SECRET, "admin@campus.example.edu")
    )
    totp = pyotp.TOTP(DEMO_TOTP_SECRET)
    print(f"  code now: {totp.now()}")
    print("  (codes rotate every 30s — a fresh one is required at login)")


if __name__ == "__main__":
    asyncio.run(main())
