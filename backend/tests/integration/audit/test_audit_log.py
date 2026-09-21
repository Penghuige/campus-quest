# backend/tests/integration/audit/test_audit_log.py
"""The durable AuditLog's own contract (G12; PR #2 hardening P0-5).

Two seams the wiring tests (community reveal, points decisions) do not
pin on their own:

- the append-only MODEL discipline: no onupdate columns, the class
  docstring documents append-only semantics — the same convention test
  PointsLedger carries (test_points_constraints), because the database
  deliberately does not enforce immutability (no trigger; the 0007
  ruling);
- the WRITER's flush-only shape: ``AuditLogWriter.append`` persists one
  row with the database-generated id/timestamp when the CALLER commits
  (backend-engineering §5) and snapshots the actor's role as plain
  text.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User

_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
_PASSWORD = "correct-horse-battery"


@pytest.mark.integration
async def test_audit_log_model_is_append_only_by_convention() -> None:
    """AuditLog is immutable audit history (G7/G12): no UPDATE or DELETE
    path exists, the sole INSERT path is the writer, and the model
    documents the discipline. The database deliberately does not enforce
    append-only — the points_ledger ruling (0007)."""
    assert AuditLog.__doc__ is not None
    assert "append-only" in AuditLog.__doc__.lower()
    for column in AuditLog.__table__.columns:
        assert column.onupdate is None, f"AuditLog.{column.name} must not auto-update"
    assert "updated_at" not in AuditLog.__table__.columns


@pytest.mark.integration
async def test_writer_appends_one_row_committed_by_the_caller(
    db_session: AsyncSession,
) -> None:
    """``append`` joins the caller's transaction (flush-only) and the
    CALLER's commit persists the row: database-generated id and
    created_at, the actor's role snapshotted as text, and the details
    mapping persisted as JSONB."""
    admin = User(
        username="audit-admin-0001",
        password_hash=hash_password(_PASSWORD),
        nickname="审计管理员",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db_session.add(admin)
    await db_session.flush()

    row = await AuditLogWriter().append(
        db_session,
        actor=Actor(user_id=admin.id, role=Role.ADMIN),
        action="TEST_PROBE_ACTION",
        target_type="comment",
        target_id="00000000-0000-0000-0000-000000000001",
        reason="契约测试",
        details={"key": "value"},
    )
    # Flush-only: the row is staged in THIS transaction, not committed.
    assert row.id is not None  # RETURNING fetched the server default
    assert row.created_at is not None
    await db_session.commit()

    loaded = await db_session.scalar(select(AuditLog).where(AuditLog.id == row.id))
    assert loaded is not None
    assert loaded.actor_user_id == admin.id
    assert loaded.actor_role == "ADMIN"
    assert loaded.action == "TEST_PROBE_ACTION"
    assert loaded.target_type == "comment"
    assert loaded.reason == "契约测试"
    assert loaded.details == {"key": "value"}
    assert loaded.created_at is not None


@pytest.mark.integration
async def test_writer_persists_the_s30_snapshot_and_request_columns(
    db_session: AsyncSession,
) -> None:
    """0016 (spec §30): ``append`` carries the before/after snapshot pair
    and the request correlation columns onto the row verbatim, and the
    omitted-argument call keeps all four NULL (the non-HTTP caller's
    documented default — workers, service-level tests)."""
    admin = User(
        username="audit-admin-0002",
        password_hash=hash_password(_PASSWORD),
        nickname="审计管理员",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db_session.add(admin)
    await db_session.flush()
    actor = Actor(user_id=admin.id, role=Role.ADMIN)

    row = await AuditLogWriter().append(
        db_session,
        actor=actor,
        action="TEST_PROBE_SNAPSHOT",
        target_type="comment",
        target_id="00000000-0000-0000-0000-000000000002",
        reason="快照契约测试",
        details={"context": "kept"},
        before_snapshot={"status": "OPEN", "points": 10},
        after_snapshot={"status": "HANDLED", "points": 10},
        ip_address="203.0.113.9",
        request_id="writer-test-0001",
    )
    bare = await AuditLogWriter().append(
        db_session,
        actor=actor,
        action="TEST_PROBE_BARE",
        target_type="comment",
        target_id="00000000-0000-0000-0000-000000000003",
    )
    await db_session.commit()

    loaded = await db_session.get(AuditLog, row.id)
    assert loaded is not None
    assert loaded.before_snapshot == {"status": "OPEN", "points": 10}
    assert loaded.after_snapshot == {"status": "HANDLED", "points": 10}
    assert loaded.details == {"context": "kept"}
    assert loaded.ip_address == "203.0.113.9"
    assert loaded.request_id == "writer-test-0001"

    bare_loaded = await db_session.get(AuditLog, bare.id)
    assert bare_loaded is not None
    assert bare_loaded.before_snapshot is None
    assert bare_loaded.after_snapshot is None
    assert bare_loaded.ip_address is None
    assert bare_loaded.request_id is None
