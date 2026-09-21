/**
 * Task 5 (Plan 09): leaderboard views — period parsing, board rows in
 * SERVER order with server ranks verbatim (positional ranks; ties keep
 * the server's order), around-me anchoring, and the §17/§40 PRIVACY
 * PIN (rows carry exactly nickname/honor/score/rank; nothing derived).
 * Also pins the rankings/growth wrappers' wire paths via stubbed fetch.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  allTimeBoard,
  aroundMeBoard,
  boardForPeriod,
  dailyBoard,
  myGrowth,
  type BoardDto,
  type RankingEntryDto,
} from "../features/rankings/api";
import {
  aroundMeView,
  boardRowView,
  boardView,
  parseRankingPeriod,
  RANKING_PERIODS,
} from "../features/rankings/leaderboardView";

type RecordedRequest = { url: string; method: string };
let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

function entry(overrides: Partial<RankingEntryDto>): RankingEntryDto {
  return {
    nickname: "同学甲",
    display_honor: null,
    score: 100,
    rank: 1,
    ...overrides,
  };
}

function board(entries: RankingEntryDto[], myRank: number | null, myScore: number | null) {
  return JSON.stringify({ entries, my_rank: myRank, my_score: myScore });
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("period tabs (URL state)", () => {
  test("the three board names in display order", () => {
    assert.deepEqual(
      RANKING_PERIODS.map((period) => period.key),
      ["daily", "monthly", "all"],
    );
  });

  test("parseRankingPeriod accepts known keys, defaults garbage", () => {
    assert.equal(parseRankingPeriod("daily"), "daily");
    assert.equal(parseRankingPeriod(undefined), "daily");
    assert.equal(parseRankingPeriod("weekly"), "daily");
    assert.equal(parseRankingPeriod(""), "daily");
    assert.equal(parseRankingPeriod("weekly", "monthly"), "monthly");
  });
});

describe("board view (server order, server ranks)", () => {
  const entries = [
    entry({ nickname: "甲", score: 900, rank: 1, display_honor: "月度之星" }),
    entry({ nickname: "乙", score: 850, rank: 2 }),
    entry({ nickname: "丙", score: 850, rank: 3 }),
  ];

  test("rows render in SERVER order with ranks verbatim (ties split positions)", () => {
    const dto: BoardDto = { entries, my_rank: null, my_score: null };
    const view = boardView(dto);
    assert.equal(view.status, "ready");
    assert.deepEqual(
      view.rows.map((row) => [row.rank, row.score, row.nickname]),
      [
        [1, 900, "甲"],
        [2, 850, "乙"],
        [3, 850, "丙"],
      ],
    );
    // The tied pair keeps the server's order — never re-sorted locally.
    assert.equal(view.rows[1].nickname, "乙");
    assert.equal(view.rows[2].nickname, "丙");
  });

  test("own standing strips through; a board row that IS me is marked", () => {
    const view = boardView({ entries, my_rank: 2, my_score: 850 });
    assert.equal(view.myRank, 2);
    assert.equal(view.myScore, 850);
    assert.deepEqual(view.rowIsMe, [false, true, false]);
  });

  test("no rows and no own score -> empty; own score alone keeps ready", () => {
    assert.equal(boardView({ entries: [], my_rank: null, my_score: null }).status, "empty");
    assert.equal(boardView({ entries: [], my_rank: 4, my_score: 10 }).status, "ready");
  });
});

describe("around-me anchoring", () => {
  const entries = [
    entry({ nickname: "丁", score: 700, rank: 6 }),
    entry({ nickname: "我", score: 650, rank: 7 }),
    entry({ nickname: "戊", score: 600, rank: 8 }),
  ];

  test("the row whose GLOBAL rank equals myRank is the anchor", () => {
    const view = aroundMeView(entries, 7);
    assert.equal(view.status, "ready");
    assert.deepEqual(view.rows.map((row) => row.isMe), [false, true, false]);
    // Global ranks ride the slice verbatim (never re-ranked 1..n).
    assert.deepEqual(view.rows.map((row) => row.rank), [6, 7, 8]);
  });

  test("a caller with no score marks nobody; empty window stays empty", () => {
    const view = aroundMeView(entries, null);
    assert.equal(view.rows.every((row) => !row.isMe), true);
    assert.equal(aroundMeView([], null).status, "empty");
  });
});

describe("PRIVACY PIN (spec §17/§40)", () => {
  const entries = [
    entry({ nickname: "20240001", score: 900, rank: 1 }),
    entry({ nickname: "乙", score: 850, rank: 2, display_honor: "月度之星" }),
  ];

  test("a row object carries EXACTLY the four public fields", () => {
    for (const source of entries) {
      const row = boardRowView(source);
      assert.deepEqual(Object.keys(row), ["nickname", "displayHonor", "score", "rank"]);
    }
  });

  test("no view path can introduce id-like or student-number-like values", () => {
    const views = [
      JSON.stringify(boardView({ entries, my_rank: 2, my_score: 850 })),
      JSON.stringify(aroundMeView(entries, 2)),
    ];
    for (const serialized of views) {
      // A UUID anywhere in the serialized view would be a leaked user id.
      assert.equal(
        /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}/.test(serialized),
        false,
      );
    }
    // Even a nickname that LOOKS like a student number is the server's
    // own display value — the view copies it verbatim and adds nothing.
    const row = boardRowView(entries[0]);
    assert.equal(row.nickname, "20240001");
  });
});

describe("rankings/growth wrappers hit the exact endpoints", () => {
  test("board endpoints dispatch by period with the board size", async () => {
    stubFetch(board([], null, null));
    await dailyBoard(20);
    assert.equal(recorded?.url, "/api/v1/rankings/daily?limit=20");
    await boardForPeriod("monthly", 10);
    assert.equal(recorded?.url, "/api/v1/rankings/monthly?limit=10");
    await allTimeBoard();
    assert.equal(recorded?.url, "/api/v1/rankings/all?limit=20");
  });

  test("around-me rides period + radius; growth rides /growth/me", async () => {
    stubFetch(board([], null, null));
    await aroundMeBoard("all", 5);
    assert.equal(recorded?.url, "/api/v1/rankings/around-me?period=all&radius=5");

    stubFetch(
      JSON.stringify({
        month_points: 120,
        month_rank: 3,
        total_earned_points: 420,
        completed_count: 20,
        on_time_count: 18,
        on_time_ratio: 0.9,
        current_streak: 4,
        best_month_rank: 2,
        honors: [],
      }),
    );
    const growth = await myGrowth();
    assert.equal(recorded?.url, "/api/v1/growth/me");
    assert.equal(growth.month_points, 120);
    assert.equal(growth.honors.length, 0);
  });
});
