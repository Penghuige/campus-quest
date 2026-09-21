/**
 * Pure redemption-surface views (spec §16/§16.1/§16.2, §32; patterns
 * §3/§6/§15). Every function maps SERVER verdicts onto display shapes —
 * no business rule is recomputed here:
 *
 * - the shelf card states read the server's own `window_open` verdict
 *   and the item's `stock` count verbatim (spendability is NEVER
 *   pre-judged client-side; the server's INSUFFICIENT_POINTS is the
 *   teacher — patterns §3 "redemption eligibility");
 * - conflict copy branches on `error.code` only, never on message text;
 * - lifecycle labels map the backend `RewardRedemption.status` values
 *   to product wording (design §9: no raw enum names).
 */
import { isApiError, isKnownErrorCode, isSystemErrorCode } from "@/lib/errors";
import { formatDeadlineDateTime, BUSINESS_TIME_CONFIG } from "@/lib/time";

import type { RewardItemDto } from "@/features/points/api";

// --- shelf card ------------------------------------------------------------------

/** The display state of one catalogue row, from server verdicts only. */
export type RewardShelfState = "redeemable" | "out-of-stock" | "window-closed";

export interface RewardShelfView {
  state: RewardShelfState;
  /** Badge text for the state (color is supplemental; design §9). */
  stateLabel: string;
  /** Stock line; null when the server says unbounded (stock=null). */
  stockLabel: string | null;
  /** Redemption-window line; null when the item has no time bounds. */
  windowLabel: string | null;
}

/** Scarcity threshold for the 仅剩 wording (presentation choice only). */
export const LOW_STOCK_THRESHOLD = 5;

/**
 * The shelf card for one item. Window verdict FIRST (a closed window is
 * the server's gate regardless of stock), then stock — both fields are
 * read verbatim from the DTO.
 */
export function rewardShelfView(item: RewardItemDto): RewardShelfView {
  let state: RewardShelfState = "redeemable";
  let stateLabel = "可兑换";
  if (!item.window_open) {
    state = "window-closed";
    stateLabel = "暂不可兑换";
  } else if (item.stock === 0) {
    state = "out-of-stock";
    stateLabel = "已兑完";
  }

  let stockLabel: string | null = null;
  if (item.stock === null) {
    stockLabel = "库存充足";
  } else if (item.stock > 0) {
    stockLabel =
      item.stock <= LOW_STOCK_THRESHOLD
        ? `仅剩 ${item.stock} 件`
        : `剩余 ${item.stock} 件`;
  }

  return {
    state,
    stateLabel,
    stockLabel,
    windowLabel: rewardWindowLabel(item),
  };
}

/** One-sided or bounded window text; null when the item is always open. */
export function rewardWindowLabel(
  item: Pick<RewardItemDto, "available_from" | "available_until">,
): string | null {
  const from = item.available_from === null ? null : formatWindowBound(item.available_from);
  const until = item.available_until === null ? null : formatWindowBound(item.available_until);
  if (from !== null && until !== null) {
    return `兑换窗口 ${from} – ${until}`;
  }
  if (from !== null) {
    return `${from} 起可兑换`;
  }
  if (until !== null) {
    return `${until} 截止`;
  }
  return null;
}

function formatWindowBound(iso: string): string {
  return formatDeadlineDateTime(Date.parse(iso), BUSINESS_TIME_CONFIG);
}

// --- typed conflict copy ----------------------------------------------------------

/** Presentation view of one failed redemption attempt. */
export interface RedeemErrorView {
  message: string;
  requestId: string | null;
}

/** Stable zh-CN copy for the §16.1 redeem-gate codes (patterns §15). */
export const REDEEM_ERROR_TEXT: Record<string, string> = {
  INSUFFICIENT_POINTS: "可花费积分不足（积分可能仍在兑换申请中冻结），本次未扣除积分",
  REWARD_OUT_OF_STOCK: "该奖励已全部兑完，请看看其他奖励",
  REDEMPTION_LIMIT_REACHED: "本学期该奖励的兑换次数已达上限",
  RATE_LIMITED: "操作过于频繁，请稍后再试",
};

const REDEEM_SYSTEM_ERROR_TEXT = "服务暂时不可用，请稍后重试";
const REDEEM_GENERIC_ERROR_TEXT = "兑换失败，请稍后重试";
export const REDEEM_NETWORK_ERROR_TEXT = "网络异常，请检查连接后重试";

/**
 * Typed copy for a failed redeem POST. Branches on `error.code` only:
 * mapped gate codes -> stable copy; system codes and registry drift ->
 * safe generic + request id; any other known business code -> the
 * backend's own Chinese message as fallback display; non-ApiError ->
 * connectivity line.
 */
export function describeRedeemError(error: unknown): RedeemErrorView {
  if (!isApiError(error)) {
    return { message: REDEEM_NETWORK_ERROR_TEXT, requestId: null };
  }
  const mapped = REDEEM_ERROR_TEXT[error.code];
  if (mapped !== undefined) {
    return { message: mapped, requestId: null };
  }
  if (isSystemErrorCode(error.code)) {
    return { message: REDEEM_SYSTEM_ERROR_TEXT, requestId: error.requestId };
  }
  if (isKnownErrorCode(error.code)) {
    return { message: error.message || REDEEM_GENERIC_ERROR_TEXT, requestId: null };
  }
  return { message: REDEEM_GENERIC_ERROR_TEXT, requestId: error.requestId };
}

// --- lifecycle labels -------------------------------------------------------------

export interface RedemptionStatusView {
  label: string;
  tone: "info" | "success" | "danger";
}

/**
 * Product wording for the backend redemption statuses (spec §16.2;
 * design §9: text always, no raw enum names). Unknown values degrade to
 * a calm neutral label rather than the raw string.
 */
export function redemptionStatusView(status: string): RedemptionStatusView {
  switch (status) {
    case "REQUESTED":
      return { label: "待审核", tone: "info" };
    case "UNDER_REVIEW":
      return { label: "审核中", tone: "info" };
    case "APPROVED":
      return { label: "已通过", tone: "success" };
    case "REJECTED":
      return { label: "未通过", tone: "danger" };
    case "FULFILLED":
      return { label: "已发放", tone: "success" };
    default:
      return { label: "处理中", tone: "info" };
  }
}

// --- idempotency keys --------------------------------------------------------------

/**
 * Mint one advisory Idempotency-Key (spec §32). FRESH PER ATTEMPT: the
 * backend accepts but does NOT dedupe on the header in V1, so a retry
 * after a typed conflict is a new logical attempt and carries a new key
 * (see points/api.ts `redeemReward` for the contract note).
 */
export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Exotic-runtime fallback: best-effort randomness is acceptable while
  // the header stays advisory.
  return `cq-${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}
