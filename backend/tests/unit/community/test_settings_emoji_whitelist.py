# backend/tests/unit/community/test_settings_emoji_whitelist.py
"""``SystemEmojiWhitelistProvider``'s resolution matrix (PR #5 final
review fix B): the row-over-seed priority and the fail-loudly ruling on
a corrupt row — the branches the HTTP composition suite cannot reach
(the settings API normalizes, so corruption is direct-database-only)."""

from __future__ import annotations

import pytest

from app.modules.community.reaction_service import DefaultEmojiWhitelistProvider
from app.modules.community.settings_emoji_whitelist import SystemEmojiWhitelistProvider


def test_no_row_answers_the_spec_eight_seed() -> None:
    """G7: 无行 -> exactly what the default provider answers (the row is
    the fact, the spec §22 eight the seed)."""
    assert (
        SystemEmojiWhitelistProvider(configured_value=None).allowed()
        == DefaultEmojiWhitelistProvider().allowed()
    )


def test_row_members_replace_the_seed_wholesale() -> None:
    """A present row IS the whitelist: removed seed emoji are gone,
    duplicates collapse, and the empty list bans everything (the
    registry's legal empty-list contract)."""
    narrowed = SystemEmojiWhitelistProvider(configured_value='["🎓", "🎓", "🫡"]')
    assert narrowed.allowed() == frozenset({"🎓", "🫡"})
    assert "👍" not in narrowed.allowed()
    assert SystemEmojiWhitelistProvider(configured_value="[]").allowed() == frozenset()


@pytest.mark.parametrize(
    "stored",
    [
        "not json",  # broken JSON
        '"🎓"',  # not an array
        "[1]",  # not an array of strings
        '["😀😀😀😀😀😀😀😀😀"]',  # 9 code points: outside the 1-8 contract
        '[""]',  # 0 code points
    ],
)
def test_corrupt_row_fails_loudly_instead_of_reverting_to_the_seed(stored: str) -> None:
    """A present-but-corrupt row raises at construction — silently
    widening back to the seed would mask a narrowed whitelist."""
    with pytest.raises(ValueError):
        SystemEmojiWhitelistProvider(configured_value=stored)
