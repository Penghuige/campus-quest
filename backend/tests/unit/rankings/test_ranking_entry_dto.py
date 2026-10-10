# backend/tests/unit/rankings/test_ranking_entry_dto.py
"""The public leaderboard row's frozen shape (spec §17/§40; G-1 gap).

The leaderboard displays nickname + honor ONLY. `RankingEntryResponse`
enforces that by construction (no field for anything else, extra=
forbid); this freeze test pins the field SET so a future field addition
— student number, contact, user id, internal score components — fails a
test instead of shipping silently. Pattern: identity's
`test_user_public_excludes_private_fields`.
"""

from __future__ import annotations

from app.modules.rankings.router import RankingEntryResponse


def test_ranking_entry_response_freezes_the_public_shape() -> None:
    # Privacy by construction (spec §40: 排行榜显示 nickname + honor):
    # the DTO enumerates its fields, so nothing else can leak into a
    # public board row even if the underlying read model grows columns.
    exposed = set(RankingEntryResponse.model_fields)
    assert exposed == {
        "nickname",
        "display_honor",
        "score",
        "rank",
    }
    # The spec §40 never-exposed names, spelled out for the failure
    # message's sake.
    assert "student_number" not in exposed
    assert "phone_e164" not in exposed
    assert "email_normalized" not in exposed
    assert "user_id" not in exposed
