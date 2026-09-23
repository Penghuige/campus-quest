# backend/app/modules/community/settings_emoji_whitelist.py
"""The store-backed emoji whitelist (PR #5 final review fix B: the data
plane consumes the audited Admin setting, spec §22).

``ReactionService`` judges membership through the ``EmojiWhitelistPort``
seam; until this module the production composition injected
``DefaultEmojiWhitelistProvider`` (the spec §22 eight), so the two Admin
keys written through ``/api/v1/admin/settings`` were auditable but never
consumed — the control-plane/data-plane split the final review rejected.

Priority — G7: the ``system_settings`` EMOJI_WHITELIST row is the FACT;
``DefaultEmojiWhitelistProvider``'s spec-eight set is only the INITIAL
SEED a deployment boots with, never an override:

1. row present -> the row's whitelist (set through the audited Admin
   settings API; the EMPTY list is legal and bans every emoji);
2. no row -> the spec §22 default eight.

The storage read is async but ``EmojiWhitelistPort`` is sync (the
reaction service calls it before any database work), so the composition
root's dependency resolves the row ONCE per request and builds this
provider with the answer (community/router.py) — the
``SystemAcademicTermProvider`` shape. The priority and validation RULE
lives here, in the provider family, so no composition root can get it
wrong.

A present-but-corrupt row (not a JSON array of 1-8-code-point strings —
unreachable through the API, which normalizes, so only direct database
edits get there) fails LOUDLY at construction instead of silently
falling back to the seed: masking a corrupted setting would quietly
re-widen the whitelist the Admin believes they narrowed.
"""

from __future__ import annotations

import json

from app.modules.community.reaction_service import DefaultEmojiWhitelistProvider

__all__ = ["SystemEmojiWhitelistProvider"]

# Mirrors the write-side registry contract (system/service.py's
# ``_EMOJI_MIN_CODE_POINTS``/``_EMOJI_MAX_CODE_POINTS``): a member is one
# grapheme cluster's worth of code points, never a multi-emoji sequence.
_MIN_CODE_POINTS = 1
_MAX_CODE_POINTS = 8


def _members_from(stored: str) -> frozenset[str]:
    """The row's members, or ``ValueError`` for any spelling the write
    side's normalizer would have refused (the fail-loudly ruling above)."""
    try:
        parsed = json.loads(stored)
    except ValueError as exc:
        raise ValueError(f"EMOJI_WHITELIST row is not valid JSON: {stored!r}") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise ValueError(
            f"EMOJI_WHITELIST row is not a JSON array of strings: {stored!r}"
        )
    for item in parsed:
        if not (_MIN_CODE_POINTS <= len(item) <= _MAX_CODE_POINTS):
            raise ValueError(
                "EMOJI_WHITELIST row holds an entry outside the 1-8 "
                f"code-point contract: {item!r}"
            )
    return frozenset(parsed)


class SystemEmojiWhitelistProvider:
    """The production ``EmojiWhitelistPort``: the audited EMOJI_WHITELIST
    row over the spec §22 default seed (G7; see the module docstring).
    Structural conformance to the port — frozen per construction, so a
    caller can never mutate the configured set through it."""

    def __init__(self, *, configured_value: str | None) -> None:
        if configured_value is None:
            # G7: 无行 → the deployment seed, i.e. exactly what the
            # default provider answers (store is the fact, default is
            # the seed).
            self._allowed: frozenset[str] = DefaultEmojiWhitelistProvider().allowed()
        else:
            self._allowed = _members_from(configured_value)

    def allowed(self) -> frozenset[str]:
        return self._allowed
