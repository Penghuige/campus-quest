/**
 * Pure dashboard section views (spec §42 Student home; patterns §3/§8).
 *
 * Each function maps SERVER data onto a section's ready/empty shape. No
 * business rule is recomputed here: "nearest affordable reward" is a
 * presentation choice over the /rewards listing the backend has already
 * verdict-ed (window_open, stock); claim grouping reads the backend's own
 * status values; rank emptiness is the server's my_rank=null.
 */
import type { RewardItemDto, WalletDto } from "@/features/points/api";
import type { BoardDto } from "@/features/rankings/api";
import type { MyClaimDto } from "@/features/tasks/api";
import { isActiveClaim, isRevisionClaim } from "@/features/tasks/display";

// --- points + nearest-reward progress ----------------------------------------

export interface PointsProgressView {
  status: "empty" | "ready";
  availablePoints: number;
  earnedPoints: number;
  /** Points currently frozen by pending redemptions (shown as a note). */
  frozenPoints: number;
  rewardName: string | null;
  rewardCost: number | null;
  /** Points still needed; null when the reward is already affordable. */
  remainingPoints: number | null;
  /** Progress toward the reward, clamped to 0..1 (display only). */
  ratio: number;
}

/**
 * Wallet headline + distance to the nearest reward the student can work
 * toward: the cheapest item on the currently purchasable shelf (server
 * window + stock verdicts; stock=null means unbounded). No candidate ->
 * the empty view keeps the wallet numbers and explains the absence.
 */
export function pointsProgressView(
  wallet: WalletDto,
  rewards: RewardItemDto[],
): PointsProgressView {
  const candidates = rewards.filter(
    (item) =>
      item.window_open &&
      item.point_cost > 0 &&
      (item.stock === null || item.stock > 0),
  );
  const target = candidates.reduce<RewardItemDto | null>((best, item) => {
    if (best === null || item.point_cost < best.point_cost) {
      return item;
    }
    return best;
  }, null);

  const base: PointsProgressView = {
    status: "empty",
    availablePoints: wallet.available_points,
    earnedPoints: wallet.earned_points,
    frozenPoints: Math.max(wallet.available_points - wallet.spendable_points, 0),
    rewardName: null,
    rewardCost: null,
    remainingPoints: null,
    ratio: 0,
  };
  if (target === null) {
    return base;
  }
  const available = wallet.available_points;
  const remaining = Math.max(target.point_cost - available, 0);
  return {
    ...base,
    status: "ready",
    rewardName: target.name,
    rewardCost: target.point_cost,
    remainingPoints: remaining === 0 ? null : remaining,
    ratio: Math.min(available / target.point_cost, 1),
  };
}

// --- active / revision claims --------------------------------------------------

export interface ClaimsSummaryView {
  /** Open claims occupying an active slot, newest first (server order). */
  active: MyClaimDto[];
  /** Teacher-returned claims awaiting a corrected submission. */
  revision: MyClaimDto[];
}

/** Group own claims for the dashboard summary (§42 进行中 / 待修改). */
export function claimsSummaryView(claims: MyClaimDto[]): ClaimsSummaryView {
  return {
    active: claims.filter((claim) => isActiveClaim(claim.status)),
    revision: claims.filter((claim) => isRevisionClaim(claim.status)),
  };
}

// --- monthly rank snapshot -----------------------------------------------------

export interface RankSnapshotEntryView {
  nickname: string;
  displayHonor: string | null;
  score: number;
  rank: number;
}

export interface RankSnapshotView {
  /** Empty while the caller holds no score on the board (server verdict). */
  status: "empty" | "ready";
  rank: number;
  score: number;
  top: RankSnapshotEntryView[];
}

/** Monthly standing snapshot: own rank/score plus a small top-of-board. */
export function rankSnapshotView(board: BoardDto): RankSnapshotView {
  const myRank = board.my_rank ?? null;
  if (myRank === null) {
    return { status: "empty", rank: 0, score: 0, top: [] };
  }
  return {
    status: "ready",
    rank: myRank,
    score: board.my_score ?? 0,
    top: board.entries.slice(0, 3).map((entry) => ({
      nickname: entry.nickname,
      displayHonor: entry.display_honor,
      score: entry.score,
      rank: entry.rank,
    })),
  };
}
