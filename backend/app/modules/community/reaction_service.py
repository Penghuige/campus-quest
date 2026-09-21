# backend/app/modules/community/reaction_service.py
"""Emoji reaction toggle (spec §22, §31.9; plan 06 task 5).

Design decisions:

- **Whitelist port (spec §22: V1 emoji 从 Admin 配置白名单选择).** The
  allowed set is read through ``EmojiWhitelistPort`` on every call —
  Plan 08 wires the audited Admin setting behind that seam; until then
  ``DefaultEmojiWhitelistProvider`` answers with the spec's eight
  defaults (👍 ❤️ 😂 🎉 😭 👀 🤔 🔥). Membership is exact-string over the
  configured set, which is also what enforces §22's 不允许 HTML 或图片
  reaction: an HTML snippet, an image reference, a multi-emoji
  sequence, a skin-toned variant, or a bare ❤ without VS16 is simply
  not a member, and only members are ever stored. Refusal is the typed
  ``UnknownEmojiError`` (the VALIDATION_ERROR family — the
  ``InvalidVoteValueError`` precedent), raised BEFORE any database
  touch.
- **Writer gate / comment visibility: the shared community gates
  (gates.py, task 6).** Reacting is a community write:
  ``gates.require_student_writer`` (Student role and ACTIVE status,
  judged on the users ROW, role-first then status) and
  ``gates.require_visible_comment`` (exists -> not a tombstone -> task
  PUBLISHED) — the same typed errors and rulings as comments, votes,
  and reports, extracted when the report service would have become the
  third private copy.
- **Toggle (spec §22: 相同 emoji 重复点击 = toggle).** The per-(comment,
  user, emoji) row IS the reaction: absent -> INSERT (added, True);
  present -> DELETE (removed, False). No value column exists to flip,
  so unlike votes there is no in-place UPDATE — and two DIFFERENT emoji
  coexist because the §31.9 anchor is per emoji, not per user. The
  existing row is locked ``FOR UPDATE`` (one serialization point per
  triple) before the branch is taken.
- **Both-create retry (§31.9).** ``FOR UPDATE`` on an absent row locks
  nothing, so two concurrent toggles from absent both INSERT and the
  UNIQUE(comment_id, user_id, emoji) constraint arbitrates: the loser's
  flush surfaces ``IntegrityError``, the transaction rolls back, and
  exactly ONE retry runs — but it REPLAYS THE ADD INTENT rather than
  re-judging the toggle. The violation only surfaces after the winner
  COMMITTED the identical triple, so the retry's ``FOR UPDATE``
  reliably finds that row and the add is already satisfied: a no-op
  returning True. Re-judging would DELETE the winner's row — turning
  two racing clicks into add-then-remove — which is why the intent is
  carried into the retry instead of being recomputed. A second
  violation, or an IntegrityError naming any other constraint,
  re-raises untouched.
- **Add-vs-remove.** A toggle that judges REMOVE over a row another
  transaction is deleting blocks on the ``FOR UPDATE`` and re-evaluates
  against the committed delete: the row is gone, so it judges ADD and
  inserts. The lock plus §31.9 make "at most one row per triple" hold
  under every interleaving without any retry.
- No clock and no event seam: ``created_at`` is a database ``now()``
  server default and the spec wires no audit stream to plain
  reactions. The service returns the bare bool the plan's interface
  names — the echoing surface (counts per emoji) is the read side's
  job, task 9's router composes it.

Transaction shape per backend-engineering §5: the gate reads, the
locked toggle, and exactly one commit per call.
"""

from __future__ import annotations

import re
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.gates import require_student_writer, require_visible_comment
from app.modules.community.models import CommentReaction

__all__ = [
    "DefaultEmojiWhitelistProvider",
    "EmojiWhitelistPort",
    "ReactionService",
    "UnknownEmojiError",
]

# The (comment_id, user_id, emoji) anchor (spec §31.9) — the only
# IntegrityError this service expects and retries.
_UQ_COMMENT_REACTIONS = "uq_comment_reactions_comment_id_user_id_emoji"

# Driver-agnostic constraint-name extraction, kept identical to the vote
# service's private helper (asyncpg exposes .constraint_name, other
# drivers only the message).
_CONSTRAINT_IN_MESSAGE = re.compile(r'constraint "(?P<name>[^"]+)"')

_UNKNOWN_EMOJI_MESSAGE = "该表情不在允许的表情白名单内"

