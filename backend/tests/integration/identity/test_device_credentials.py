# backend/tests/integration/identity/test_device_credentials.py
"""Remembered-device credential lifecycle (V1.1 §2.2, Wave 1 Task 3;
the 2026-10-24 multi-slot batch).

Integration (real PostgreSQL rollback harness) because every operation
is a table round-trip; the Clock is frozen so the 30-day expiry and the
``last_used_at`` bump are exact, not slept for.

The multi-slot contract (2026-10-24): one cookie NAME per account —
``cq_dev_<sha256(canonical identifier)[:10]>`` — so remembering account
B never overwrites account A's slot in the same browser jar. The slot
handle is the cookie-name suffix: opaque, non-reversible, and the only
thing the frontend echoes back as the verify ``target``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.modules.identity.device_credentials import (
    DEVICE_COOKIE_PREFIX,
    DEVICE_TTL_DAYS,
    canonical_identifier,
    issue,
    resolve_remembered,
    revoke_all,
    slot_suffix,
    slots_from,
    validate,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import RememberedDevice, User

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 10, 15, 8, 0, 0, tzinfo=UTC)


async def _seed_staff(db: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        password_hash=None,
        nickname="devices",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _frozen_at(moment: datetime) -> FrozenClock:
    return FrozenClock(moment)


def test_cookie_contract_constants() -> None:
    """The frontend contract: the slot-name prefix and the 30-day
    lifetime. The single-slot name ``cq_staff_device`` is RETIRED
    (2026-10-24): it let one "remember me" overwrite another account's
    credential — the multi-account switching bug."""
    assert DEVICE_COOKIE_PREFIX == "cq_dev_"
    assert DEVICE_TTL_DAYS == 30


def test_slot_suffix_is_a_stable_ten_char_handle() -> None:
    """sha256(identifier)[:10] — deterministic, distinct per identifier,
    and never the identifier itself (the frontend holds only this and
    the masked form)."""
    assert slot_suffix("+8613800160001") == sha256(b"+8613800160001").hexdigest()[:10]
    assert slot_suffix("+8613800160001") == slot_suffix("+8613800160001")
    assert slot_suffix("+8613800160001") != slot_suffix("+8613800160002")
    assert len(slot_suffix("x")) == 10


def test_canonical_identifier_chain_phone_email_username() -> None:
    """The one identifier chain every slot names by and every probe
    masks from: phone_e164 first, then email_normalized, then a
    username that IS a mailbox (the pre-normalization invite shape). A
    row with none of them has no slot and no banner face."""
    with_phone = User(
        username="20250000001",
        nickname="x",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
        phone_e164="+8613800160001",
        email_normalized="a@campus.example.edu",
    )
    assert canonical_identifier(with_phone) == ("+8613800160001", "phone")

    email_only = User(
        username="b@campus.example.edu",
        nickname="x",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
        email_normalized="b@campus.example.edu",
    )
    assert canonical_identifier(email_only) == ("b@campus.example.edu", "email")

    mailbox_username = User(
        username="legacy@campus.example.edu",
        nickname="x",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    assert canonical_identifier(mailbox_username) == (
        "legacy@campus.example.edu",
        "email",
    )

    bare = User(
        username="20250000002",
        nickname="x",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    assert canonical_identifier(bare) is None


def test_slots_from_collects_only_slot_cookies() -> None:
    """The request-cookie harvest: every cq_dev_* name becomes
    suffix→value; the retired name and unrelated cookies are ignored."""
    harvested = slots_from(
        {
            "cq_staff_device": "retired-value",
            "cq_dev_ab12cd34ef": "value-a",
            "cq_dev_": "empty-suffix",
            "refresh": "unrelated",
        }
    )
    assert harvested == {"ab12cd34ef": "value-a", "": "empty-suffix"}


async def test_issue_returns_value_and_slot_and_stores_only_the_hash(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-hash@campus.example.edu")
    clock = _frozen_at(_T0)

    cookie_value, slot = await issue(db_session, staff, clock=clock)

    rows = (
        (
            await db_session.execute(
                select(RememberedDevice).where(RememberedDevice.user_id == staff.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    stored = rows[0]
    assert stored.token_hash == sha256(cookie_value.encode()).hexdigest()
    assert stored.token_hash != cookie_value
    assert stored.expires_at == _T0 + timedelta(days=DEVICE_TTL_DAYS)
    assert stored.last_used_at is None
    # The cookie value is urlsafe(32): 43 base64url characters.
    assert len(cookie_value) == 43
    # The slot is the canonical identifier's handle (the @-username arm).
    assert slot == slot_suffix("devices-hash@campus.example.edu")


async def test_issue_mints_the_same_slot_for_the_same_account(
    db_session: AsyncSession,
) -> None:
    """同账号覆盖: every mint of one account names the SAME slot — the
    browser jar holds one cookie per account, whichever form the login
    typed (the canonical chain, not the typed identifier, names it)."""
    staff = await _seed_staff(db_session, "devices-slot@campus.example.edu")
    clock = _frozen_at(_T0)

    _, first_slot = await issue(db_session, staff, clock=clock)
    _, second_slot = await issue(db_session, staff, clock=clock)

    assert first_slot == second_slot


async def test_validate_roundtrip_returns_user_and_bumps_last_used(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-roundtrip@campus.example.edu")
    clock = _frozen_at(_T0)
    cookie_value, _ = await issue(db_session, staff, clock=clock)

    assert await validate(db_session, cookie_value, clock=clock) == staff.id

    row = (
        await db_session.execute(
            select(RememberedDevice).where(RememberedDevice.user_id == staff.id)
        )
    ).scalar_one()
    assert row.last_used_at == _T0


async def test_validate_expired_cookie_returns_none(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-expired@campus.example.edu")
    cookie_value, _ = await issue(db_session, staff, clock=_frozen_at(_T0))

    later = FrozenClock(_T0 + timedelta(days=DEVICE_TTL_DAYS, seconds=1))
    assert await validate(db_session, cookie_value, clock=later) is None


async def test_validate_wrong_value_returns_none(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-wrong@campus.example.edu")
    await issue(db_session, staff, clock=_frozen_at(_T0))

    assert (
        await validate(db_session, "not-a-device-cookie", clock=_frozen_at(_T0)) is None
    )


async def test_resolve_remembered_sorts_by_recency_without_bumping(
    db_session: AsyncSession,
) -> None:
    """The enumeration read behind the probe and the empty-body verify:
    valid entries sorted most-recent first, keyed by (last_used_at or
    created_at), and NO last_used_at bump — enumeration must not
    flatten the very recency order it reports (a bump rides only the
    credential a login actually uses).

    The clock anchors AHEAD of the row's server-default created_at
    (real now): under production's SystemClock a bump is always the
    newest timestamp, and the frozen test must reproduce that relation,
    not invert it (a past-anchored bump can never beat a later mint).
    """
    from datetime import datetime as _datetime

    ahead = _datetime(2027, 1, 1, tzinfo=UTC)
    first = await _seed_staff(db_session, "resolve-a@campus.example.edu")
    second = await _seed_staff(db_session, "resolve-b@campus.example.edu")
    clock = _frozen_at(ahead)
    first_value, first_slot = await issue(db_session, first, clock=clock)
    second_value, second_slot = await issue(db_session, second, clock=clock)

    entries = await resolve_remembered(
        db_session,
        {first_slot: first_value, second_slot: second_value, "deadbeef99": "garbage"},
        clock=clock,
    )

    # The garbage slot drops; the valid ones arrive with their rows and
    # users, never-used rows keyed by created_at (tie: insertion order).
    assert [e.slot for e in entries if e.slot != second_slot] == [first_slot]
    assert {e.user.id for e in entries} == {first.id, second.id}
    assert all(e.row.last_used_at is None for e in entries)

    # A bump through the login read (validate) reorders: first becomes
    # the most recent WITHOUT touching second's row.
    assert await validate(db_session, first_value, clock=clock) == first.id
    reordered = await resolve_remembered(
        db_session, {first_slot: first_value, second_slot: second_value}, clock=clock
    )
    assert reordered[0].slot == first_slot
    second_row = next(e.row for e in reordered if e.slot == second_slot)
    assert second_row.last_used_at is None


async def test_resolve_remembered_dedupes_an_account_across_renamed_slots(
    db_session: AsyncSession,
) -> None:
    """A phone/email change re-keys an account's slot: the browser may
    then hold BOTH the old-name and new-name cookies of one user. The
    enumeration answers that account ONCE (its most recent entry) —
    the banner never lists the same account twice."""
    staff = await _seed_staff(db_session, "rename-old@campus.example.edu")
    clock = _frozen_at(_T0)
    old_value, old_slot = await issue(db_session, staff, clock=clock)

    staff.email_normalized = "rename-new@campus.example.edu"
    await db_session.flush()
    new_value, new_slot = await issue(db_session, staff, clock=clock)
    assert new_slot != old_slot

    entries = await resolve_remembered(
        db_session, {old_slot: old_value, new_slot: new_value}, clock=clock
    )

    assert len(entries) == 1
    assert entries[0].user.id == staff.id


async def test_resolve_remembered_expires_old_rows(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "resolve-exp@campus.example.edu")
    value, slot = await issue(db_session, staff, clock=_frozen_at(_T0))

    later = FrozenClock(_T0 + timedelta(days=DEVICE_TTL_DAYS, seconds=1))
    entries = await resolve_remembered(db_session, {slot: value}, clock=later)

    assert entries == []


async def test_revoke_all_deletes_every_device_and_counts(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-revoke@campus.example.edu")
    clock = _frozen_at(_T0)
    first, _ = await issue(db_session, staff, clock=clock)
    second, _ = await issue(db_session, staff, clock=clock)

    deleted = await revoke_all(db_session, staff.id)

    assert deleted == 2
    assert await validate(db_session, first, clock=clock) is None
    assert await validate(db_session, second, clock=clock) is None


async def test_two_users_devices_are_independent(
    db_session: AsyncSession,
) -> None:
    first = await _seed_staff(db_session, "devices-a@campus.example.edu")
    second = await _seed_staff(db_session, "devices-b@campus.example.edu")
    clock = _frozen_at(_T0)
    first_cookie, first_slot = await issue(db_session, first, clock=clock)
    second_cookie, second_slot = await issue(db_session, second, clock=clock)
    assert first_slot != second_slot

    assert await revoke_all(db_session, first.id) == 1

    assert await validate(db_session, first_cookie, clock=clock) is None
    assert await validate(db_session, second_cookie, clock=clock) == second.id


async def test_validate_uuid_return_type(db_session: AsyncSession) -> None:
    """validate answers a UUID (or None) — never the raw row."""
    staff = await _seed_staff(db_session, "devices-type@campus.example.edu")
    cookie_value, _ = await issue(db_session, staff, clock=_frozen_at(_T0))

    user_id = await validate(db_session, cookie_value, clock=_frozen_at(_T0))
    assert isinstance(user_id, UUID)
