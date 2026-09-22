/**
 * Typed wrappers for the Admin workspace endpoints (Plan 08's 27-endpoint
 * admin surface, consumed by Plan 09 Task 10; backend
 * `identity/admin_router.py`, `identity/whitelist_admin.py`,
 * `system/router.py`, `audit/router.py`, `notifications/router.py`,
 * `points/admin_router.py` and the points review-queue routes).
 *
 * Every shape comes from the GENERATED OpenAPI snapshot in
 * `lib/api/schema` (already regenerated on this branch — no hand-written
 * DTOs). Wire-shape pins live in `src/__tests__/admin-api.test.ts`.
 *
 * Guard posture (backend `require_admin_actor` + the store-backed
 * management-network dependency): these paths answer 403 for non-admin
 * sessions; the AdminShell's role gate keeps them from even firing.
 * 409s carry the frozen-registry `CONFLICT` code (registered Plan 08
 * T9) — branch through `describeAdminMutationError` in `adminView`.
 *
 * Listing-surface honesty (frozen contract, UI mirrors it):
 * - the reward catalogue READ the rewards page mounts is still the
 *   student listing `GET /rewards` (enabled items only); the admin
 *   listing `GET /admin/rewards` (disabled rows included) and the
 *   template listing `GET /admin/notification-templates` exist in the
 *   generated contract (T10 gap-fill) but no page consumes them yet —
 *   wiring them is a separately-scoped UI change; disable responses
 *   are held client-side so a just-disabled row keeps rendering its
 *   verdict;
 * - NotificationTemplates gained an admin listing in the contract
 *   (T10); the template panel still edits by id from the create
 *   response / audit trail until a page consumes the listing.
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

// --- DTOs (generated contract) ---------------------------------------------------------

export type RoleDto = Schemas["Role"];
export type UserStatusDto = Schemas["UserStatus"];
export type AdminUserDto = Schemas["AdminUserResponse"];
export type AdminUserPageDto = Schemas["AdminUserListResponse"];
export type AccountStatusDto = Schemas["AccountStatusResponse"];

export type WhitelistEntryDto = Schemas["WhitelistEntryResponse"];
export type WhitelistPageDto = Schemas["WhitelistListResponse"];
export type WhitelistPreviewDto = Schemas["WhitelistPreviewResponse"];
export type WhitelistRowDecisionDto = Schemas["WhitelistRowDecisionResponse"];
export type WhitelistCountsDto = Schemas["WhitelistCountsResponse"];
export type WhitelistConfirmDto = Schemas["WhitelistConfirmResponse"];
export type WhitelistToggleDto = Schemas["WhitelistToggleResponse"];

/** The student-view catalogue row the admin listing reads (see module note). */
export type RewardCatalogueDto = Schemas["RewardItemResponse"];
/** The full admin row every admin mutation answers with. */
export type AdminRewardItemDto = Schemas["AdminRewardItemResponse"];
export type RewardCreateBody = Schemas["RewardItemCreateRequest"];
export type RewardUpdateBody = Schemas["RewardItemUpdateRequest"];

export type RedemptionReviewDto = Schemas["RedemptionReviewResponse"];
/** Collision-safe generated name: the points router owns this queue shape. */
export type RedemptionPageDto =
  Schemas["app__modules__points__router__ReviewQueueResponse"];

export type AuditLogDto = Schemas["AuditLogResponse"];
export type AuditLogPageDto = Schemas["AuditLogListResponse"];

export type SystemSettingItemDto = Schemas["SystemSettingItemResponse"];
export type SystemSettingsListDto = Schemas["SystemSettingsListResponse"];
export type SystemSettingValueDto = Schemas["SystemSettingValueResponse"];
export type CurrentAcademicTermDto = Schemas["CurrentAcademicTermResponse"];

export type NotificationFailureDto = Schemas["NotificationFailureResponse"];
export type NotificationFailurePageDto = Schemas["NotificationFailuresResponse"];
export type AdminNotificationTemplateDto =
  Schemas["AdminNotificationTemplateResponse"];
export type TemplateEventTypeDto = Schemas["NotificationEventType"];
export type TemplateChannelDto = Schemas["NotificationChannel"];

export type RevealIdentityDto = Schemas["RevealIdentityResponse"];
export type ReleaseOccupiedAssignmentDto =
  Schemas["ReleaseOccupiedAssignmentResponse"];
