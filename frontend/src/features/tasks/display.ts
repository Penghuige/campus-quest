/**
 * Pure presentation mapping for task cards and claim copy (spec §42 card
 * fields; design-system §9 badges; patterns §3/§14).
 *
 * Everything here is a pure function of SERVER data plus an explicit
 * `nowMs` — no hidden `Date.now()`, no fetching, so every derivation is
 * deterministically testable. Two §42 rules are pinned by construction:
 * - the card shows the available COUNT, never an assignment list;
 * - countdown/urgency text is DISPLAY-ONLY ("倒计时仅用于 UX"); the server
 *   is the sole authority — a claim attempt past the cutoff answers
 *   CLAIM_CUTOFF_REACHED regardless of what the card said.
 */
import { countdownFrom, formatDeadlineSummary, parseServerInstant } from "@/lib/time";

// --- rarity (design-system §4: restrained accent + always a text label) -------

export type RarityKey = "NORMAL" | "RARE" | "EPIC" | "LEGENDARY";

export interface RarityView {
  /** Canonical key; drives the `data-rarity` accent token in CSS. */
  rarity: RarityKey;
  /** zh-CN label — always rendered next to the accent (§4). */
  label: string;
}

const RARITY_LABELS: Record<RarityKey, string> = {
  NORMAL: "普通",
  RARE: "稀有",
  EPIC: "史诗",
  LEGENDARY: "传说",
};

/**
 * Rarity view; unknown values fall back to NORMAL so the badge stays
 * token-backed even under contract drift.
 */
export function rarityView(rarity: string): RarityView {
  const key = (Object.keys(RARITY_LABELS) as RarityKey[]).find(
    (candidate) => candidate === rarity,
  );
  const rarityKey: RarityKey = key ?? "NORMAL";
  return { rarity: rarityKey, label: RARITY_LABELS[rarityKey] };
}

// --- rating / availability (§42: aggregate + count only) ----------------------

/** Rating aggregate text; "暂无评分" while the community module has no data. */
export function ratingText(rating: {
  average: number;
  count: number;
} | null): string {
  if (rating === null || rating.count <= 0) {
    return "暂无评分";
  }
  const average = Number.isFinite(rating.average)
    ? rating.average.toFixed(1)
    : "—";
  return `评分 ${average} · ${rating.count} 条评价`;
}

/** Availability COUNT text (§42); zero reads as depleted, never as a list. */
export function availabilityText(count: number): string {
  return count > 0 ? `可领取 ${count} 个` : "已被领完";
}

// --- deadline (spec §9.1/§9.2 modes; patterns §14 display format) -------------

export type DeadlineUrgency = "none" | "near" | "closed";

export interface DeadlineView {
  /** FIXED vs RELATIVE as product wording (never the raw enum). */
  modeLabel: string;
  /** One-line remaining-time text ("9月19日 18:00 · 还剩 3 小时 12 分"). */
  line: string;
  /** Display-only urgency hint derived from the server deadline. */
  urgency: DeadlineUrgency;
}

/**
 * Display threshold for the FIXED near-cutoff hint. The backend's default
 * claim cutoff is 240 minutes (spec §6 default; `Settings` carries the
 * task-level value, which the card DTO does not include), so this mirrors
 * the default — a UX hint only; the authoritative verdict stays with the
 * claim attempt.
 */
export const NEAR_CUTOFF_MS = 4 * 60 * 60 * 1000;

export function deadlineView(
  card: {
    deadline_mode: string;
    fixed_deadline_at: string | null;
    duration_minutes: number | null;
  },
  nowMs: number,
): DeadlineView {
  if (card.deadline_mode === "RELATIVE") {
    const minutes = card.duration_minutes;
    return {
      modeLabel: "领取后计时",
      line:
        minutes !== null && minutes > 0
          ? `领取后 ${minutes} 分钟内提交`
          : "领取后限时提交",
      urgency: "none",
    };
  }
  if (card.fixed_deadline_at === null) {
    return { modeLabel: "固定截止", line: "截止时间待定", urgency: "none" };
  }
  const deadlineMs = parseServerInstant(card.fixed_deadline_at);
  const parts = countdownFrom(deadlineMs, nowMs);
  const urgency: DeadlineUrgency = parts.expired
    ? "closed"
    : parts.totalMs <= NEAR_CUTOFF_MS
      ? "near"
      : "none";
  return {
    modeLabel: "固定截止",
    line: formatDeadlineSummary(deadlineMs, nowMs),
    urgency,
  };
}

// --- claim status (design-system §9: product wording, no raw enums) -----------

export type ClaimStatusTone = "info" | "success" | "warning" | "muted";

export interface ClaimStatusView {
  label: string;
  tone: ClaimStatusTone;
}

const CLAIM_STATUS_VIEWS: Record<string, ClaimStatusView> = {
  CLAIMED: { label: "待提交", tone: "info" },
  VALIDATING: { label: "校验中", tone: "info" },
  UNDER_REVIEW: { label: "待审核", tone: "info" },
  REVISION_REQUIRED: { label: "需修改", tone: "warning" },
  COMPLETED: { label: "已完成", tone: "success" },
  ABANDONED: { label: "已放弃", tone: "muted" },
  EXPIRED: { label: "已过期", tone: "muted" },
};

/** Status wording + tone; unknown statuses (drift) degrade to a neutral view. */
export function claimStatusView(status: string): ClaimStatusView {
  return CLAIM_STATUS_VIEWS[status] ?? { label: "进行中", tone: "info" };
}

/** Statuses still occupying one active slot (backend active set, §8.2). */
export function isActiveClaim(status: string): boolean {
  return (
    status === "CLAIMED" ||
    status === "VALIDATING" ||
    status === "UNDER_REVIEW"
  );
}

/** The teacher-returned state: still open for a corrected submission. */
export function isRevisionClaim(status: string): boolean {
  return status === "REVISION_REQUIRED";
}

// --- reward copy (spec §42: overdue framing stays non-punitive) ---------------

/**
 * Reward line for the claim panel.
 *
 * - before the deadline: the design-system §14 preferred copy with the
 *   server-provided snapshot;
 * - overdue but within grace: §42's non-punitive framing WITHOUT a
 *   number — the decayed current reward is a server settlement (patterns
 *   §3 forbids duplicating the §9.3 ladder client-side), so no figure is
 *   invented; adopt a server current-reward field when the contract
 *   grows one;
 * - at/after grace: the closed copy (the claim itself is terminal).
 */
export function claimRewardLine(
  baseRewardPoints: number,
  deadlineMs: number,
  graceDeadlineMs: number,
  nowMs: number,
): string {
  if (nowMs >= graceDeadlineMs) {
    return "提交窗口已关闭";
  }
  if (nowMs > deadlineMs) {
    return "已超过截止时间，当前仍可获得积分（以提交时结算为准）";
  }
  return `当前可获得 ${baseRewardPoints} 积分`;
}
