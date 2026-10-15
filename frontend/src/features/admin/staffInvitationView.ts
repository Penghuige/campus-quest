/**
 * Staff-invitation issuance view logic (spec §5.8 step 1; the §8 Admin
 * surface) — pure decisions for the /admin/invitations island.
 *
 * - the role select offers exactly the backend's TEACHER/ADMIN
 *   narrowing (students register through the whitelist flow, never
 *   through invitations — staff_service);
 * - the email gate mirrors the backend's minimal shape check
 *   (`staff_service._normalized_email`) so a client-rejected value
 *   never burns a rate-limit slot on the 20/hour admin bucket;
 * - the copyable link joins the deployment mount prefix through the
 *   shared apiBase contract — "" in dev/CI, "/campus" in production —
 *   never a hardcoded either;
 * - error copy branches on `error.code` only (§29). The
 *   USERNAME_ALREADY_EXISTS branch exists on the ACCEPT side today; it
 *   lands on create when the backend adds the pre-check (reviewer
 *   ruling 2026-10-15: the V1.1 invitation-lifecycle scope candidate).
 */
import { isApiError } from "@/lib/errors";

/** The backend's invitation role narrowing (spec §5.8). */
export type StaffInvitationRole = "TEACHER" | "ADMIN";

export const INVITATION_ROLE_OPTIONS: readonly {
  value: StaffInvitationRole;
  label: string;
}[] = [
  { value: "TEACHER", label: "教师" },
  { value: "ADMIN", label: "管理员" },
];

/** The invitation TTL the backend issues (staff_service: 48h). */
export const INVITATION_TTL_HOURS = 48;

const INVALID_EMAIL_TEXT = "请输入有效的邮箱地址";

/**
 * Client mirror of the backend's minimal email shape check — every
 * server-rejected shape rejects here first (whitespace is tolerated
 * around the address; the server trims and lowercases on its side).
 */
export function validateInvitationEmail(value: string): string | undefined {
  const normalized = value.trim().toLowerCase();
  if (
    normalized.length === 0 ||
    !normalized.includes("@") ||
    normalized.startsWith("@") ||
    normalized.endsWith("@") ||
    normalized.length > 320
  ) {
    return INVALID_EMAIL_TEXT;
  }
  return undefined;
}

/**
 * The one-time invitation URL the admin copies out: the page origin,
 * the deployment mount prefix, then the accept route
 * (`/staff/invite/[token]`; token is URL-safe by construction).
 */
export function buildInvitationLink(
  origin: string,
  basePath: string,
  token: string,
): string {
  return `${origin}${basePath}/staff/invite/${token}`;
}

export interface StaffInvitationErrorView {
  message: string;
  requestId: string | null;
}

const INVITATION_NETWORK_ERROR_TEXT = "网络异常，请检查连接后重试";

/** Code-branched copy for a refused issuance (§29: never by status). */
export function describeInvitationError(
  error: unknown,
  fallback: string,
): StaffInvitationErrorView {
  if (!isApiError(error)) {
    return { message: INVITATION_NETWORK_ERROR_TEXT, requestId: null };
  }
  if (error.code === "USERNAME_ALREADY_EXISTS") {
    return { message: "该邮箱已是注册账号，不能重复邀请", requestId: error.requestId };
  }
  if (error.code === "RATE_LIMITED") {
    return {
      message: `创建过于频繁（每小时最多 20 个邀请），请稍后再试`,
      requestId: error.requestId,
    };
  }
  return { message: error.message || fallback, requestId: error.requestId };
}
