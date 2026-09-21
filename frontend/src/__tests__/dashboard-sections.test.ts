/**
 * Task 3 (Plan 09): dashboard section views — the ready/empty derivations
 * per section (spec §42) and the fetch-stubbed wrappers that feed them
 * (wallet, rewards, monthly board). Pins the "nearest affordable reward"
 * presentation choice and the server-owned window/stock verdicts.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  claimsSummaryView,
  pointsProgressView,
  rankSnapshotView,
} from "../features/dashboard/sections";
import { listRewards, myWallet, type RewardItemDto } from "../features/points/api";
import { monthlyBoard } from "../features/rankings/api";
import { listMyClaims } from "../features/tasks/api";

type RecordedRequest = { url: string; method: string };

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

function reward(overrides: Partial<RewardItemDto>): RewardItemDto {
  return {
    id: "44444444-4444-4444-8444-444444444444",
    name: "文创帆布包",
    description: null,
    point_cost: 300,
    stock: 5,
    per_user_term_limit: null,
    available_from: null,
    available_until: null,
    requires_manual_review: false,
    window_open: true,
    ...overrides,
  };
}

const WALLET = {
  available_points: 160,
  earned_points: 420,
  spendable_points: 160,
};

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

describe("points + nearest-reward progress", () => {
  test("targets the cheapest item on the purchasable shelf", () => {
    const view = pointsProgressView(WALLET, [
      reward({ id: "a", name: "贵", point_cost: 500 }),
      reward({ id: "b", name: "便宜", point_cost: 300 }),
    ]);
    assert.equal(view.status, "ready");
    assert.equal(view.rewardName, "便宜");
    assert.equal(view.rewardCost, 300);
    assert.equal(view.remainingPoints, 140);
    assert.equal(view.ratio, 160 / 300);
  });

  test("already affordable -> no remaining distance", () => {
    const view = pointsProgressView(WALLET, [reward({ point_cost: 120 })]);
    assert.equal(view.remainingPoints, null);
    assert.equal(view.ratio, 1);
  });

  test("server verdicts respected: closed window and zero stock are not candidates", () => {
    const view = pointsProgressView(WALLET, [
      reward({ name: "窗口关闭", window_open: false, point_cost: 100 }),
      reward({ name: "无库存", stock: 0, point_cost: 150 }),
      reward({ name: "无限库存", stock: null, point_cost: 400 }),
    ]);
    // window/stock exclusions leave only the unbounded-stock item.
    assert.equal(view.rewardName, "无限库存");
  });

  test("empty shelf -> wallet numbers still shown with the empty shape", () => {
    const view = pointsProgressView(WALLET, []);
    assert.equal(view.status, "empty");
    assert.equal(view.rewardName, null);
    assert.equal(view.availablePoints, 160);
    assert.equal(view.earnedPoints, 420);
  });

  test("frozen points (available > spendable) surface as a note figure", () => {
    const view = pointsProgressView(
      { ...WALLET, spendable_points: 100 },
      [reward({ point_cost: 120 })],
    );
    assert.equal(view.frozenPoints, 60);
  });

  test("wallet and rewards wrappers hit the exact endpoints", async () => {
    stubFetch(JSON.stringify(WALLET));
    const wallet = await myWallet();
    assert.equal(recorded?.url, "/api/v1/points/me");
    assert.equal(wallet.available_points, 160);

    stubFetch(JSON.stringify({ items: [reward({})] }));
    const rewards = await listRewards();
    assert.equal(recorded?.url, "/api/v1/rewards");
    assert.equal(rewards.items.length, 1);
  });
});

describe("claims summary grouping", () => {
  const claim = (status: string) => ({
    claim_id: `${status}-claim`,
    task_id: "11111111-1111-4111-8111-111111111111",
    task_title: "图书馆书影采集",
    status,
    platform: "小红书",
    keyword: "图书馆",
    claimed_at: "2026-09-20T08:00:00Z",
    deadline_at: "2026-09-20T20:00:00Z",
    grace_deadline_at: "2026-09-21T20:00:00Z",
    base_reward_points_snapshot: 200,
  });

  test("active and revision claims split; terminal history drops out", () => {
    const view = claimsSummaryView([
      claim("CLAIMED"),
      claim("REVISION_REQUIRED"),
      claim("COMPLETED"),
      claim("ABANDONED"),
      claim("UNDER_REVIEW"),
    ]);
    assert.deepEqual(
      view.active.map((row) => row.status),
      ["CLAIMED", "UNDER_REVIEW"],
    );
    assert.deepEqual(
      view.revision.map((row) => row.status),
      ["REVISION_REQUIRED"],
    );
  });

  test("empty own-claims page -> both groups empty (empty state renders)", () => {
    const view = claimsSummaryView([]);
    assert.deepEqual(view, { active: [], revision: [] });
  });

  test("own-claims wrapper rides the dashboard page size", async () => {
    stubFetch(JSON.stringify({ items: [], total: 0, limit: 10, offset: 0 }));
    await listMyClaims({ limit: 10 });
    assert.equal(recorded?.url, "/api/v1/me/claims?limit=10");
  });
});

describe("monthly rank snapshot", () => {
  const board = (myRank: number | null, myScore: number | null) =>
    JSON.stringify({
      entries: [
        { nickname: "同学甲", display_honor: "月度之星", score: 900, rank: 1 },
        { nickname: "同学乙", display_honor: null, score: 850, rank: 2 },
        { nickname: "同学丙", display_honor: null, score: 800, rank: 3 },
        { nickname: "同学丁", display_honor: null, score: 700, rank: 4 },
      ],
      my_rank: myRank,
      my_score: myScore,
    });

  test("ready: own standing + a 3-row top-of-board slice", () => {
    const view = rankSnapshotView(JSON.parse(board(7, 640)));
    assert.equal(view.status, "ready");
    assert.equal(view.rank, 7);
    assert.equal(view.score, 640);
    assert.equal(view.top.length, 3);
    assert.equal(view.top[0].displayHonor, "月度之星");
  });

  test("server says no score -> empty snapshot (empty state renders)", () => {
    const view = rankSnapshotView(JSON.parse(board(null, null)));
    assert.equal(view.status, "empty");
    assert.deepEqual(view.top, []);
  });

  test("board wrapper reads the monthly endpoint with a board size", async () => {
    stubFetch(board(1, 900));
    await monthlyBoard(5);
    assert.equal(recorded?.url, "/api/v1/rankings/monthly?limit=5");
  });
});