# spec §22 defaults — the eight emoji V1 ships with; Plan 08's audited
# Admin setting replaces the provider, never this constant's role as the
# documented default.
_DEFAULT_EMOJI = frozenset({"👍", "❤️", "😂", "🎉", "😭", "👀", "🤔", "🔥"})


class UnknownEmojiError(BusinessError):
    """``emoji`` is not a member of the Admin-configured whitelist (spec
    §22) — including HTML, image references, and multi-emoji sequences,
    which no configuration can admit without being explicitly added."""

    def __init__(self, emoji: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _UNKNOWN_EMOJI_MESSAGE,
            status_code=400,
            details={"emoji": emoji},
        )


class EmojiWhitelistPort(Protocol):
    """The Admin-configured emoji whitelist (spec §22). Plan 08's audited
    settings store implements this port; ``DefaultEmojiWhitelistProvider``
    answers until that wiring lands. Sync and cheap by contract — the
    Plan 08 store is a cached read, and the call sits before any
    database work so it can never widen a transaction."""

    def allowed(self) -> frozenset[str]: ...


class DefaultEmojiWhitelistProvider:
    """The V1 default whitelist: the spec §22 eight, frozen per call so a
    caller can never mutate the shared set through the port."""

    def allowed(self) -> frozenset[str]:
        return _DEFAULT_EMOJI


def _constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's name, from the driver or the message."""
    name = getattr(exc.orig, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    match = _CONSTRAINT_IN_MESSAGE.search(str(exc.orig))
    return match.group("name") if match is not None else None


class ReactionService:
    """The emoji reaction toggle (spec §22): ``toggle_reaction`` is the
    whole surface, parameterized by the ``EmojiWhitelistPort`` seam."""

    def __init__(self, whitelist: EmojiWhitelistPort | None = None) -> None:
        self._whitelist: EmojiWhitelistPort = (
            whitelist if whitelist is not None else DefaultEmojiWhitelistProvider()
        )

    async def toggle_reaction(
        self, db: AsyncSession, user_id: UUID, comment_id: UUID, emoji: str
    ) -> bool:
        """Toggle this user's ``emoji`` reaction on one comment: True when
        the reaction was added, False when it was removed. See the module
        docstring for the whitelist, gate, locking, and retry rulings."""
        if emoji not in self._whitelist.allowed():
            raise UnknownEmojiError(emoji)
        # One retry covers both-create-from-absent, and it REPLAYS the add
        # intent (judge=False): the violation proves an identical triple
        # already committed, so the retry's locked read finds the twin row
        # and confirms the add instead of toggling it away.
        for attempt in (0, 1):
            try:
                return await self._attempt(
                    db, user_id, comment_id, emoji, judge=attempt == 0
                )
            except IntegrityError as exc:
                if attempt == 1 or _constraint_name(exc) != _UQ_COMMENT_REACTIONS:
                    raise
                await db.rollback()
        raise AssertionError("unreachable: the loop returns or raises")

    # -- internals ----------------------------------------------------------------

    async def _attempt(
        self,
        db: AsyncSession,
        user_id: UUID,
        comment_id: UUID,
        emoji: str,
        *,
        judge: bool,
    ) -> bool:
        """One full locked attempt: gates -> FOR UPDATE -> apply ->
        commit. ``judge=True`` (the caller's first attempt) reads the row
        to decide add-vs-remove; ``judge=False`` (the retry) forces the
        add intent — the only branch whose INSERT can violate the §31.9
        triple, re-entered exactly once after that violation."""
        await require_student_writer(db, user_id)
        await require_visible_comment(db, comment_id)

        reaction = await db.scalar(
            select(CommentReaction)
            .where(
                CommentReaction.comment_id == comment_id,
                CommentReaction.user_id == user_id,
                CommentReaction.emoji == emoji,
            )
            .with_for_update()
        )
        add = reaction is None if judge else True
        if add:
            if reaction is None:
                # absent -> INSERT; the UNIQUE anchor arbitrates the race.
                db.add(
                    CommentReaction(comment_id=comment_id, user_id=user_id, emoji=emoji)
                )
                await db.flush()
            # present under the forced add intent: the twin's committed
            # row already IS this caller's reaction — nothing to do.
            await db.commit()
            return True
        if reaction is not None:
            # present -> DELETE: "removed" is the absence of the row.
            await db.execute(
                delete(CommentReaction).where(CommentReaction.id == reaction.id)
            )
        await db.commit()
        return False
