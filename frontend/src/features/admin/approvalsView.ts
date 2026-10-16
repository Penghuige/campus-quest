/**
 * The admin approvals page's view logic (auth-extension-II spec §5):
 * pure decisions for the pending-student listing and its review
 * actions. The frozen-surface copy pins + the phone-mask rule live
 * here; the page island consumes them.
 */
import { isApiError } from "@/lib/errors";

/** The pending list row (hand-written contract until the backend PR's
 * OpenAPI regen; the shape mirrors AdminUserResponse's approval cut). */
export interface PendingStudentDto {
  id: string;
  username: string; // the student number
  nickname: string;
  /** E.164, displayed MASKED on the page (privacy on the admin eye). */
  phone_e164: string | null;
  created_at: string;
}

/** CN mobile mask: keep the first 3 and last 4, mask the middle 4. */
export function maskPhone(phone: string | null): string {
  if (phone === null) {
    return "—";
  }
  if (/^1[3-9]\d{9}$/.test(phone)) {
    return `${phone.slice(0, 3)}****${phone.slice(7)}`;
  }
  // Non-CN shapes: mask all but the last 2 (never render a raw contact).
  return phone.length <= 2 ? "**" : `${"*".repeat(phone.length - 2)}${phone.slice(-2)}`;
}

/** The reject reason is transport-OPTIONAL but admin-encouraged. */
export function validateRejectReason(reason: string): string | undefined {
  if (reason.trim().length > 200) {
    return "理由请控制在 200 字以内";
  }
  return undefined;
}

export interface ApprovalErrorView {
  message: string;
  requestId: string | null;
}

const APPROVAL_NETWORK_ERROR_TEXT = "网络异常，请检查连接后重试";

/** The shared mutation-error view (the admin envelope family). */
export function describeApprovalError(
  error: unknown,
  fallback: string,
): ApprovalErrorView {
  if (!isApiError(error)) {
    return { message: APPROVAL_NETWORK_ERROR_TEXT, requestId: null };
  }
  if (error.code === "CONFLICT") {
    return {
      message: `${error.message || fallback}（该学生可能已被其他管理员审核，请刷新后重试）`,
      requestId: error.requestId,
    };
  }
  return { message: error.message || fallback, requestId: error.requestId };
}
