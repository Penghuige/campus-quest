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
  RANKING_PERIODS,
} from "@/features/rankings/leaderboardView";

export function Leaderboard({ period }: { period: RankingPeriodKey }) {
  const board = useSection(() => boardForPeriod(period));
  const around = useSection(() => aroundMeBoard(period));

  return (
    <>
      <nav className="tab-bar" aria-label="排行周期">
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
        entries={around.state.status === "ready" ? around.state.data.entries : null}
        retry={around.retry}
      />
    </>
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

/** One public row: nickname + honor + score + rank, nothing else (§40). */
function BoardRow({
  row,
  isMe,
}: {
  row: { nickname: string; displayHonor: string | null; score: number; rank: number };
  isMe: boolean;
}) {
  return (
    <li className="board-row" data-me={isMe ? "true" : undefined}>
      <span className="board-rank meta-num">#{row.rank}</span>
      <span className="board-nick">
        {row.nickname}
        {row.displayHonor !== null ? (
          <span className="honor-chip">{row.displayHonor}</span>
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
  entries,
  retry,
}: {
  status: "loading" | "ready" | "error";
  error: unknown;
  myRank: number | null;
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
          <ol className="board-rows">
            {view.rows.map((row) => (
              <BoardRow key={row.rank} row={row} isMe={row.isMe} />
            ))}
          </ol>
        )
      ) : null}
    </section>
  );
}
