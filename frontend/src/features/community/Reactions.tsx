"use client";
/**
 * Per-comment engagement controls (spec §22): like/dislike votes and
 * whitelisted emoji reactions.
 *
 * PESSIMISTIC BY CHOICE (patterns §7 allows optimistic vote/reaction
 * with rollback; this surface deliberately does not use it): the
 * comment contract has NO viewer-stance read — `CommentPublicResponse`
 * carries neither the caller's current vote/reactions nor any counts —
 * so a first tap cannot predict add-vs-remove, and an optimistic
 * toggle would have to guess. Each tap instead POSTs and the response
 * echo IS the state: `VoteResult` (current_value/likes/dislikes) and
 * `ReactionResult` (added + per-emoji counts) are authoritative and
 * overwrite local state directly — one round trip, no rollback path
 * to get wrong. Buttons show inline busy + typed failure copy.
 *
 * EMOJI WHITELIST (spec §22): the eight V1 defaults render from the
 * `DEFAULT_EMOJI_WHITELIST` mirror; the Admin-configurable server set
 * stays the authority (an off-list state answers typed
 * VALIDATION_ERROR copy via `describeCommunityError`).
 */
import { useState } from "react";

import {
  castCommentVote,
  DEFAULT_EMOJI_WHITELIST,
  toggleCommentReaction,
  type ReactionResultDto,
  type VoteResultDto,
} from "./api";
import { describeCommunityError, nextVoteValue, type VoteDirection } from "./communityView";

/** One shared inline failure line (typed copy; request id on system faults). */
function InteractionError({ error }: { error: unknown }) {
  if (error === null) {
    return null;
  }
  const view = describeCommunityError(error);
  // A span (not p): this rides inside the .vote-group/.reaction-bar
  // phrasing container — valid nesting keeps the DOM stable.
  return (
    <span className="interaction-error" role="alert">
      {view.message}
      {view.requestId !== null ? `（请求 ID：${view.requestId}）` : ""}
    </span>
  );
}

export interface CommentVotesProps {
  commentId: string;
}

/**
 * Like/dislike with count displays. Counts appear once known — the
 * first interaction's echo — because inventing zeros for unread state
 * would present fabricated data (patterns §3).
 */
export function CommentVotes({ commentId }: CommentVotesProps) {
  // null = the viewer's stance and the totals are not loaded (no
  // initial read exists in the contract); filled by the first echo.
  const [state, setState] = useState<VoteResultDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function vote(direction: VoteDirection) {
    if (busy) {
      return;
    }
    setError(null);
    setBusy(true);
    try {
      // Toggle semantics live server-side; `nextVoteValue` only shapes
      // the request from the last known stance (0 = no stance yet).
      setState(
        await castCommentVote(
          commentId,
          nextVoteValue(state?.current_value ?? 0, direction),
        ),
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="vote-group" aria-label="点赞或点踩">
      <button
        type="button"
        className="vote-btn"
        aria-pressed={state?.current_value === 1}
        aria-label="赞"
        disabled={busy}
        onClick={() => void vote(1)}
      >
        <span aria-hidden="true">👍</span>
        <span className="vote-count">{state !== null ? state.likes : ""}</span>
      </button>
      <button
        type="button"
        className="vote-btn"
        aria-pressed={state?.current_value === -1}
        aria-label="踩"
        disabled={busy}
        onClick={() => void vote(-1)}
      >
        <span aria-hidden="true">👎</span>
        <span className="vote-count">{state !== null ? state.dislikes : ""}</span>
      </button>
      <InteractionError error={error} />
    </span>
  );
}

export interface CommentReactionsProps {
  commentId: string;
}

/**
 * Whitelisted emoji reactions with aggregated per-emoji counts. The
 * same emoji again is the toggle (spec §22); `mine` tracks the echoed
 * `added` verdicts so pressed-state stays truthful within a session.
 */
export function CommentReactions({ commentId }: CommentReactionsProps) {
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [mine, setMine] = useState<ReadonlySet<string>>(new Set());
  const [busyEmoji, setBusyEmoji] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function toggle(emoji: string) {
    if (busyEmoji !== null) {
      return;
    }
    setError(null);
    setBusyEmoji(emoji);
    try {
      const result: ReactionResultDto = await toggleCommentReaction(
        commentId,
        emoji,
      );
      setCounts(result.counts);
      setMine((previous) => {
        const next = new Set(previous);
        if (result.added) {
          next.add(result.emoji);
        } else {
          next.delete(result.emoji);
        }
        return next;
      });
    } catch (cause) {
      setError(cause);
    } finally {
      setBusyEmoji(null);
    }
  }

  return (
    <span className="reaction-bar" aria-label="表情反应">
      {DEFAULT_EMOJI_WHITELIST.map((emoji) => {
        const count = counts?.[emoji] ?? null;
        return (
          <button
            key={emoji}
            type="button"
            className="reaction-btn"
            aria-pressed={mine.has(emoji)}
            aria-label={`表情 ${emoji}`}
            disabled={busyEmoji !== null}
            onClick={() => void toggle(emoji)}
          >
            <span aria-hidden="true">{emoji}</span>
            {count !== null ? (
              <span className="reaction-count">{count}</span>
            ) : null}
          </button>
        );
      })}
      <InteractionError error={error} />
    </span>
  );
}
