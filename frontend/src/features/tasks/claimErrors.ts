/**
 * Claim conflict presentation (spec §8, §29 envelope; patterns §6/§15):
 * branch on `error.code` — never message parsing — with typed zh-CN copy
 * per conflict family. `NO_ASSIGNMENT_AVAILABLE` and `RATE_LIMITED` are
 * transient, so their copy invites a retry; the claim button itself
 * re-enables after EVERY conflict (the server is the sole verdict on
 * whether a retry can succeed — nothing here locks the user out).
 */
import { isApiError, isKnownErrorCode, isSystemErrorCode } from "@/lib/errors";

/** Presentation view of one failed claim attempt. */
export interface ClaimErrorView {
  message: string;
  requestId: string | null;
}

/**
 * Stable zh-CN copy per claim-relevant envelope code (§29 registry; the
 * backend's own Chinese message is display fallback for anything else).
 */
export const CLAIM_CONFLICT_TEXT: Record<string, string> = {
  NO_ASSIGNMENT_AVAILABLE: "当前没有可领取的任务单元，请稍后重试或先看看其他任务",
  ASSIGNMENT_LIMIT_REACHED: "进行中的任务已达上限，请先提交或放弃一个任务后再领取",
  TASK_ACTIVE_CLAIM_EXISTS: "你已经领取过这个任务，完成或放弃后可再次领取",
  CLAIM_CUTOFF_REACHED: "该任务已过领取截止时间，看看其他任务吧",
  TASK_NOT_CLAIMABLE: "该任务当前不可领取",
  RATE_LIMITED: "操作过于频繁，请稍后再试",
  ACCOUNT_NOT_ACTIVE: "账号当前不可用，请联系管理员",
  PERMISSION_DENIED: "仅学生账号可领取任务",
  AUTHENTICATION_REQUIRED: "登录状态已失效，请重新登录",
};

const NETWORK_TEXT = "网络异常，请检查连接后重试";
const SYSTEM_TEXT = "服务暂时不可用，请稍后重试";
const GENERIC_TEXT = "领取失败，请稍后重试";

/** Derive a claim failure view. Pure: same failure in, same view out. */
export function describeClaimError(error: unknown): ClaimErrorView {
  if (!isApiError(error)) {
    return { message: NETWORK_TEXT, requestId: null };
  }
  const mapped = CLAIM_CONFLICT_TEXT[error.code];
  if (mapped !== undefined) {
    return { message: mapped, requestId: null };
  }
  if (isSystemErrorCode(error.code)) {
    return { message: SYSTEM_TEXT, requestId: error.requestId };
  }
  if (isKnownErrorCode(error.code)) {
    return { message: error.message || GENERIC_TEXT, requestId: null };
  }
  return { message: GENERIC_TEXT, requestId: error.requestId };
}
