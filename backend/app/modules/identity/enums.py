# backend/app/modules/identity/enums.py
"""Identity enums frozen by docs/architecture/interfaces.md.

Members and values are canonical (`value == member name`); database columns
persist the exact string as VARCHAR + CHECK constraints (see models.py for
why they are not PostgreSQL native enums).
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    ADMIN = "ADMIN"


class UserStatus(StrEnum):
    PENDING_PHONE = "PENDING_PHONE"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    BANNED = "BANNED"
    # V1.1 auth-extension II (spec 2026-10-16 §1-§2): open enrollment
    # lands students here — login and browsing allowed, every acting
    # surface frozen until an admin approves (→ACTIVE) or rejects
    # (→BANNED with reason).
    PENDING_APPROVAL = "PENDING_APPROVAL"