export type ForceFailDeliveryDto = Schemas["ForceFailDeliveryResponse"];

// --- shared pagination -------------------------------------------------------------------

export interface PageQuery {
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}

function pagination(query: PageQuery): string {
  const params = new URLSearchParams();
  if (query.limit !== undefined) {
    params.set("limit", String(query.limit));
  }
  if (query.offset !== undefined) {
    params.set("offset", String(query.offset));
  }
  const encoded = params.toString();
  return encoded.length > 0 ? `?${encoded}` : "";
}

function encodeId(value: string): string {
  return encodeURIComponent(value);
}

// --- account directory (spec §5.7) --------------------------------------------------------

/** The account directory page (GET /admin/users; role/status filters). */
export function listAdminUsers(
  query: PageQuery & { role?: RoleDto; status?: UserStatusDto } = {},
): Promise<AdminUserPageDto> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) {
    params.set("limit", String(query.limit));
  }
  if (query.offset !== undefined) {
    params.set("offset", String(query.offset));
  }
  if (query.role !== undefined) {
    params.set("role", query.role);
  }
  if (query.status !== undefined) {
    params.set("status", query.status);
  }
  const encoded = params.toString();
  return apiRequest<AdminUserPageDto>(
    `/api/v1/admin/users${encoded.length > 0 ? `?${encoded}` : ""}`,
    { signal: query.signal },
  );
}

/** ACTIVE -> SUSPENDED (POST /admin/users/{id}/suspend; reason mandatory, audited). */
export function suspendAdminUser(userId: string, reason: string): Promise<AccountStatusDto> {
  return apiRequest<AccountStatusDto>(
    `/api/v1/admin/users/${encodeId(userId)}/suspend`,
    { method: "POST", body: { reason } },
  );
}

/** ACTIVE -> BANNED (POST /admin/users/{id}/ban; reason mandatory, audited). */
export function banAdminUser(userId: string, reason: string): Promise<AccountStatusDto> {
  return apiRequest<AccountStatusDto>(
    `/api/v1/admin/users/${encodeId(userId)}/ban`,
    { method: "POST", body: { reason } },
  );
}

/** SUSPENDED/BANNED -> ACTIVE (POST /admin/users/{id}/reactivate; reason mandatory). */
export function reactivateAdminUser(
  userId: string,
  reason: string,
): Promise<AccountStatusDto> {
  return apiRequest<AccountStatusDto>(
    `/api/v1/admin/users/${encodeId(userId)}/reactivate`,
    { method: "POST", body: { reason } },
  );
}

// --- whitelist (spec §5.1) -----------------------------------------------------------------

/** The whitelist page, student-number ordered (GET /admin/whitelist). */
export function listWhitelist(query: PageQuery = {}): Promise<WhitelistPageDto> {
  return apiRequest<WhitelistPageDto>(
    `/api/v1/admin/whitelist${pagination(query)}`,
    { signal: query.signal },
  );
}

/**
 * Classify every non-blank line, zero writes (POST /admin/whitelist/preview).
 * The body is the RAW import TEXT as JSON `{content}` — the service owns
 * decode, trimming, and every row verdict; the digest binds the confirm.
 */
export function previewWhitelistImport(content: string): Promise<WhitelistPreviewDto> {
  return apiRequest<WhitelistPreviewDto>("/api/v1/admin/whitelist/preview", {
    method: "POST",
    body: { content },
  });
}

/**
 * Insert exactly the previewed set, all-or-nothing (POST
 * /admin/whitelist/confirm). A digest mismatch or DB collision is the
 * typed 409 `CONFLICT` (details carry the colliding `student_numbers`).
 */
export function confirmWhitelistImport(payload: {
  confirm_token: string;
  enable: boolean;
  student_numbers: string[];
}): Promise<WhitelistConfirmDto> {
  return apiRequest<WhitelistConfirmDto>("/api/v1/admin/whitelist/confirm", {
    method: "POST",
    body: payload,
  });
}

/**
 * Flip one entry (PATCH /admin/whitelist/{student_number}). The reason is
 * transport-OPTIONAL (it rides the per-entry audit rows when present);
 * an unknown number is the typed 400 with the missing list.
 */
