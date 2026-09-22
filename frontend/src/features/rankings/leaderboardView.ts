/**
 * Pure leaderboard views (spec §17/§17.2, §40; patterns §3/§4).
 *
 * PRIVACY PIN (spec §17/§40): a rendered row carries EXACTLY the four
 * RankingEntry fields — nickname, display_honor, score, rank. The entry
 * has no user-id/student-number/contact field to leak, and this module
 * derives none: `boardRowView` copies the DTO verbatim. The unit tests
 * pin the shape (Object.keys) and the serialized bytes (no UUID-like or
 * student-number-like substrings).
 *
 * Rank semantics (backend rankings/service.py): POSITIONAL ranks (1, 2,
 * 3, …) by score descending; ties keep the server's deterministic order
 * and split positions rather than sharing one. Rows therefore render in
 * SERVER ORDER with the server's numbers verbatim — the view never
 * re-sorts and never recomputes a rank. Because ranks are positional,
 * they are unique per board, which is what makes the around-me anchor
 * (rank equality) unambiguous.
 */
import type {
  BoardDto,
  RankingEntryDto,
  RankingPeriodKey,
} from "@/features/rankings/api";

// --- period tabs (patterns §4: ranking period is URL state) -----------------------

export const RANKING_PERIODS: readonly {
  key: RankingPeriodKey;
  label: string;
}[] = [
  { key: "daily", label: "今日榜" },
  { key: "monthly", label: "本月榜" },
  { key: "all", label: "总榜" },
];

export const DEFAULT_RANKING_PERIOD: RankingPeriodKey = "daily";

const PERIOD_KEYS: ReadonlySet<string> = new Set(
  RANKING_PERIODS.map((period) => period.key),
);

export function isRankingPeriodKey(value: string): value is RankingPeriodKey {
  return PERIOD_KEYS.has(value);
}

/** Parse a ?period= value with the calm default; garbage cannot 500 a page. */
export function parseRankingPeriod(
  value: string | undefined,
  fallback: RankingPeriodKey = DEFAULT_RANKING_PERIOD,
): RankingPeriodKey {
  return value !== undefined && isRankingPeriodKey(value) ? value : fallback;
}

// --- rows (the privacy pin) --------------------------------------------------------

/** EXACTLY the four public RankingEntry fields — nothing derived. */
export interface BoardRowView {
  nickname: string;
  displayHonor: string | null;
  score: number;
  rank: number;
}

/**
 * The display row for one entry: a verbatim copy of the four DTO
 * fields. This function is the single choke point every rendered row
 * goes through, which is what the privacy tests pin.
 */
export function boardRowView(entry: RankingEntryDto): BoardRowView {
  return {
    nickname: entry.nickname,
    displayHonor: entry.display_honor,
    score: entry.score,
    rank: entry.rank,
  };
}

// --- board view -------------------------------------------------------------------

export interface BoardView {
  /** Empty when the board carries no rows and the caller holds no score. */
  status: "empty" | "ready";
  rows: BoardRowView[];
  /** The caller's own standing (server verdict; null while unranked). */
  myRank: number | null;
  myScore: number | null;
  /** Rows in the top list that ARE the caller (rank equality anchor). */
  rowIsMe: boolean[];
}

export function boardView(board: BoardDto): BoardView {
  const rows = board.entries.map(boardRowView);
  return {
    status: rows.length === 0 && board.my_rank === null ? "empty" : "ready",
    rows,
    myRank: board.my_rank ?? null,
    myScore: board.my_score ?? null,
    rowIsMe: rows.map((row) => board.my_rank === row.rank),
  };
}

// --- around-me view ---------------------------------------------------------------

export interface AroundMeRowView extends BoardRowView {
  /** Anchored marker: this row's GLOBAL rank equals the caller's. */
  isMe: boolean;
}

export interface AroundMeView {
  status: "empty" | "ready";
  rows: AroundMeRowView[];
}

/**
 * The caller's neighborhood with the anchor marks. The entries arrive
 * with GLOBAL ranks (a slice of the board, never a re-ranked mini-board)
 * and positional ranks are unique, so `rank === myRank` identifies the
 * caller's own row; a null myRank (no score) marks nobody.
 */
export function aroundMeView(
  entries: RankingEntryDto[],
  myRank: number | null,
): AroundMeView {
  const rows: AroundMeRowView[] = entries.map((entry) => ({
    ...boardRowView(entry),
    isMe: myRank !== null && entry.rank === myRank,
  }));
  return { status: rows.length === 0 ? "empty" : "ready", rows };
}
