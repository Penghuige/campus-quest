import type { Metadata } from "next";

import { Leaderboard } from "@/features/rankings/Leaderboard";
import {
  DEFAULT_RANKING_PERIOD,
  parseRankingPeriod,
} from "@/features/rankings/leaderboardView";

export const metadata: Metadata = {
  title: "排行榜 · CampusQuest",
  description: "今日榜、本月榜、总榜与我的附近",
};

interface RankingsPageProps {
  searchParams: Promise<{ period?: string | string[] }>;
}

/**
 * Rankings page (spec §17/§42; patterns §4 — the period is URL state).
 * The tab is a search param so boards are shareable; the server parses
 * it (garbage degrades to the default) and REMOUNTS the island per
 * period, so every fetch keys off the tab, never client state.
 */
export default async function RankingsPage({ searchParams }: RankingsPageProps) {
  const params = await searchParams;
  const raw = params.period;
  const period = parseRankingPeriod(
    Array.isArray(raw) ? raw[0] : raw,
    DEFAULT_RANKING_PERIOD,
  );

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">排行榜</h1>
        <p className="page-subtitle">今日榜、本月榜、总榜与我的附近</p>
      </div>
      <Leaderboard key={period} period={period} />
    </>
  );
}
