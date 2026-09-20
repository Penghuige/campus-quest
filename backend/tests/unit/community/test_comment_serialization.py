# backend/tests/unit/community/test_comment_serialization.py
"""Comment serialization safety and content normalization (spec §21.1,
§21.4, §40; plan 06 tasks 2-3).

Unit-level, no database: the serializers and the pure content normalizer
are exercised directly so the privacy and XSS posture does not depend on
PostgreSQL being reachable.

- Anonymous comments render the author as exactly ``匿名用户``; the public
  DTO has NO ``user_id`` field at all, and a full JSON dump of the DTO
  carries none of the author's known identity facts (student number /
  username, phone, email, raw user id) — spec §40 公开页面不得暴露.
- Non-anonymous comments show the nickname and nothing else identity-wise.
- The moderation DTO (Task 8 placeholder) carries no author identity
  either; ``moderation_key`` stays a None seam until that task lands, and
  ``hard_hidden`` (task 3) exposes the Admin hard-hide flag for
  moderation review without resurrecting public visibility.
- Tombstones (task 3, spec §21.3 该评论已删除): a soft-deleted parent kept
  for thread anchoring serializes with null content and the uniform
  deleted display — for anonymous AND named authors alike.
- Content normalization (spec §21.1): dangerous control characters are
  stripped (newlines and tabs survive), whitespace-only content is
  rejected, and the configurable length cap rejects over-limit content.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.comment_service import (
    DEFAULT_COMMENT_MAX_LENGTH,
    normalize_comment_content,
)
from app.modules.community.models import Comment
from app.modules.community.schemas import CommentPublic, ModerationComment
from app.modules.community.serializers import (
    ANONYMOUS_AUTHOR_DISPLAY,
    DELETED_COMMENT_DISPLAY,
    serialize_moderation_comment,
    serialize_public_comment,
    serialize_tombstone_comment,
)

_T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

# Known identity facts of the fictional author. The leakage assertions
# check that NONE of these survive public serialization.
_STUDENT_NUMBER = "20250010001"
_PHONE = "+8613800138000"
_EMAIL = "author@pku.edu.cn"

_PUBLIC_FIELDS = {
    "id",
    "task_id",
    "parent_id",
    "content",
    "is_anonymous",
    "author_display",
    "created_at",
    "updated_at",
    "edited",
    "deleted",
}

_MODERATION_FIELDS = (_PUBLIC_FIELDS - {"author_display"}) | {
    "moderation_key",
    "hard_hidden",
}


def _comment(*, is_anonymous: bool, content: str = "这条任务说明很清楚。") -> Comment:
    """An in-memory Comment row carrying the author's real identity (the
    service ALWAYS stores user_id, spec §21.4); serialization decides what
    is visible."""
    return Comment(
        task_id=uuid4(),
        user_id=uuid4(),
        content=content,
        is_anonymous=is_anonymous,
        created_at=_T0,
        updated_at=_T0,
    )


def _deleted_comment(*, is_anonymous: bool) -> Comment:
    comment = _comment(is_anonymous=is_anonymous, content="被删除的内容")
    comment.deleted_at = _T0
    comment.deleted_by = uuid4()
    comment.delete_reason = "owner"
    return comment


def _identity_facts(user_id: object) -> list[str]:
    return [_STUDENT_NUMBER, _PHONE, _EMAIL, str(user_id)]


# --- anonymous leakage (brief step 1) -----------------------------------------------


def test_anonymous_comment_serializes_author_as_anonymous_display() -> None:
    comment = _comment(is_anonymous=True)
    public = serialize_public_comment(comment, author_nickname="小北")

    assert public.author_display == ANONYMOUS_AUTHOR_DISPLAY == "匿名用户"
    assert public.is_anonymous is True


def test_anonymous_comment_json_leaks_no_identity_facts() -> None:
    comment = _comment(is_anonymous=True)
    public = serialize_public_comment(comment, author_nickname="小北")

    payload = json.dumps(dataclasses.asdict(public), default=str)
    for fact in _identity_facts(comment.user_id):
        assert fact not in payload


def test_public_dto_has_no_user_id_field_at_all() -> None:
    # Not even for non-anonymous comments: identity travels as the display
    # nickname only (spec §40), never the raw user_id.
    assert {field.name for field in dataclasses.fields(CommentPublic)} == _PUBLIC_FIELDS
    assert "user_id" not in _PUBLIC_FIELDS


def test_non_anonymous_comment_shows_nickname_only() -> None:
    comment = _comment(is_anonymous=False)
    public = serialize_public_comment(comment, author_nickname="小北")

    assert public.author_display == "小北"
    payload = json.dumps(dataclasses.asdict(public), default=str)
    for fact in _identity_facts(comment.user_id):
        assert fact not in payload


def test_public_dto_carries_thread_and_state_shape() -> None:
    parent = uuid4()
    comment = _comment(is_anonymous=False)
    comment.parent_id = parent
    public = serialize_public_comment(comment, author_nickname="小北", edited=True)

    assert public.parent_id == parent
    assert public.edited is True
    assert public.deleted is False


def test_moderation_dto_has_no_author_identity() -> None:
    # The Task 8 placeholder shape: everything public except the author
    # display, plus a None moderation_key seam and the task-3 hard_hidden
    # flag. No nickname, no user_id — the pseudonymous key is Task 8's to
    # derive.
    comment = _comment(is_anonymous=True)
    moderation = serialize_moderation_comment(comment)

    assert {field.name for field in dataclasses.fields(ModerationComment)} == (
        _MODERATION_FIELDS
    )
    assert moderation.moderation_key is None
    assert moderation.hard_hidden is False  # live comments: flag unset
    payload = json.dumps(dataclasses.asdict(moderation), default=str)
    for fact in _identity_facts(comment.user_id):
        assert fact not in payload


# --- tombstones (task 3, spec §21.3 该评论已删除) ------------------------------------


@pytest.mark.parametrize("is_anonymous", [True, False])
def test_tombstone_serializes_null_content_and_uniform_display(
    is_anonymous: bool,
) -> None:
    """A deleted parent kept for thread anchoring carries NO content and
    the uniform deleted display — the deleted author's identity (named or
    anonymous) is subsumed by the marker."""
    comment = _deleted_comment(is_anonymous=is_anonymous)
    tombstone = serialize_tombstone_comment(comment)

    assert tombstone.deleted is True
    assert tombstone.content is None
    assert tombstone.author_display == DELETED_COMMENT_DISPLAY == "该评论已删除"
    assert tombstone.edited is False  # edit history is moot once content is gone
    assert tombstone.id == comment.id
    assert tombstone.parent_id == comment.parent_id


@pytest.mark.parametrize("is_anonymous", [True, False])
def test_tombstone_json_leaks_no_identity_facts(is_anonymous: bool) -> None:
    comment = _deleted_comment(is_anonymous=is_anonymous)
    tombstone = serialize_tombstone_comment(comment)

    payload = json.dumps(dataclasses.asdict(tombstone), default=str)
    for fact in _identity_facts(comment.user_id):
        assert fact not in payload
    assert comment.content not in payload  # the deleted content itself is gone


# --- content normalization (spec §21.1) ----------------------------------------------


def test_normalization_strips_dangerous_control_characters() -> None:
    # NUL, carriage return, escape, and DEL are removed; newline and tab
    # survive (multi-line comments stay expressible).
    normalized = normalize_comment_content("第一行\r\n第二行\x00\x1b\x7f", 2000)
    assert normalized == "第一行\n第二行"


def test_normalization_trims_surrounding_whitespace() -> None:
    assert normalize_comment_content("  正文 \n", 2000) == "正文"


def test_whitespace_only_content_is_rejected() -> None:
    with pytest.raises(BusinessError) as raised:
        normalize_comment_content(" \n\t\r\x00 ", 2000)
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400


def test_over_limit_content_is_rejected_at_the_configured_cap() -> None:
    with pytest.raises(BusinessError) as raised:
        normalize_comment_content("字" * (DEFAULT_COMMENT_MAX_LENGTH + 1), 2000)
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.details == {"max_length": 2000}

    # Exactly at the cap passes; the boundary is inclusive.
    assert (
        normalize_comment_content("字" * DEFAULT_COMMENT_MAX_LENGTH, 2000)
        == "字" * DEFAULT_COMMENT_MAX_LENGTH
    )


def test_length_cap_is_configurable() -> None:
    # The cap arrives from Settings at the composition root; a deployment
    # may tighten it. 2000 is only the default, not a constant of the rule.
    with pytest.raises(BusinessError):
        normalize_comment_content("123456", 5)
    assert normalize_comment_content("12345", 5) == "12345"


def test_non_string_content_is_rejected() -> None:
    with pytest.raises(BusinessError):
        normalize_comment_content(None, 2000)  # type: ignore[arg-type]


# --- the Settings field (spec §21.1 可配置; default 2000) ----------------------------


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def test_settings_comment_max_length_defaults_to_2000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Lives in the community gate (not tests/unit/core) because the field
    # is community-owned: spec §21.1 fixes the default at 2000 and makes it
    # configurable. Parity with the service default is the deployment copy.
    _set_required_env(monkeypatch)
    settings = Settings()
    assert settings.comment_max_length == DEFAULT_COMMENT_MAX_LENGTH == 2000


def test_settings_rejects_unusable_comment_max_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cap below 1 would reject every comment while still burning the
    # rate-limit budget (task 9); a deployment wanting that should disable
    # the surface, not set an unusable quota.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("COMMENT_MAX_LENGTH", "0")
    with pytest.raises(ValidationError, match="comment_max_length"):
        Settings()