export function toggleWhitelistEntry(
  studentNumber: string,
  payload: { enabled: boolean; reason?: string | null },
): Promise<WhitelistToggleDto> {
  return apiRequest<WhitelistToggleDto>(
    `/api/v1/admin/whitelist/${encodeId(studentNumber)}`,
    { method: "PATCH", body: payload },
  );
}

// --- reward catalogue + review grants (spec §16) -------------------------------------------

/**
 * The catalogue listing (GET /rewards — the STUDENT surface; enabled
 * items only, server-computed window verdict). The admin catalogue view
 * does not exist in the frozen contract; this is its read surface.
 */
export function listRewardCatalogue(
  init: { signal?: AbortSignal } = {},
): Promise<Schemas["RewardsListResponse"]> {
  return apiRequest<Schemas["RewardsListResponse"]>("/api/v1/rewards", {
    signal: init.signal,
  });
}

/** Create one ENABLED catalogue row (POST /admin/rewards; reason rides the audit). */
export function createRewardItem(body: RewardCreateBody): Promise<AdminRewardItemDto> {
  return apiRequest<AdminRewardItemDto>("/api/v1/admin/rewards", {
    method: "POST",
    body,
  });
}

/**
 * Partial update under the item row lock (PATCH /admin/rewards/{id}):
 * ABSENT leaves the column unchanged, explicit null clears a nullable
 * bound; cost/limit/stock edits bind FUTURE requests only.
 */
export function updateRewardItem(
  rewardItemId: string,
  body: RewardUpdateBody,
): Promise<AdminRewardItemDto> {
  return apiRequest<AdminRewardItemDto>(
    `/api/v1/admin/rewards/${encodeId(rewardItemId)}`,
    { method: "PATCH", body },
  );
}

/** 下架 (POST /admin/rewards/{id}/disable; reason mandatory; idempotent replay). */
export function disableRewardItem(
  rewardItemId: string,
  reason: string,
): Promise<AdminRewardItemDto> {
  return apiRequest<AdminRewardItemDto>(
    `/api/v1/admin/rewards/${encodeId(rewardItemId)}/disable`,
    { method: "POST", body: { reason } },
  );
}

/**
 * Grant the global REWARD_REVIEW authorization to a Teacher (POST
 * /admin/reward-review-grants; 204; a live grant is the typed 409).
 */
export function grantRewardReview(
  teacherId: string,
  reason: string,
): Promise<void> {
  return apiRequest<void>("/api/v1/admin/reward-review-grants", {
    method: "POST",
    body: { teacher_id: teacherId, reason },
  });
}

/**
 * Revoke the authorization (DELETE /admin/reward-review-grants/{teacher_id};
 * 204). The reason rides a QUERY parameter — a DELETE body is poorly
 * supported across clients (the backend router's documented choice).
 */
export function revokeRewardReview(
  teacherId: string,
  reason: string,
): Promise<void> {
  const params = new URLSearchParams({ reason });
  return apiRequest<void>(
    `/api/v1/admin/reward-review-grants/${encodeId(teacherId)}?${params.toString()}`,
    { method: "DELETE" },
  );
}

// --- redemption review queue (spec §16.2; the scoped-delegation surface) --------------------

/** The pending redemption queue, oldest first (GET /teacher/rewards/redemptions). */
export function listRedemptionQueue(query: PageQuery = {}): Promise<RedemptionPageDto> {
  return apiRequest<RedemptionPageDto>(
    `/api/v1/teacher/rewards/redemptions${pagination(query)}`,
    { signal: query.signal },
  );
}

/** Approve a pending redemption (POST .../approve; bare body by contract). */
export function approveRedemption(redemptionId: string): Promise<RedemptionReviewDto> {
  return apiRequest<RedemptionReviewDto>(
    `/api/v1/teacher/rewards/redemptions/${encodeId(redemptionId)}/approve`,
    { method: "POST" },
  );
}

/** Reject a pending redemption (POST .../reject; the reason is mandatory). */
export function rejectRedemption(
  redemptionId: string,
  reason: string,
): Promise<RedemptionReviewDto> {
  return apiRequest<RedemptionReviewDto>(
    `/api/v1/teacher/rewards/redemptions/${encodeId(redemptionId)}/reject`,
    { method: "POST", body: { reason } },
  );
}

