# backend/tests/integration/identity/test_whitelist_admin.py
"""Whitelist bulk administration against real PostgreSQL (Plan 08 T3).

Scenario map (the plan's steps, verbatim semantics):

- **Preview classification (step 1):** one file carrying every row kind
  — valid number, full-width digits, duplicate-in-file, DB-existing,
  wrong length (short and long), garbage characters — gets per-row
  decisions with physical row numbers and codes, plus summary counts.
  Full-width rows are REJECTED per Plan 02 semantics (never normalized
  into the import); outer whitespace and a BOM are tolerated at the
  import boundary; blank lines are skipped without losing row numbers.
- **Confirm happy path:** only the previewed importable set lands, with
  the payload's enabled state, and one ``WHITELIST_IMPORT_CONFIRMED``
  audit row (G12) joins the same transaction, targeting the import
  digest.
- **Replay check:** a tampered payload (row added/removed/malformed) or
  a mismatched digest is a typed 409 and writes nothing.
- **Conflicts are deterministic and all-or-nothing:** a candidate that
  landed between preview and confirm refuses the whole import with the
  per-number conflict list; nothing of the batch lands (no partial
  silent overwrite).
- **Concurrent overlapping confirms (step 2):** two Admins with
  overlapping previews race on separate connections; exactly one
  imports fully, the loser gets the same typed conflict listing exactly
  the overlap, and the loser's unique rows are absent.
- **Enable/disable:** the flip writes ``WHITELIST_ENTRY_TOGGLED`` per
  actually-changed entry with before/after snapshots and the reason;
  idempotent re-flips change nothing and audit nothing; unknown numbers
  refuse the call.
- **Role gate:** STUDENT/TEACHER actors are refused on every surface.

Harness notes: savepoint-wrapped ``db_session`` for everything except
the concurrency test (services commit inside it; the outer rollback
keeps tests hermetic). The concurrency test follows the
claim-concurrency pattern: independent sessions with REAL commits from
the engine factory, released at one barrier, explicit committed DELETEs
in ``finally`` (whitelist rows and the test's own audit rows).
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import StudentWhitelist
from app.modules.identity.whitelist_admin import (
    AUDIT_WHITELIST_ENTRY_TOGGLED,
    AUDIT_WHITELIST_IMPORT_CONFIRMED,
    InvalidWhitelistConfirmError,
    WhitelistAdminService,
    WhitelistConfirmPayload,
    WhitelistImportConflictError,
    WhitelistImportCounts,
    WhitelistRowCode,
    import_digest,
)

_NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def _run_digits() -> str:
    """Unique-per-run digit prefix so leaked rows can never collide with
    a later seeding pass (the claim-concurrency discipline)."""
    return f"{secrets.randbelow(10**8):08d}"


def _actor(role: Role) -> Actor:
    return Actor(user_id=uuid.uuid4(), role=role)


def _service() -> WhitelistAdminService:
    return WhitelistAdminService(clock=FrozenClock(_NOW))


async def _seed_entry(
    db: AsyncSession, student_number: str, *, enabled: bool = True
) -> StudentWhitelist:
    entry = StudentWhitelist(student_number=student_number, enabled=enabled)
    db.add(entry)
    await db.flush()
    return entry


async def _count_rows(db: AsyncSession, prefix: str) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(StudentWhitelist)
            .where(StudentWhitelist.student_number.like(f"{prefix}%"))
        )
    )


async def _audit_rows(db: AsyncSession, action: str) -> list[AuditLog]:
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action)))


# --- preview classification (plan step 1) --------------------------------------------


@pytest.mark.integration
async def test_preview_classifies_every_row_kind(db_session: AsyncSession) -> None:
    run = _run_digits()
    existing = f"{run}02"  # seeded below; the file repeats it
    await _seed_entry(db_session, existing)
    trimmed = f"{run}03"
    content = (
        f"{run}01\n"  # 1: valid, importable
        "\n"  # 2: blank — skipped, still counted for row numbers
        "１２３４５６\n"  # 3: full-width digits — rejected (Plan 02), not normalized
        f"{run}01\n"  # 4: duplicate of row 1 in file
        f"{existing}\n"  # 5: already in DB
        f"{trimmed}  \n"  # 6: valid after outer-whitespace trim
        "12345\n"  # 7: ASCII digits, too short
        "123456789012345678901\n"  # 8: ASCII digits, too long (21)
        "20260a7\n"  # 9: garbage characters
    ).encode("utf-8-sig")  # BOM tolerated at the import boundary

    preview = await _service().preview_whitelist_import(
        db_session, _actor(Role.ADMIN), content
    )

    assert [(d.row_number, d.code) for d in preview.decisions] == [
        (1, WhitelistRowCode.IMPORTABLE),
        (3, WhitelistRowCode.FULL_WIDTH_DIGITS),
        (4, WhitelistRowCode.DUPLICATE_IN_FILE),
        (5, WhitelistRowCode.DUPLICATE_IN_DB),
        (6, WhitelistRowCode.IMPORTABLE),
        (7, WhitelistRowCode.INVALID_LENGTH),
        (8, WhitelistRowCode.INVALID_LENGTH),
        (9, WhitelistRowCode.INVALID_CHARACTERS),
    ]
    assert preview.decisions[0].student_number == f"{run}01"
    assert preview.decisions[1].student_number is None  # full-width: no canonical form
    assert preview.decisions[2].student_number == f"{run}01"  # the in-file duplicate
    assert preview.decisions[3].student_number == existing
    assert preview.decisions[4].student_number == trimmed
    assert preview.decisions[5].student_number == "12345"  # wrong length keeps it
    assert preview.importable == tuple(sorted([f"{run}01", trimmed]))
    assert preview.confirm_token == import_digest([f"{run}01", trimmed])
    assert preview.counts == WhitelistImportCounts(
        total_rows=8,
        importable=2,
        duplicate_in_file=1,
        duplicate_in_db=1,
        full_width_digits=1,
        invalid_characters=1,
        invalid_length=2,
    )
    # Preview writes nothing.
    assert await _count_rows(db_session, run) == 1


@pytest.mark.integration
async def test_full_width_row_is_never_normalized_into_the_import(
    db_session: AsyncSession,
) -> None:
    # Plan 02 semantics pinned end to end: a full-width row that WOULD be
    # legal after normalization is reported FULL_WIDTH_DIGITS and absent
    # from the importable set — the service must not "fix" it.
    preview = await _service().preview_whitelist_import(
        db_session, _actor(Role.ADMIN), "１２３４５６７８\n".encode()
    )
    assert [d.code for d in preview.decisions] == [WhitelistRowCode.FULL_WIDTH_DIGITS]
    assert preview.importable == ()
    assert preview.confirm_token == import_digest([])


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TEACHER])
async def test_preview_refuses_non_admin(db_session: AsyncSession, role: Role) -> None:
    with pytest.raises(BusinessError) as exc_info:
        await _service().preview_whitelist_import(
            db_session, _actor(role), b"20260101\n"
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == ErrorCode.PERMISSION_DENIED


# --- confirm -------------------------------------------------------------------------


@pytest.mark.integration
async def test_confirm_imports_the_previewed_set_and_audits(
    db_session: AsyncSession,
) -> None:
    run = _run_digits()
    first, second = f"{run}11", f"{run}12"
    service = _service()
    admin = _actor(Role.ADMIN)
    preview = await service.preview_whitelist_import(
        db_session, admin, f"{first}\n{second}\n".encode()
    )
    payload = WhitelistConfirmPayload(
        confirm_token=preview.confirm_token,
        enable=True,
        student_numbers=preview.importable,
    )

    result = await service.confirm_whitelist_import(db_session, admin, payload)

    assert result.created == 2
    rows = {
        row.student_number: row
        for row in await db_session.scalars(
            select(StudentWhitelist).where(
                StudentWhitelist.student_number.in_([first, second])
            )
        )
    }
    assert set(rows) == {first, second}
    assert all(row.enabled for row in rows.values())
    assert all(row.disabled_at is None for row in rows.values())

    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == AUDIT_WHITELIST_IMPORT_CONFIRMED,
            AuditLog.target_id == preview.confirm_token,
        )
    )
    assert audit is not None
    assert audit.details == {"created": 2, "duplicated": 0, "enable": True}


@pytest.mark.integration
async def test_confirm_imports_disabled_rows_when_payload_disables(
    db_session: AsyncSession,
) -> None:
    run = _run_digits()
    number = f"{run}21"
    service = _service()
    admin = _actor(Role.ADMIN)
    preview = await service.preview_whitelist_import(
        db_session, admin, f"{number}\n".encode()
    )
    payload = WhitelistConfirmPayload(
        confirm_token=preview.confirm_token,
        enable=False,
        student_numbers=preview.importable,
    )

    result = await service.confirm_whitelist_import(db_session, admin, payload)

    assert result.enable is False
    row = await db_session.scalar(
        select(StudentWhitelist).where(StudentWhitelist.student_number == number)
    )
    assert row is not None and row.enabled is False
    assert row.disabled_at == _NOW


@pytest.mark.integration
@pytest.mark.parametrize(
    ("numbers", "token"),
    [
        # row added after preview
        (["20260901", "20260902"], import_digest(["20260901"])),
        # row removed after preview
        (["20260901"], import_digest(["20260901", "20260902"])),
        # malformed row smuggled in
        (["12345"], import_digest(["12345"])),
        # empty set
        ([], import_digest([])),
    ],
)
async def test_confirm_rejects_payloads_that_do_not_replay_the_preview(
    db_session: AsyncSession, numbers: list[str], token: str
) -> None:
    payload = WhitelistConfirmPayload(
        confirm_token=token, enable=True, student_numbers=tuple(numbers)
    )
    with pytest.raises(InvalidWhitelistConfirmError) as exc_info:
        await _service().confirm_whitelist_import(
            db_session, _actor(Role.ADMIN), payload
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == ErrorCode.CONFLICT
    rows = await db_session.scalars(select(StudentWhitelist))
    assert list(rows) == []


@pytest.mark.integration
async def test_confirm_conflicts_refuse_the_whole_batch(
    db_session: AsyncSession,
) -> None:
    run = _run_digits()
    overlap, fresh = f"{run}31", f"{run}32"
    service = _service()
    admin = _actor(Role.ADMIN)
    # Preview sees both as importable...
    preview = await service.preview_whitelist_import(
        db_session, admin, f"{overlap}\n{fresh}\n".encode()
    )
    # ...then a concurrent import lands the overlap AFTER preview: the
    # digest still replays, so it is the DB conflict — not the digest —
    # that must refuse the confirm, deterministically.
    await _seed_entry(db_session, overlap)
    payload = WhitelistConfirmPayload(
        confirm_token=preview.confirm_token,
        enable=True,
        student_numbers=preview.importable,
    )

    with pytest.raises(WhitelistImportConflictError) as exc_info:
        await service.confirm_whitelist_import(db_session, admin, payload)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.details == {"student_numbers": [overlap]}
    # All-or-nothing: the fresh row did NOT land beside the conflict.
    assert await _count_rows(db_session, run) == 1
    assert await _audit_rows(db_session, AUDIT_WHITELIST_IMPORT_CONFIRMED) == []


@pytest.mark.integration
async def test_concurrent_overlapping_confirms_resolve_deterministically(
    db_engine: AsyncEngine,
) -> None:
    """Plan step 2: two Admins confirm overlapping imports. Exactly one
    imports fully; the loser gets the typed per-number duplicate result
    and nothing of theirs lands — the UNIQUE constraint closes the race
    the friendly pre-check cannot see."""
    run = _run_digits()
    a_only, overlap, b_only = f"{run}41", f"{run}42", f"{run}43"
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    service = _service()
    payloads: dict[str, WhitelistConfirmPayload] = {
        "a": WhitelistConfirmPayload(
            confirm_token=import_digest([a_only, overlap]),
            enable=True,
            student_numbers=(a_only, overlap),
        ),
        "b": WhitelistConfirmPayload(
            confirm_token=import_digest([overlap, b_only]),
            enable=True,
            student_numbers=(overlap, b_only),
        ),
    }

    async def _confirm(name: str, start: asyncio.Event) -> object:
        async with factory() as session:
            await session.connection()  # warm the pooled connection first
            await start.wait()
            return await service.confirm_whitelist_import(
                session, _actor(Role.ADMIN), payloads[name]
            )

    start = asyncio.Event()
    tasks = [asyncio.create_task(_confirm(name, start)) for name in ("a", "b")]
    await asyncio.sleep(0.05)  # let every coroutine reach the barrier
    start.set()
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    outcomes = dict(zip(("a", "b"), gathered, strict=True))

    winners = [n for n, o in outcomes.items() if not isinstance(o, Exception)]
    losers = [n for n, o in outcomes.items() if isinstance(o, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    loser = outcomes[losers[0]]
    assert isinstance(loser, WhitelistImportConflictError)
    assert loser.code == ErrorCode.CONFLICT
    assert loser.details == {"student_numbers": [overlap]}
    winner_numbers = set(payloads[winners[0]].student_numbers)

    try:
        async with factory() as session:
            landed = set(
                await session.scalars(
                    select(StudentWhitelist.student_number).where(
                        StudentWhitelist.student_number.like(f"{run}%")
                    )
                )
            )
            # The winner's set landed whole; the loser's unique row did
            # not; the overlap exists exactly once.
            assert landed == winner_numbers
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(StudentWhitelist)
                    .where(StudentWhitelist.student_number == overlap)
                )
                == 1
            )
            # The winner's audit row exists (the loser wrote nothing).
            audit = await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == AUDIT_WHITELIST_IMPORT_CONFIRMED,
                    AuditLog.target_id.in_(
                        [payloads["a"].confirm_token, payloads["b"].confirm_token]
                    ),
                )
            )
            assert len(list(audit)) == 1
    finally:
        async with factory() as session:
            await session.execute(
                delete(StudentWhitelist).where(
                    StudentWhitelist.student_number.like(f"{run}%")
                )
            )
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.action == AUDIT_WHITELIST_IMPORT_CONFIRMED,
                    AuditLog.target_id.in_(
                        [payloads["a"].confirm_token, payloads["b"].confirm_token]
                    ),
                )
            )
            await session.commit()


# --- enable/disable ------------------------------------------------------------------


@pytest.mark.integration
async def test_toggle_writes_audit_and_is_idempotent(db_session: AsyncSession) -> None:
    run = _run_digits()
    first, second = f"{run}51", f"{run}52"
    await _seed_entry(db_session, first)
    await _seed_entry(db_session, second)
    actor = _actor(Role.ADMIN)
    service = _service()

    disabled = await service.set_entries_enabled(
        db_session,
        actor,
        [first, second],
        enabled=False,
        reason="批量停用测试",
    )

    assert disabled.toggled == (first, second)
    assert disabled.unchanged == ()
    rows = {
        row.student_number: row
        for row in await db_session.scalars(
            select(StudentWhitelist).where(
                StudentWhitelist.student_number.in_([first, second])
            )
        )
    }
    assert all(not row.enabled for row in rows.values())
    assert all(row.disabled_at == _NOW for row in rows.values())

    audits = await _audit_rows(db_session, AUDIT_WHITELIST_ENTRY_TOGGLED)
    assert len(audits) == 2
    assert [row.before_snapshot for row in audits] == [
        {"enabled": True},
        {"enabled": True},
    ]
    assert [row.after_snapshot for row in audits] == [
        {"enabled": False},
        {"enabled": False},
    ]
    assert all(row.reason == "批量停用测试" for row in audits)

    # Idempotent re-flip: nothing changes, nothing new is audited.
    again = await service.set_entries_enabled(
        db_session, actor, [first, second], enabled=False
    )
    assert again.toggled == ()
    assert again.unchanged == (first, second)
    assert len(await _audit_rows(db_session, AUDIT_WHITELIST_ENTRY_TOGGLED)) == 2

    # And back on: disabled_at clears with the flag.
    enabled = await service.set_entries_enabled(
        db_session, actor, [first], enabled=True
    )
    assert enabled.toggled == (first,)
    row = await db_session.scalar(
        select(StudentWhitelist).where(StudentWhitelist.student_number == first)
    )
    assert row is not None and row.enabled and row.disabled_at is None
    assert len(await _audit_rows(db_session, AUDIT_WHITELIST_ENTRY_TOGGLED)) == 3


@pytest.mark.integration
async def test_toggle_refuses_unknown_numbers_wholesale(
    db_session: AsyncSession,
) -> None:
    run = _run_digits()
    known = f"{run}61"
    await _seed_entry(db_session, known)

    with pytest.raises(BusinessError) as exc_info:
        await _service().set_entries_enabled(
            db_session, _actor(Role.ADMIN), [known, f"{run}99"], enabled=False
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.details == {"missing": [f"{run}99"]}
    row = await db_session.scalar(
        select(StudentWhitelist).where(StudentWhitelist.student_number == known)
    )
    assert row is not None and row.enabled  # untouched


@pytest.mark.integration
async def test_toggle_refuses_non_admin(db_session: AsyncSession) -> None:
    run = _run_digits()
    await _seed_entry(db_session, f"{run}71")

    with pytest.raises(BusinessError) as exc_info:
        await _service().set_entries_enabled(
            db_session, _actor(Role.TEACHER), [f"{run}71"], enabled=False
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == ErrorCode.PERMISSION_DENIED
