"use client";
/**
 * Leaderboard island (spec §17/§17.2; patterns §8 Rankings archetype):
 * period tabs -> top list -> around-me. The period lives in the URL
 * (`/rankings?period=daily|monthly|all`, patterns §4 shareable tabs) —
 * the page parses it server-side and REMOUNTS this island per period,
 * so each tab is a plain link and every fetch starts from the tab, not
 * from client state.
 *
 * Rows render EXACTLY the four public RankingEntry fields through
 * `boardRowView` (spec §17/§40 — nickname / honor / score / rank in
 * SERVER order, server ranks verbatim); the caller's own rows are
 * anchored by rank equality and marked with a non-color "我" cue
 * (design §9 Leaderboard: current-user highlight, never danger styling
 * for low rank).
 *
 * Hierarchy contract (plan-13 T2 / plan-11 P2, owner 2026-09-24):
 * restrained top-3 distinction (rank-numeral weight + one subtle amber
 * keyline tint via `hasTopDistinction` — a presentation read of the
 * server's verbatim rank, NO podium), and a strong 我 anchor — the
 * brand-washed, strong-keylined current-user row reads identically in
 * the top list and 我的附近. When the caller is OUTSIDE the top list
 * (`boardView().meInTopList === false`), the around-me panel re-states
 * their global rank as a quiet lead so the neighborhood reads as one
 * continuous story: above rows, the highlighted own row, below rows.
 * The period tabs are one segmented control (behavior and URLs
 * unchanged). Honor renders as quiet text next to the nickname (the
 * DTO's own display_honor, verbatim).
 */
import Link from "next/link";

