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
  /**
   * The wallet's overdraft, VERBATIM from `point_debt` (the backend
   * clamps available/spendable at 0 and carries the overdraft here;
   * never a client-side negative). Shown as a note when > 0.
   */
  pointDebt: number;
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
    pointDebt: wallet.point_debt,
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

// --- shelf gating for the wallet panel ------------------------------------------

/** The rewards shelf's load state as the wallet panel consumes it. */
export interface RewardsShelf {
  status: "loading" | "error" | "ready";
  items: RewardItemDto[];
}

/**
 * What the wallet panel may render about the rewards shelf. The empty
 * copy ("暂无可兑换的奖励") is a SERVER-VERDICTED fact — no purchasable
 * item exists — so it is reachable ONLY through the ready shelf:
 * - `loading`  -> skeleton line (no verdict yet);
 * - `unavailable` -> muted note (the section error + retry already
 *   rendered above; the wallet numbers stay);
 * - `verdict`  -> the ready-shelf progress view, empty or not.
 */
export type WalletShelfView =
  | { shelf: "loading"; frozenPoints: number; pointDebt: number }
  | { shelf: "unavailable"; frozenPoints: number; pointDebt: number }
  | { shelf: "verdict"; frozenPoints: number; pointDebt: number; progress: PointsProgressView };

/**
 * Gate the shelf branch for the wallet panel; pure presentation rule.
 * `frozenPoints` and `pointDebt` ride every branch — both are WALLET
 * facts (the freeze note and the overdraft note), not properties of the
 * shelf's load state. `pointDebt` is the backend's clamped overdraft
 * carried verbatim (see `PointsProgressView.pointDebt`).
 */
export function walletShelfView(
  wallet: WalletDto,
  rewards: RewardsShelf,
): WalletShelfView {
  const frozenPoints = Math.max(
    wallet.available_points - wallet.spendable_points,
    0,
  );
  const pointDebt = wallet.point_debt;
  if (rewards.status === "loading") {
    return { shelf: "loading", frozenPoints, pointDebt };
  }
  if (rewards.status === "error") {
    return { shelf: "unavailable", frozenPoints, pointDebt };
  }
  return {
    shelf: "verdict",
    frozenPoints,
    pointDebt,
    progress: pointsProgressView(wallet, rewards.items),
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

// --- next-action hero ------------------------------------------------------------

export interface NextActionView {
  kind: "revision" | "active";
  claimId: string;
  taskTitle: string;
  /** The backend status — feeds the mini progress rail (claimStepView). */
  status: string;
  /**
   * The active claim's server deadline (ISO). Revision claims carry no
   * deadline in the /me/claims DTO yet — null there (the copy explains
   * the state instead).
   */
  deadlineAt: string | null;
}

/**
 * The dashboard hero's single "当前最重要" (brief §9: the next action
 * should visually dominate). A teacher-returned revision outranks
 * everything — someone is waiting on you; otherwise the earliest
 * ISO-deadline open claim; null when the student holds no open work.
 * Pure presentation choice over server statuses and deadlines.
 */
export function nextActionView(claims: MyClaimDto[]): NextActionView | null {
  const { active, revision } = claimsSummaryView(claims);
  if (revision.length > 0) {
    const first = revision[0];
    return {
      kind: "revision",
      claimId: first.claim_id,
      taskTitle: first.task_title,
      status: first.status,
      deadlineAt: null,
    };
  }
  const withDeadline = active
    .filter((claim) => typeof claim.deadline_at === "string")
    .sort((a, b) =>
      a.deadline_at.localeCompare(b.deadline_at),
    );
  const pick = withDeadline[0] ?? active[0] ?? null;
  if (pick === null) {
    return null;
  }
  return {
    kind: "active",
    claimId: pick.claim_id,
    taskTitle: pick.task_title,
    status: pick.status,
    deadlineAt: pick.deadline_at ?? null,
  };
}

/**
 * Dashboard discovery dedup: hide cards the student already holds an
 * OPEN claim on (the baseline audit showed the same task twice —
 * once in 我的任务, again in 最新任务). Presentation-only filter over
 * server data; the task square keeps the full listing.
 */
export function withoutClaimedTasks<T extends { id: string }>(
  cards: T[],
  claims: MyClaimDto[],
): T[] {
  const claimed = new Set(
    claims
      .filter((claim) => isActiveClaim(claim.status) || isRevisionClaim(claim.status))
      .map((claim) => claim.task_id),
  );
  return cards.filter((card) => !claimed.has(card.id));
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
