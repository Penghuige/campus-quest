/**
 * Typed wrappers for the Wave 2a admin power-pack (V1.1 spec §3/§4;
 * the 2026-10-18 frozen contract with the backend session) —
 * contract-first: the shapes below are HANDWRITTEN against the spec
 * text and the session's frozen answers while the backend lands
 * (branch wave2a-admin-powers); when its PR merges these flip to
 * generated aliases from `lib/api/schema` (the PR #72 discipline).
 *
 * The frozen answers this module holds:
 * - batch rows carry STABLE error codes in `reason` (same domain as
 *   BusinessError.code; ok=true rows have none) — the UI maps codes to
 *   copy, never renders free text from the wire;
 * - overview's by_role/by_status and tasks.by_status answer FULL
 *   fixed key sets, zero values included (no data-dependent keys);
 * - whitelist_size is gone (the whitelist retired with #95; spec §4.3
 *   voided by 547099b);
 * - the invitation family rides the /admin/staff/invitations prefix
 *   (the create flow keeps its own /staff/invitations path) with the
 *   four-state lifecycle and one-time resend links.
 */
import { apiRequest } from "@/lib/api";

/** One role bucket of the overview's user census. */
export type RoleKey = "STUDENT" | "TEACHER" | "ADMIN";

/** The user-status machine's four states (fixed keys, zero included). */
export type UserStatusKey = "ACTIVE" | "PENDING_APPROVAL" | "SUSPENDED" | "BANNED";

/** The task lifecycle's five states (fixed keys, zero included). */
export type TaskStatusKey = "DRAFT" | "PUBLISHED" | "PAUSED" | "CLOSED" | "ARCHIVED";

/** `GET /admin/overview` — the dashboard's one aggregate (spec §4.1). */
export interface AdminOverviewDto {
  users: {
    total: number;
    by_role: Record<RoleKey, number>;
    by_status: Record<UserStatusKey, number>;
  };
  tasks: { by_status: Record<TaskStatusKey, number> };
  pending_submissions: number;
  pending_redemptions: number;
  today: { new_users: number; active_users: number };
}

/** One row of the detail page's recent-submissions list (spec §3.1). */
export interface AdminRecentSubmissionDto {
  id: string;
  task_title: string;
  status: string;
  submitted_at: string;
}

/** `GET /admin/users/{id}` — the detail panorama (spec §3.1). */
export interface AdminUserDetailDto {
  /** The users listing's own row shape (the shared account view). */
  user: {
    id: string;
    username: string;
    nickname: string;
    role: string;
    status: string;
  };
  wallet_balance: number;
  recent_submissions: AdminRecentSubmissionDto[];
  active_session_count: number;
  remembered_device_count: number;
}

/** One active session row (spec §3.3's listing). */
export interface AdminSessionDto {
  id: string;
  created_at: string;
}

/** One batch verdict row — `reason` is a STABLE error code (frozen). */
export interface AdminBatchResultDto {
  user_id: string;
  ok: boolean;
  reason?: string;
}

/** The invitation lifecycle's four states (spec §4.2). */
export type AdminInvitationStatus = "PENDING" | "ACCEPTED" | "EXPIRED" | "REVOKED";

/** One invitation row (the admin surface may carry the plain email). */
export interface AdminInvitationDto {
  id: string;
  email: string;
  role: string;
  status: AdminInvitationStatus;
  created_by: string;
  created_at: string;
  expires_at: string;
}

/** The dashboard's aggregate (spec §4.1). */
export function fetchAdminOverview(): Promise<AdminOverviewDto> {
  return apiRequest<AdminOverviewDto>("/api/v1/admin/overview");
}

/** The user detail panorama (spec §3.1). */
export function fetchAdminUserDetail(userId: string): Promise<AdminUserDetailDto> {
  return apiRequest<AdminUserDetailDto>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}`,
  );
}

/**
 * Issue the one-time password-reset link (spec §3.2): the response
 * carries the link for exactly-once display (48h validity); not for
 * the admin's own account (server-enforced, uniform refusal).
 */
export function issueAdminPasswordReset(userId: string): Promise<{ reset_link: string }> {
  return apiRequest<{ reset_link: string }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/password-reset`,
    { method: "POST" },
  );
}

/** List the target user's active sessions (spec §3.3). */
export function listAdminUserSessions(userId: string): Promise<{ items: AdminSessionDto[] }> {
  return apiRequest<{ items: AdminSessionDto[] }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/sessions`,
  );
}

/**
 * Kick: revoke ALL of the target user's refresh tokens and remembered
 * devices (spec §3.3) — the answer is the revoked count; the kicked
 * user's next request answers 401.
 */
export function revokeAdminUserSessions(userId: string): Promise<{ revoked: number }> {
  return apiRequest<{ revoked: number }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/sessions`,
    { method: "DELETE" },
  );
}

/**
 * TEACHER↔ADMIN (spec §3.4): not the admin's own account, not the
 * last ACTIVE ADMIN (server-enforced); answers the updated row.
 */
export function changeAdminUserRole(
  userId: string,
  role: "TEACHER" | "ADMIN",
): Promise<AdminUserDetailDto["user"]> {
  return apiRequest<AdminUserDetailDto["user"]>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/role`,
    { method: "PATCH", body: { role } },
  );
}

/**
 * Batch suspend/reactivate (spec §3.5): ≤100 ids per call; every row
 * answers its own verdict (stable error codes in `reason`).
 */
export function batchAccountAction(
  action: "suspend" | "reactivate",
  userIds: readonly string[],
): Promise<{ results: AdminBatchResultDto[] }> {
  return apiRequest<{ results: AdminBatchResultDto[] }>("/api/v1/admin/users/batch", {
    method: "POST",
    body: { action, user_ids: userIds },
  });
}

/** The invitation list (spec §4.2, the admin prefix family). */
export function listAdminInvitations(): Promise<{ items: AdminInvitationDto[] }> {
  return apiRequest<{ items: AdminInvitationDto[] }>("/api/v1/admin/staff/invitations");
}

/** Revoke a not-yet-accepted invitation (204; the token dies now). */
export function revokeAdminInvitation(invitationId: string): Promise<void> {
  return apiRequest<void>(
    `/api/v1/admin/staff/invitations/${encodeURIComponent(invitationId)}`,
    { method: "DELETE" },
  );
}

/**
 * Resend (spec §4.2): mints a fresh token (the old one dies) and the
 * response carries the new link for exactly-once display.
 */
export function resendAdminInvitation(invitationId: string): Promise<{ link: string }> {
  return apiRequest<{ link: string }>(
    `/api/v1/admin/staff/invitations/${encodeURIComponent(invitationId)}/resend`,
    { method: "POST" },
  );
}
