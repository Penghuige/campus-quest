/**
 * Submission/abandon conflict presentation (spec §10/§29 envelope;
 * patterns §6/§15): branch on `error.code` — never message parsing — with
 * typed zh-CN copy per conflict family. Every failure keeps a retry path
 * visible: the server is the sole verdict on whether a retry can succeed
 * (the claim itself is only ever moved by server responses — a failed
 * attempt never mutates the visible claim state; patterns §7).
 */
import { isApiError, isKnownErrorCode, isSystemErrorCode } from "@/lib/errors";

/** Presentation view of one failed submission/abandon attempt. */
export interface SubmissionErrorView {
  message: string;
  requestId: string | null;
}

/**
 * Stable zh-CN copy per submission-surface envelope code (§29 registry;
 * the backend's own Chinese message is the display fallback otherwise).
 */
export const SUBMISSION_CONFLICT_TEXT: Record<string, string> = {
  SUBMISSION_WINDOW_CLOSED: "提交窗口已关闭，这个任务无法再提交",
  CLAIM_NOT_SUBMITTABLE: "当前状态不能提交，可能已在审核中或已结束",
  FILE_TOO_LARGE: "文件大小超出限制，请压缩后重新选择",
  FILE_TYPE_NOT_ALLOWED: "这个任务不接受该文件类型，请改用任务允许的格式",
  RATE_LIMITED: "操作过于频繁，请稍后再试",
  ABANDON_LIMIT_REACHED: "今日放弃次数已达上限，请明天再试",
  CLAIM_NOT_ABANDONABLE: "当前状态不能放弃（审核中的任务由老师处理）",
  AUTHENTICATION_REQUIRED: "登录状态已失效，请重新登录",
  ACCOUNT_NOT_ACTIVE: "账号当前不可用，请联系管理员",
  PERMISSION_DENIED: "仅任务所有者可以执行这个操作",
};

const NETWORK_TEXT = "网络异常，请检查连接后重试";
const SYSTEM_TEXT = "服务暂时不可用，请稍后重试";
const GENERIC_TEXT = "操作失败，请稍后重试";

/** Derive a submission/abandon failure view. Pure: same failure in, same view out. */
export function describeSubmissionError(error: unknown): SubmissionErrorView {
  if (!isApiError(error)) {
    return { message: NETWORK_TEXT, requestId: null };
  }
  const mapped = SUBMISSION_CONFLICT_TEXT[error.code];
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