import {
  EmptyState,
  SectionError,
  SectionHeading,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import {
  aroundMeBoard,
  boardForPeriod,
  type BoardDto,
  type RankingPeriodKey,
} from "@/features/rankings/api";
import {
  aroundMeView,
  boardView,
  hasTopDistinction,
  RANKING_PERIODS,
} from "@/features/rankings/leaderboardView";

export function Leaderboard({ period }: { period: RankingPeriodKey }) {
  const board = useSection(() => boardForPeriod(period), `GET /api/v1/rankings/${period}`);
  const around = useSection(() => aroundMeBoard(period), `GET /api/v1/rankings/${period}/around-me`);

  const meInTopList =
    board.state.status === "ready" ? boardView(board.state.data).meInTopList : null;

  return (
    <div className="rankings-view">
      <nav className="tab-bar segmented-tabs" aria-label="排行周期">
        {RANKING_PERIODS.map((tab) => (
          <Link
            key={tab.key}
            href={`/rankings?period=${tab.key}`}
            className="tab-link"
            aria-current={tab.key === period ? "page" : undefined}
          >
            {tab.label}
          </Link>
        ))}
      </nav>

      <BoardSection
        status={board.state.status}
        error={board.state.status === "error" ? board.state.error : null}
        data={board.state.status === "ready" ? board.state.data : null}
        retry={board.retry}
      />
      <AroundMeSection
        status={around.state.status}
        error={around.state.status === "error" ? around.state.error : null}
        myRank={board.state.status === "ready" ? board.state.data.my_rank ?? null : null}
        meInTopList={meInTopList}
        entries={around.state.status === "ready" ? around.state.data.entries : null}
        retry={around.retry}
      />
    </div>
  );
}

// --- top list ------------------------------------------------------------------------

function BoardSection({
  status,
  error,
  data,
  retry,
}: {
  status: "loading" | "ready" | "error";
  error: unknown;
  data: BoardDto | null;
  retry: () => void;
}) {
  const view = data === null ? null : boardView(data);
  return (
    <section className="section" aria-label="排行榜">
      <SectionHeading title="排行榜" />
      {status === "loading" ? <SectionSkeleton lines={5} /> : null}
      {status === "error" ? <SectionError error={error} onRetry={retry} /> : null}
      {status === "ready" && view !== null ? (
        view.status === "empty" ? (
          <EmptyState
            title="本期暂无排名"
            hint="完成任务获得积分后，榜单和你的名次会在这里出现"
          />
        ) : (
          <div className="panel">
            {view.myRank !== null ? (
              <p className="board-my-standing">
                我的名次：第 <span className="meta-num">{view.myRank}</span> 名
                {view.myScore !== null ? (
                  <span className="metric-unit"> · {view.myScore} 积分</span>
                ) : null}
              </p>
            ) : (
              <p className="board-my-standing">暂未上榜，完成任务后即可登上榜单</p>
            )}
            <ol className="board-rows">
              {view.rows.map((row, index) => (
                <BoardRow
                  key={row.rank}
                  row={row}
                  isMe={view.rowIsMe[index] === true}
                />
              ))}
            </ol>
          </div>
        )
      ) : null}
    </section>
  );
}

/**
 * One public row: nickname + honor + score + rank, nothing else (§40).
 * Top-3 rows carry `data-top3` (the restrained distinction), the
 * caller's own row `data-me` (the strong anchor). Exported for the
 * dev-only component gallery's fixture composition (plan-13 T2): the
 * thin e2e world's single-entity board can never render top-3 rows or
 * an outside-top around-me window, so those variants are reviewable
 * (and pixel-baselined) only through the gallery.
 */
export function BoardRow({
  row,
  isMe,
}: {
  row: { nickname: string; displayHonor: string | null; score: number; rank: number };
  isMe: boolean;
}) {
  return (
    <li
      className="board-row"
      data-me={isMe ? "true" : undefined}
      data-top3={hasTopDistinction(row.rank) ? "true" : undefined}
    >
      <span className="board-rank meta-num">#{row.rank}</span>
      <span className="board-nick">
        {row.nickname}
        {row.displayHonor !== null ? (
          <span className="board-honor">{row.displayHonor}</span>
        ) : null}
        {isMe ? <span className="board-me-tag">我</span> : null}
      </span>
      <span className="board-score meta-num">{row.score}</span>
    </li>
  );
}

// --- around me -----------------------------------------------------------------------

function AroundMeSection({
  status,
  error,
  myRank,
  meInTopList,
  entries,
  retry,
}: {
  status: "loading" | "ready" | "error";
  error: unknown;
  myRank: number | null;
  /** The top list's verdict on the caller (null until the board loads):
   * only a caller OUTSIDE the top list gets the quiet standing lead —
   * inside the top list the board's own 我的名次 line already says it. */
  meInTopList: boolean | null;
  entries:
    | { nickname: string; display_honor: string | null; score: number; rank: number }[]
    | null;
  retry: () => void;
}) {
  const view = entries === null ? null : aroundMeView(entries, myRank);
  return (
    <section className="section" aria-label="我的附近">
      <SectionHeading title="我的附近" />
      {status === "loading" ? <SectionSkeleton lines={3} /> : null}
      {status === "error" ? <SectionError error={error} onRetry={retry} /> : null}
      {status === "ready" && view !== null ? (
        view.status === "empty" ? (
          <EmptyState
            title="暂无附近排名"
            hint="获得积分后，这里会显示你前后的同学与你自己的位置"
          />
        ) : (
          <div className="panel">
            {/* One continuous story (plan-13 T2): the quiet lead pins the
                caller's global rank, then above rows -> the highlighted
                own row -> below rows read as a single neighborhood. */}
            {meInTopList === false && myRank !== null ? (
              <p className="board-around-lead">
                我的名次：第 <span className="meta-num">{myRank}</span> 名 ·
                你前后的同学
              </p>
            ) : null}
            <ol className="board-rows">
              {view.rows.map((row) => (
                <BoardRow key={row.rank} row={row} isMe={row.isMe} />
              ))}
            </ol>
          </div>
        )
      ) : null}
    </section>
  );
}
