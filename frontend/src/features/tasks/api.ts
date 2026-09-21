/**
 * Typed wrappers for the student task/claim endpoints (spec §8, §28;
 * backend `app/modules/tasks/router.py` + `transport_schemas.py`).
 * Shapes come from the GENERATED OpenAPI types in `lib/api/schema` — the
 * field names below are the backend contract (`base_reward_points`,
 * `assignments_available`, `grace_deadline_at`, ...).
 *
 * Claim is bodyless BY CONTRACT (spec §8.3): the server picks the
 * assignment randomly under lock. This client never sends — and has no
 * way to express — an assignment id; the response's platform/keyword are
 * the only assignment material a student surface ever receives (§42).
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** `TaskCardResponse` — the §42 card shape (counts only, never a list). */
export type TaskCardDto = Schemas["TaskCardResponse"];

/** `TaskListResponse` — one offset page of cards. */
export type TaskListPageDto = Schemas["TaskListResponse"];

/** `TaskDetailResponse` — published detail + the viewer's own claim. */
export type TaskDetailDto = Schemas["TaskDetailResponse"];

/** `ClaimResponse` — the owner-scoped claim view (platform/keyword). */
export type ClaimDto = Schemas["ClaimResponse"];

/** `MyClaimResponse` — a /me/claims row (claim view + task title). */
export type MyClaimDto = Schemas["MyClaimResponse"];

/** `MyClaimsResponse` — one offset page of own-claim history. */
export type MyClaimsPageDto = Schemas["MyClaimsResponse"];

/** Shared offset-pagination options (backend: limit 1..50, offset >= 0). */
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

/** One page of PUBLISHED task cards (GET /api/v1/tasks). */
export function listTasks(query: PageQuery = {}): Promise<TaskListPageDto> {
  return apiRequest<TaskListPageDto>(`/api/v1/tasks${pagination(query)}`, {
    signal: query.signal,
  });
}

/** Published task detail + own non-terminal claim (GET /api/v1/tasks/{id}). */
export function getTask(
  taskId: string,
  init: { signal?: AbortSignal } = {},
): Promise<TaskDetailDto> {
  return apiRequest<TaskDetailDto>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}`,
    { signal: init.signal },
  );
}

/**
 * Claim one random assignment (POST /api/v1/tasks/{id}/claim, 201).
 * No request body: allocation is the server's decision (spec §8.3).
 */
export function claimTask(taskId: string): Promise<ClaimDto> {
  return apiRequest<ClaimDto>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/claim`,
    { method: "POST" },
  );
}

/** Own claim history, newest first (GET /api/v1/me/claims). */
export function listMyClaims(query: PageQuery = {}): Promise<MyClaimsPageDto> {
  return apiRequest<MyClaimsPageDto>(`/api/v1/me/claims${pagination(query)}`, {
    signal: query.signal,
  });
}