/** Mark an APPROVED redemption delivered (POST .../fulfill; the note is optional). */
export function fulfillRedemption(
  redemptionId: string,
  note: string | null,
): Promise<RedemptionReviewDto> {
  return apiRequest<RedemptionReviewDto>(
    `/api/v1/teacher/rewards/redemptions/${encodeId(redemptionId)}/fulfill`,
    { method: "POST", body: { note } },
  );
}

// --- audit search ---------------------------------------------------------------------------

/** The read-only audit page (GET /admin/audit-logs; equality filters, newest first). */
export function listAuditLogs(
  query: PageQuery & {
    action?: string;
    actorUserId?: string;
    targetType?: string;
  } = {},
): Promise<AuditLogPageDto> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) {
    params.set("limit", String(query.limit));
  }
  if (query.offset !== undefined) {
    params.set("offset", String(query.offset));
  }
  if (query.action !== undefined && query.action !== "") {
    params.set("action", query.action);
  }
  if (query.actorUserId !== undefined && query.actorUserId !== "") {
    params.set("actor_user_id", query.actorUserId);
  }
  if (query.targetType !== undefined && query.targetType !== "") {
    params.set("target_type", query.targetType);
  }
  const encoded = params.toString();
  return apiRequest<AuditLogPageDto>(
    `/api/v1/admin/audit-logs${encoded.length > 0 ? `?${encoded}` : ""}`,
    { signal: query.signal },
  );
}

// --- system settings (typed keys; registry in backend system/service.py) --------------------

/**
 * The settings PUT body: the typed `{value}` plus the OPTIONAL `reason`
 * (T10's audit-completeness gap-fill) — included only when non-blank
 * after trim, so a bare-value PUT keeps the exact `{value}` wire shape.
 * The backend's reject-reason discipline applies: absent is legal,
 * whitespace-only is the typed 422 (refused there, never dropped here).
 */
function settingsPutBody(
  value: unknown,
  reason: string | undefined,
): Record<string, unknown> {
  const trimmed = reason?.trim() ?? "";
  return trimmed.length > 0 ? { value, reason: trimmed } : { value };
}

/** Every registered key's current state, registry order (GET /admin/settings). */
export function listSystemSettings(
  init: { signal?: AbortSignal } = {},
): Promise<SystemSettingsListDto> {
  return apiRequest<SystemSettingsListDto>("/api/v1/admin/settings", {
    signal: init.signal,
  });
}

/** The EFFECTIVE term (row value or deployment seed; GET .../current-academic-term). */
export function getCurrentAcademicTerm(
  init: { signal?: AbortSignal } = {},
): Promise<CurrentAcademicTermDto> {
  return apiRequest<CurrentAcademicTermDto>(
    "/api/v1/admin/settings/current-academic-term",
    { signal: init.signal },
  );
}

/** Turn the term (PUT; every later redemption snapshots the new term). */
export function putCurrentAcademicTerm(
  value: string,
  reason?: string,
): Promise<CurrentAcademicTermDto> {
  return apiRequest<CurrentAcademicTermDto>(
    "/api/v1/admin/settings/current-academic-term",
    { method: "PUT", body: settingsPutBody(value, reason) },
  );
}

/** Set the emoji allowlist (PUT; the empty list bans all emoji — spec §22). */
export function putEmojiWhitelist(
  value: string[],
  reason?: string,
): Promise<SystemSettingValueDto> {
  return apiRequest<SystemSettingValueDto>(
    "/api/v1/admin/settings/emoji-whitelist",
    { method: "PUT", body: settingsPutBody(value, reason) },
  );
}

/** Set the per-natural-day abandon cap (PUT; 0 disables abandoning — §12.4). */
export function putAbandonDailyLimit(
  value: number,
  reason?: string,
): Promise<SystemSettingValueDto> {
  return apiRequest<SystemSettingValueDto>(
    "/api/v1/admin/settings/abandon-daily-limit",
    { method: "PUT", body: settingsPutBody(value, reason) },
  );
}

/**
 * Toggle the management-network restriction (PUT). Enabling with an empty
 * effective CIDR list is the typed cross-key 422 (the backend's ruling).
 */
export function putManagementNetworkEnabled(
  value: boolean,
  reason?: string,
): Promise<SystemSettingValueDto> {
  return apiRequest<SystemSettingValueDto>(
    "/api/v1/admin/settings/management-network-enabled",
    { method: "PUT", body: settingsPutBody(value, reason) },
  );
}

