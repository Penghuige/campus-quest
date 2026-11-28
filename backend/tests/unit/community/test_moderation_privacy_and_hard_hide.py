"""Unit tests for B06-03: hard-hidden comment body redaction for non-Admin viewers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.modules.community.serializers import serialize_moderation_comment


def _fake_comment(*, is_hard_hidden: bool) -> SimpleNamespace:
    now = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid4(),
        task_id=uuid4(),
        user_id=uuid4(),
        parent_id=None,
        content="sensitive removed text",
        is_anonymous=True,
        created_at=now,
        updated_at=now,
        deleted_at=now if is_hard_hidden else None,
        is_hard_hidden=is_hard_hidden,
    )


def test_hard_hidden_comment_content_is_redacted_for_non_admin_viewers() -> None:
    dto = serialize_moderation_comment(
        _fake_comment(is_hard_hidden=True),  # type: ignore[arg-type]
        author_nickname="同学甲",
        key_secret="test-secret",
        redact_hard_hidden=True,
    )
    assert dto.hard_hidden is True
    assert dto.content is None


def test_hard_hidden_comment_content_remains_visible_for_admin_viewers() -> None:
    dto = serialize_moderation_comment(
        _fake_comment(is_hard_hidden=True),  # type: ignore[arg-type]
        author_nickname="同学甲",
        key_secret="test-secret",
        redact_hard_hidden=False,
    )
    assert dto.hard_hidden is True
    assert dto.content == "sensitive removed text"
