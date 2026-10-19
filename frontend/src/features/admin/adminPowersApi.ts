/**
 * Typed wrappers for the Wave 2a admin power-pack (V1.1 spec §3/§4;
 * the 2026-10-18 frozen contract, RECONCILED against the merged
 * backend's generated schema after #114): the shapes are GENERATED
 * aliases from `lib/api/schema` (the PR #72 discipline — the
 * handwritten contract-first types served their pins and retired).
 *
 * The reconciled truths this module rides (three drifted at
 * implementation; the terminal round caught them):
 * - the §3.2 reset issue answers a one-time `reset_token` (NOT a
 *   composed link) — the frontend composes the public consume URL
 *   (`/reset-password?token=…`, the page that spends it via
 *   POST /auth/password/reset-token);
 * - the sessions GET discloses the two COUNTS only (no row listing —
 *   the panorama carries both counts; only the kick DELETE remains);
 * - the resend answers the invitation's fresh `token` (the create
 *   contract's own shape) — the copyable URL composes through the
 *   SAME builder the create panel uses.
 *
 * The frozen answers that HELD:
 * - batch rows carry STABLE error codes in `reason` (ok=true rows
 *   have none) — the UI maps codes to copy, never renders free text;
 * - overview's by_role/by_status and tasks.by_status answer full
 *   fixed key sets, zero values included (the generated type is the
 *   looser record; the fixed keys are the wire pins' + the dashboard
 *   render's to hold);
 * - the invitation family rides the /admin/staff/invitations prefix
 *   with the four-state lifecycle and one-time tokens.
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** `GET /admin/overview` — the dashboard's one aggregate (spec §4.1). */
export type AdminOverviewDto = Schemas["AdminOverviewResponse"];

/** `GET /admin/users/{id}` — the detail panorama (spec §3.1). */
export type AdminUserDetailDto = Schemas["AdminUserPanoramaResponse"];

/** The §3.2 issue answer — the one-time token, exactly-once display. */
export type AdminResetTokenDto = Schemas["AdminPasswordResetLinkResponse"];

/** The kick's answer — both revoked counts (spec §3.3). */
export type AdminKickDto = Schemas["AdminKickResponse"];

/** The role change's answer — the updated row (spec §3.4). */
export type AdminUserRoleDto = Schemas["AdminUserRoleResponse"];

/** One batch verdict row — `reason` is a STABLE error code (frozen). */
export type AdminBatchResultDto = Schemas["AdminBatchRowResult"];

/** One invitation row (the admin surface may carry the plain email). */
export type AdminInvitationDto = Schemas["AdminInvitationRow"];

/** The resend/create answer's own shape — the fresh one-time token. */
export type AdminInvitationTokenDto = Schemas["StaffInvitationResponse"];

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
 * Issue the one-time password-reset token (spec §3.2): the response
 * carries the urlsafe32 token for exactly-once display (48h validity,
 * only its hash is kept); not for the admin's own account
 * (server-enforced, uniform refusal). The consumable URL composes
 * client-side: `/reset-password?token=…`.
 */
export function issueAdminPasswordReset(userId: string): Promise<AdminResetTokenDto> {
  return apiRequest<AdminResetTokenDto>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/password-reset`,
    { method: "POST" },
  );
}

/**
 * Kick: revoke ALL of the target user's refresh tokens and remembered
 * devices (spec §3.3) — the answer carries both revoked counts; the
 * kicked user's next request answers 401. (The GET on this route
 * discloses the two counts only — the panorama already carries them;
 * no row listing exists to wrap.)
 */
export function revokeAdminUserSessions(userId: string): Promise<AdminKickDto> {
  return apiRequest<AdminKickDto>(
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
): Promise<AdminUserRoleDto> {
  return apiRequest<AdminUserRoleDto>(
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
): Promise<Schemas["AdminBatchResponse"]> {
  return apiRequest<Schemas["AdminBatchResponse"]>("/api/v1/admin/users/batch", {
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
 * Resend (spec §4.2): mints a fresh one-time token (the old one dies,
 * the window reopens) — the create contract's own answer shape; the
 * copyable URL composes through buildStaffInviteUrl.
 */
export function resendAdminInvitation(invitationId: string): Promise<AdminInvitationTokenDto> {
  return apiRequest<AdminInvitationTokenDto>(
    `/api/v1/admin/staff/invitations/${encodeURIComponent(invitationId)}/resend`,
    { method: "POST" },
  );
}