/** Set the management-network CIDR allowlist (PUT; strict parsing, canonical storage). */
export function putManagementNetworkCidrs(
  value: string[],
  reason?: string,
): Promise<SystemSettingValueDto> {
  return apiRequest<SystemSettingValueDto>(
    "/api/v1/admin/settings/management-network-cidrs",
    { method: "PUT", body: settingsPutBody(value, reason) },
  );
}

// --- notification failures + templates (spec §25.4/§25.5) ------------------------------------

/** FAILED deliveries with provider-safe error metadata, newest first. */
export function listNotificationFailures(
  query: PageQuery = {},
): Promise<NotificationFailurePageDto> {
  return apiRequest<NotificationFailurePageDto>(
    `/api/v1/admin/notification-failures${pagination(query)}`,
    { signal: query.signal },
  );
}

/** Create the (event_type, channel) template at version 1 (typed 409 when it exists). */
export function createNotificationTemplate(payload: {
  event_type: TemplateEventTypeDto;
  channel: TemplateChannelDto;
  title: string;
  template_body: string;
}): Promise<AdminNotificationTemplateDto> {
  return apiRequest<AdminNotificationTemplateDto>(
    "/api/v1/admin/notification-templates",
    { method: "POST", body: payload },
  );
}

/** Edit title/body; version bumps by one (unsafe markup is the typed 422 here). */
export function updateNotificationTemplate(
  templateId: string,
  payload: { title: string; template_body: string },
): Promise<AdminNotificationTemplateDto> {
  return apiRequest<AdminNotificationTemplateDto>(
    `/api/v1/admin/notification-templates/${encodeId(templateId)}`,
    { method: "PATCH", body: payload },
  );
}

/** Enable a template (idempotent; version does not move). */
export function enableNotificationTemplate(
  templateId: string,
): Promise<AdminNotificationTemplateDto> {
  return apiRequest<AdminNotificationTemplateDto>(
    `/api/v1/admin/notification-templates/${encodeId(templateId)}/enable`,
    { method: "POST" },
  );
}

/** Disable a template (idempotent; version does not move). */
export function disableNotificationTemplate(
  templateId: string,
): Promise<AdminNotificationTemplateDto> {
  return apiRequest<AdminNotificationTemplateDto>(
    `/api/v1/admin/notification-templates/${encodeId(templateId)}/disable`,
    { method: "POST" },
  );
}

// --- named state repairs (Plan 08 T9's operational backstops) --------------------------------

/**
 * Put a dangling OCCUPIED Assignment back to AVAILABLE (POST; 409
 * `CONFLICT` when the occupancy is legitimate or the claim is not
 * release-terminal — the concurrent-conflict branch).
 */
export function releaseOccupiedAssignment(
  assignmentId: string,
  reason: string,
): Promise<ReleaseOccupiedAssignmentDto> {
  return apiRequest<ReleaseOccupiedAssignmentDto>(
    "/api/v1/admin/repairs/release-occupied-assignment",
    { method: "POST", body: { assignment_id: assignmentId, reason } },
  );
}

/** Fail a wedged SENDING delivery (POST; 409 `CONFLICT` for any non-SENDING status). */
export function forceFailDelivery(
  deliveryId: string,
  reason: string,
): Promise<ForceFailDeliveryDto> {
  return apiRequest<ForceFailDeliveryDto>(
    "/api/v1/admin/repairs/force-fail-delivery",
    { method: "POST", body: { delivery_id: deliveryId, reason } },
  );
}

// --- anonymous identity reveal (spec §21.4; G11/G12) -----------------------------------------

/**
 * The explicit, audited identity reveal: reason mandatory and capped
 * (POST /admin/comments/{id}/reveal-identity). The ONLY wire shape where
 * a student number is representable at all — call it solely from the
 * explicit confirm dialog, never while loading comment lists.
 */
export function revealCommentIdentity(
  commentId: string,
  reason: string,
): Promise<RevealIdentityDto> {
  return apiRequest<RevealIdentityDto>(
    `/api/v1/admin/comments/${encodeId(commentId)}/reveal-identity`,
    { method: "POST", body: { reason } },
  );
}
