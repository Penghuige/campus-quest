# backend/app/modules/identity/device_credentials.py
"""Remembered-device cookie credentials (V1.1 §2.2, Wave 1; the
2026-10-24 multi-slot batch).

The 30-day "remember this device" credential, ONE COOKIE NAME PER
ACCOUNT: ``cq_dev_<sha256(canonical identifier)[:10]>``. The name is
the account's slot — remembering account B overwrites only B's slot in
the browser jar, never A's (the single-name ``cq_staff_device`` model
let each "remember me" evict the previous account's credential, which
broke multi-account switching; retired 2026-10-24 with this batch —
the few local/production holders simply re-login once, the minted
values under the old name are no longer read).

``issue`` mints one cookie value for the account's slot, stores only
its SHA-256 digest, and hands the plaintext back exactly once for the
response's ``Set-Cookie``; ``validate`` resolves a presented value to
its user (bumping ``last_used_at``); ``resolve_remembered`` enumerates
a whole presented jar for the probe and the empty-body verify (no
bump — listing must not flatten the recency order it reports);
``revoke_all`` drops every device credential of one user — called by
session-kick and self-service management so a stolen cookie cannot
outlive the account's other credentials (Review Focus 3).

The slot handle (the cookie-name suffix) is deliberately the ONLY
account reference the frontend ever holds: it is opaque,
non-reversible, and the verify leg's ``target`` echoes it back.

Plain module functions, not a service class: every operation is one
stateless table round-trip with no wiring beyond the session the caller
already holds. Expiry is a caller-injected ``Clock`` comparison, never
a slept wall clock (backend-engineering §11).

Deliberately NOT an index on ``token_hash``: lookup volume is one row
per login attempt, and the unique-ish digest scan stays trivial at
that shape; the index would only serve a brute-force workload the
cookie's 256 bits already make hopeless.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.identity.models import RememberedDevice, User

#: The browser-facing cookie-name PREFIX — each account's slot appends
#: its handle: ``cq_dev_<sha256(canonical identifier)[:10]>``. The
#: single-slot predecessor ``cq_staff_device`` is retired (see the
#: module docstring); its name is no longer read anywhere.
DEVICE_COOKIE_PREFIX = "cq_dev_"
DEVICE_TTL_DAYS = 30
_COOKIE_MAX_AGE_SECONDS = DEVICE_TTL_DAYS * 24 * 3600
AUDIT_OWN_DEVICES_REVOKED = "OWN_DEVICES_REVOKED"

#: The probe's banner cap: the popover lists at most this many faces
#: (most recent first). A slot beyond the cap stays USABLE by ``target``
#: — the cap is a listing rule, not an auth rule.
PROBE_MAX_ENTRIES = 5


def _hash_cookie(cookie_value: str) -> str:
    return sha256(cookie_value.encode()).hexdigest()


def slot_suffix(identifier: str) -> str:
    """The slot handle: sha256(identifier)[:10] — stable, opaque, and
    never the identifier itself."""
    return sha256(identifier.encode()).hexdigest()[:10]


def canonical_identifier(user: User) -> tuple[str, str] | None:
    """The ONE identifier an account's slot names by and its banner
    face masks from: the bound phone first, then the normalized email,
    then a username that IS a mailbox (the pre-normalization invite
    shape). Returns ``(identifier, kind)`` with kind ``"phone"`` /
    ``"email"`` (the mask selector), or None for a row with none of
    the three — such a row has no slot and no banner face."""
    if user.phone_e164:
        return user.phone_e164, "phone"
    if user.email_normalized:
        return user.email_normalized, "email"
    if user.username and "@" in user.username:
        return user.username, "email"
    return None


def slots_from(cookies: Mapping[str, str]) -> dict[str, str]:
    """Harvest the presented jar into {slot handle: cookie value} —
    every ``cq_dev_*`` name, nothing else (the retired name included
    in the ignore list by construction)."""
    harvested: dict[str, str] = {}
    for name, value in cookies.items():
        if name.startswith(DEVICE_COOKIE_PREFIX):
            harvested[name[len(DEVICE_COOKIE_PREFIX) :]] = value
    return harvested


@dataclass(frozen=True, slots=True)
class RememberedEntry:
    """One resolved slot: its handle, its credential row, and its
    user — the probe's and the empty-body verify's working unit."""

    slot: str
    row: RememberedDevice
    user: User


async def issue(db: AsyncSession, user: User, *, clock: Clock) -> tuple[str, str]:
    """Mint one device credential for ``user``'s slot; return
    ``(cookie_value, slot_suffix)`` — the value travels exactly once
    for the response's ``Set-Cookie``, the suffix names it.

    Only the SHA-256 digest is persisted, so a database leak yields
    nothing replayable (see the model docstring). The slot is the
    account's canonical identifier's handle, so every mint of one
    account lands on the same name — the browser jar holds one cookie
    per account (同账号覆盖), other accounts untouched.

    A row with no canonical identifier cannot be slotted and mints
    nothing: the empty pair answers, and the caller sets no cookie.
    """
    canonical = canonical_identifier(user)
    if canonical is None:
        return "", ""
    cookie_value = secrets.token_urlsafe(32)
    db.add(
        RememberedDevice(
            user_id=user.id,
            token_hash=_hash_cookie(cookie_value),
            expires_at=clock.now() + timedelta(days=DEVICE_TTL_DAYS),
        )
    )
    await db.flush()
    return cookie_value, slot_suffix(canonical[0])


async def validate(db: AsyncSession, cookie_value: str, *, clock: Clock) -> UUID | None:
    """Resolve a presented cookie; None when unknown or expired.

    A success bumps ``last_used_at`` so the self-service device list
    can show real recency; the bump rides the caller's transaction.
    Enumerations that must NOT bump (the probe, the empty-body leg's
    non-chosen slots) read through ``resolve_remembered`` instead.
    """
    row = await _resolve_row(db, cookie_value, clock=clock)
    if row is None:
        return None
    row.last_used_at = clock.now()
    await db.flush()
    return row.user_id


async def _resolve_row(
    db: AsyncSession, cookie_value: str, *, clock: Clock
) -> RememberedDevice | None:
    row = await db.scalar(
        select(RememberedDevice).where(
            RememberedDevice.token_hash == _hash_cookie(cookie_value)
        )
    )
    if row is None or row.expires_at <= clock.now():
        return None
    return row


async def resolve_remembered(
    db: AsyncSession, slots: Mapping[str, str], *, clock: Clock
) -> list[RememberedEntry]:
    """Enumerate a presented jar: every slot whose value resolves to a
    live credential becomes one entry, sorted most-recent first by
    (``last_used_at`` or ``created_at`` — a never-used credential
    counts as its mint time). No ``last_used_at`` bump: listing is not
    using, and an enumeration that bumped its own ordering signal would
    flatten it (the bump rides only the credential a login uses).

    An account renamed since mint (a changed phone/email re-keys its
    slot) can present BOTH the old-name and new-name cookies; the
    enumeration answers the account once — its most recent entry.
    """
    entries: list[RememberedEntry] = []
    for slot, value in slots.items():
        row = await _resolve_row(db, value, clock=clock)
        if row is None:
            continue
        user = await db.get(User, row.user_id)
        if user is None:
            continue
        entries.append(RememberedEntry(slot=slot, row=row, user=user))
    entries.sort(key=lambda e: e.row.last_used_at or e.row.created_at, reverse=True)
    deduped: list[RememberedEntry] = []
    seen_users: set[UUID] = set()
    for entry in entries:  # most-recent first: the first hit per user wins
        if entry.user.id in seen_users:
            continue
        seen_users.add(entry.user.id)
        deduped.append(entry)
    return deduped


async def any_valid_for(
    db: AsyncSession, slots: Mapping[str, str], user_id: UUID, *, clock: Clock
) -> bool:
    """The honoring read (the password/code legs): does the presented
    jar hold ANY live credential of THIS account?

    The slot NAMES never gate honoring — ownership does: a renamed
    identifier since mint still honors, because the proof is the row's
    user, not the cookie's name. Foreign credentials resolve WITHOUT a
    bump (their recency is not this login's to spend); the first
    matching row bumps — it just helped complete a login."""
    for value in slots.values():
        row = await _resolve_row(db, value, clock=clock)
        if row is not None and row.user_id == user_id:
            row.last_used_at = clock.now()
            await db.flush()
            return True
    return False


async def revoke_all(db: AsyncSession, user_id: UUID) -> int:
    """Delete every device credential of ``user_id``; return the count.

    One account, every slot, every browser: a remembered credential
    must not outlive the account's other credentials."""
    result = await db.execute(
        delete(RememberedDevice).where(RememberedDevice.user_id == user_id)
    )
    await db.flush()
    # rowcount is the matched-row count (the cleanup-jobs cast precedent).
    return int(cast("CursorResult[Any]", result).rowcount)


async def list_for_user(db: AsyncSession, user_id: UUID) -> list[RememberedDevice]:
    """The owner's devices, newest first — the /me/devices list source.

    The route maps rows to the explicit public DTO; the hash-bearing
    rows never leave this module as anything but ``id``/timestamps.
    """
    return list(
        await db.scalars(
            select(RememberedDevice)
            .where(RememberedDevice.user_id == user_id)
            .order_by(RememberedDevice.created_at.desc(), RememberedDevice.id)
        )
    )


async def revoke_all_for_user(
    db: AsyncSession,
    *,
    user_id: UUID,
    actor: Actor,
    audit: AuditLogWriter | None = None,
    audit_context: AuditContext | None = None,
) -> int:
    """Self-service revocation (V1.1 §2.2): drop every device credential
    of the caller and write the ``OWN_DEVICES_REVOKED`` audit row in the
    SAME transaction (flush-only writer, the PR #80 discipline; actor =
    the account that revoked its own devices).
    """
    writer = audit if audit is not None else AuditLogWriter()
    count = await revoke_all(db, user_id)
    await writer.append(
        db,
        actor=actor,
        action=AUDIT_OWN_DEVICES_REVOKED,
        target_type="user",
        target_id=str(user_id),
        after_snapshot={"revoked": count},
        ip_address=audit_context.ip_address if audit_context else None,
        request_id=audit_context.request_id if audit_context else None,
    )
    await db.commit()
    return count
