/**
 * Pure reward display for the claim view (spec §11.2/§42; patterns §3).
 *
 * SERVER-AUTHORITATIVE PIN: every number rendered here is a SERVER
 * value, verbatim — `base_reward_points_snapshot` from the claim DTO.
 * The §9.3 decay ladder (100/80/50/20%) is NEVER computed client-side:
 * the student claim contract carries no lock projection yet, so no tier
 * percentage is displayed at all; when the DTO grows
 * `reward_lock_*`/`current_reward_points` fields, they render verbatim
 * here. Overdue copy deliberately shows NO number (patterns §3: decay
 * settlement is the server's).
 */
import { claimRewardLine } from "@/features/tasks/display";
import { parseServerInstant } from "@/lib/time";

/** The claim facts the reward view needs (all server-provided). */
export interface RewardClaimFacts {
  status: string;
  base_reward_points_snapshot: number;
  deadline_at: string;
  grace_deadline_at: string;
}

export interface RewardStatusView {
  tone: "info" | "success" | "warning" | "muted";
  /** Primary line first; secondary lines follow in order. */
  lines: string[];
}

/**
 * Reward copy per claim state. Pure: same server facts + nowMs in, same
 * view out. Pre-deadline and revision states reuse the §42 non-punitive
 * deadline copy from `tasks/display` (§16 reuse rule).
 */
export function rewardStatusView(claim: RewardClaimFacts, nowMs: number): RewardStatusView {
  const deadlineMs = parseServerInstant(claim.deadline_at);
  const graceMs = parseServerInstant(claim.grace_deadline_at);

  switch (claim.status) {
    case "REVISION_REQUIRED":
      // §11.3: 退回 preserves the PROVISIONAL lock — the snapshot stays
      // the earnable figure (design §14 preferred copy).
      return {
        tone: "warning",
        lines: [
          "老师已退回修改，奖励档位已保留",
          `当前仍可获得 ${claim.base_reward_points_snapshot} 积分`,
        ],
      };
    case "UNDER_REVIEW":
      return {
        tone: "info",
        lines: ["已提交，等待老师审核，积分以审核结果为准"],
      };
    case "COMPLETED":
      return {
        tone: "success",
        lines: ["任务已完成，积分已发放"],
      };
    case "ABANDONED":
    case "EXPIRED":
      return {
        tone: "muted",
        lines: ["任务未完成，未获得积分"],
      };
    case "VALIDATING":
      return {
        tone: "info",
        lines: ["正在校验刚提交的文件，请稍候"],
      };
    default:
      // CLAIMED (and any drift status): the §42 deadline-relative copy.
      return {
        tone: "info",
        lines: [claimRewardLine(claim.base_reward_points_snapshot, deadlineMs, graceMs, nowMs)],
      };
  }
}
