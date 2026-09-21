/**
 * Typed wrappers for the Teacher workbench endpoints (spec §41, §28;
 * backend `app/modules/tasks/router.py`, `app/modules/submissions/router.py`,
 * and the S2 `app/modules/community/router.py` moderation surfaces).
 *
 * Shapes for the tasks/submissions surfaces come from the GENERATED
 * OpenAPI types in `lib/api/schema` (the S1 contract is in the snapshot).
 * The community moderation endpoints below are HAND-WRITTEN — the same
 * documented pattern as `features/community/api.ts`: the branch snapshot
 * predates the S2 module, so those DTOs are transcribed field-by-field
 * from the S2 router's Pydantic models. MERGE-TIME STEP: when S2 merges,
 * replace the local shapes with `components["schemas"]` references and
 * drop this note (`git grep ModerationCommentDto src/features/admin`
 * finds every seam). The unit tests pin the wire paths/methods/bodies
 * against the S2 router so drift is caught at that boundary.
 *
 * PRIVACY SPOT-CHECK (binding constraint, spec §40/§21.4): the review
 * queue DTO carries platform/keyword — the sanctioned teacher judging
 * context — and NO student identity (not even a user id; contact fields
 * are unrepresentable in the wire shape). The moderation comment DTO
 * carries author_display only (匿名用户 for anonymous rows) plus the
 * pseudonymous moderation_key; the report DTO adds reporter nickname/id —
 * the §23 moderator surface. Nothing here renders a student number,
 * phone, email, or login identifier.
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

// --- DTOs (generated S1 contract) ---------------------------------------------------

/** `TeacherTaskListItemResponse` — one workbench list row, every status. */
export type TeacherTaskListItemDto = Schemas["TeacherTaskListItemResponse"];

/** `TeacherTaskListResponse` — one offset page of workbench rows. */
export type TeacherTaskListPageDto = Schemas["TeacherTaskListResponse"];

/** `TeacherTaskResponse` — full workbench detail incl. contract fields. */
export type TeacherTaskDto = Schemas["TeacherTaskResponse"];

/** `TaskTransitionResponse` — one lifecycle verb's landing state. */
export type TaskTransitionDto = Schemas["TaskTransitionResponse"];

/** `ImportPreviewResponse` — preview counts + per-row errors + token. */
export type ImportPreviewDto = Schemas["ImportPreviewResponse"];

/** `ImportPreviewErrorResponse` — one row-level or file-level error. */
export type ImportPreviewErrorDto = Schemas["ImportPreviewErrorResponse"];

/** `ImportConfirmResponse` — all-or-nothing insert count. */
export type ImportConfirmDto = Schemas["ImportConfirmResponse"];

/** `CollaboratorResponse` — a granted capability set. */
export type CollaboratorDto = Schemas["CollaboratorResponse"];

/** `TaskStatisticsResponse` — counts-only aggregate + rating. */
export type TaskStatisticsDto = Schemas["TaskStatisticsResponse"];

/** `ReviewQueueItemResponse` — one VALIDATED awaiting-decision row. */
export type ReviewQueueItemDto = Schemas["ReviewQueueItemResponse"];

/** `ReviewQueueResponse` — one offset page of the queue (collision-safe
 * generated name: the points module has a same-named schema). */
export type ReviewQueuePageDto =
  Schemas["app__modules__submissions__schemas__ReviewQueueResponse"];

/** `ApproveResponse` — the §14 outcome (claim landing + grant). */
export type ApproveResultDto = Schemas["ApproveResponse"];

/** `RevisionRequiredResponse` — claim state after 退回/判无效. */
export type RevisionResultDto = Schemas["RevisionRequiredResponse"];

/** `DownloadUrlResponse` — a short-lived presigned GET grant. */
export type DownloadGrantDto = Schemas["DownloadUrlResponse"];

// --- create-task body (TaskCreateRequest; task_type fixed to the V1 universe) -------

/** The closed V1 task-type universe (backend `TaskType`: DATA_CRAWL only). */
export const TASK_TYPES = ["DATA_CRAWL"] as const;
export type TaskTypeKey = (typeof TASK_TYPES)[number];

/** File types a task may allow (backend `SUPPORTED_FILE_TYPES`). */
export const ALLOWED_TASK_FILE_TYPES = ["CSV", "XLSX", "SQLITE"] as const;
export type TaskFileTypeKey = (typeof ALLOWED_TASK_FILE_TYPES)[number];

/** Platform upload cap mirror (backend `max_upload_bytes_default`, 200 MB). */
export const MAX_TASK_FILE_SIZE_BYTES = 200 * 1024 * 1024;

/** Create-task request body (spec §6; server is the validation authority). */
export interface TaskCreateBody {
  title: string;
  description: string;
  base_reward_points: number;
  deadline_mode: string;
  allowed_file_types: string[];
  max_file_size_bytes: number;
  task_type: TaskTypeKey;
  rarity: string;
  fixed_deadline_at: string | null;
  duration_minutes: number | null;
  claim_cutoff_minutes: number;
  submission_schema: Record<string, unknown> | null;
  submission_schema_version: number | null;
  notify_24h: boolean;
  notify_4h: boolean;
  notification_channels: string[];
}

// --- shared pagination options --------------------------------------------------------

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

// --- teacher tasks (spec §41) ---------------------------------------------------------

/** Workbench list: own + collaborated tasks, every status (GET /teacher/tasks). */
export function listTeacherTasks(
  query: PageQuery = {},
): Promise<TeacherTaskListPageDto> {
  return apiRequest<TeacherTaskListPageDto>(
    `/api/v1/teacher/tasks${pagination(query)}`,
    { signal: query.signal },
  );
}

/** Full workbench detail (GET /teacher/tasks/{id}; DRAFT readable). */
export function getTeacherTask(
  taskId: string,
  init: { signal?: AbortSignal } = {},
): Promise<TeacherTaskDto> {
  return apiRequest<TeacherTaskDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}`,
    { signal: init.signal },
  );
}

/** Create one DRAFT task owned by the actor (POST /teacher/tasks, 201). */
export function createTeacherTask(
  body: TaskCreateBody,
): Promise<TeacherTaskDto> {
  return apiRequest<TeacherTaskDto>("/api/v1/teacher/tasks", {
    method: "POST",
    body,
  });
}

/** The five lifecycle verbs (spec §6.2 transition table). */
export const TASK_LIFECYCLE_VERBS = [
  "publish",
  "pause",
  "resume",
  "close",
  "archive",
] as const;
export type TaskLifecycleVerb = (typeof TASK_LIFECYCLE_VERBS)[number];

/** Narrow a string to a lifecycle verb (URL building stays total). */
export function isLifecycleVerb(value: string): value is TaskLifecycleVerb {
  return (TASK_LIFECYCLE_VERBS as readonly string[]).includes(value);
}

/**
 * Run one lifecycle verb (POST /teacher/tasks/{id}/{verb}). The response
 * is the server's landing verdict — callers update rows from it, never
 * from a client-side transition guess (patterns §3).
 */
export function runTaskLifecycle(
  taskId: string,
  verb: TaskLifecycleVerb,
): Promise<TaskTransitionDto> {
  return apiRequest<TaskTransitionDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/${verb}`,
    { method: "POST" },
  );
}

/**
 * Parse and pre-check a CSV upload (spec §7.1 steps 1-4). The file is
 * the RAW request body with `Content-Type: text/csv` — the backend's
 * documented transport choice (no multipart); the byte cap rejects
 * oversize payloads before any parsing.
 */
export function previewAssignmentImport(
  taskId: string,
  file: Blob,
): Promise<ImportPreviewDto> {
  return apiRequest<ImportPreviewDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/assignments/import/preview`,
    { method: "POST", body: file, headers: { "Content-Type": "text/csv" } },
  );
}

/** Insert exactly the previewed rows (spec §7.1 steps 5-6; token is single-use). */
export function confirmAssignmentImport(
  taskId: string,
  previewToken: string,
): Promise<ImportConfirmDto> {
  return apiRequest<ImportConfirmDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/assignments/import/confirm`,
    { method: "POST", body: { preview_token: previewToken } },
  );
}

/** Grant a capability set to a Teacher (PUT /teacher/tasks/{id}/collaborators/{tid}). */
export function addCollaborator(
  taskId: string,
  teacherId: string,
  permissions: string[],
): Promise<CollaboratorDto> {
  return apiRequest<CollaboratorDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/collaborators/${encodeURIComponent(teacherId)}`,
    { method: "PUT", body: { permissions } },
  );
}

/** Remove a collaborator (DELETE, 204; owner/Admin only — the server decides). */
export function removeCollaborator(
  taskId: string,
  teacherId: string,
): Promise<void> {
  return apiRequest<void>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/collaborators/${encodeURIComponent(teacherId)}`,
    { method: "DELETE" },
  );
}

/** Counts-only workbench aggregate (GET /teacher/tasks/{id}/statistics). */
export function getTaskStatistics(
  taskId: string,
  init: { signal?: AbortSignal } = {},
): Promise<TaskStatisticsDto> {
  return apiRequest<TaskStatisticsDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/statistics`,
    { signal: init.signal },
  );
}

// --- teacher review surface (spec §11.3, §12.4, §14, §41) ------------------------------

/** One offset page of the review queue, oldest first (GET /teacher/submissions/review-queue). */
export function listReviewQueue(
  query: PageQuery = {},
): Promise<ReviewQueuePageDto> {
  return apiRequest<ReviewQueuePageDto>(
    `/api/v1/teacher/submissions/review-queue${pagination(query)}`,
    { signal: query.signal },
  );
}

/** The §14 ten-step approve transaction (replay answers already_reviewed). */
export function approveSubmission(
  submissionId: string,
): Promise<ApproveResultDto> {
  return apiRequest<ApproveResultDto>(
    `/api/v1/teacher/submissions/${encodeURIComponent(submissionId)}/approve`,
    { method: "POST" },
  );
}

/** 退回修改 (spec §11.3): the note is mandatory at the transport. */
export function requireRevision(
  submissionId: string,
  note: string,
): Promise<RevisionResultDto> {
  return apiRequest<RevisionResultDto>(
    `/api/v1/teacher/submissions/${encodeURIComponent(submissionId)}/revision-required`,
    { method: "POST", body: { note } },
  );
}

/** 判无效 (spec §11.3): cancels the PROVISIONAL lock; the reason is mandatory. */
export function invalidateRewardLock(
  submissionId: string,
  reason: string,
): Promise<RevisionResultDto> {
  return apiRequest<RevisionResultDto>(
    `/api/v1/teacher/submissions/${encodeURIComponent(submissionId)}/invalidate-reward-lock`,
    { method: "POST", body: { reason } },
  );
}

/**
 * Mint a short-lived presigned GET (spec §33.3). Authorized for the
 * reviewing teacher; the URL expires in minutes — mint on click, never
 * embed a presigned URL in a listed page.
 */
export function mintSubmissionDownload(
  submissionId: string,
): Promise<DownloadGrantDto> {
  return apiRequest<DownloadGrantDto>(
    `/api/v1/submissions/${encodeURIComponent(submissionId)}/download`,
  );
}

// --- S2 community moderation (HAND-WRITTEN contract; see the module note) -------------

/** `ModerationCommentResponse` — the Teacher-safe comment shape (§21.4). */
export interface ModerationCommentDto {
  id: string;
  task_id: string;
  parent_id: string | null;
  content: string | null;
  is_anonymous: boolean;
  /** nickname | 匿名用户 (server-resolved; NO identity field exists here). */
  author_display: string;
  created_at: string;
  updated_at: string;
  edited: boolean;
  deleted: boolean;
  /** Pseudonymous key, present exactly on anonymous rows (§21.4). */
  moderation_key: string | null;
  hard_hidden: boolean;
}

/** `ModerationCommentListResponse` — one offset page. */
export interface ModerationCommentPageDto {
  items: ModerationCommentDto[];
  total: number;
  limit: number;
  offset: number;
}

/** `CommentReportResponse` — one report-queue row (§23 moderator surface). */
export interface ModerationReportDto {
  id: string;
  comment: ModerationCommentDto;
  category: string;
  note: string | null;
  status: string;
  /** Reporter identity — sanctioned on the moderation surface only. */
  reporter_user_id: string | null;
  reporter_nickname: string | null;
  created_at: string;
  handled_by: string | null;
  handled_at: string | null;
}

/** `CommentReportListResponse` — one offset page. */
export interface ModerationReportPageDto {
  items: ModerationReportDto[];
  total: number;
  limit: number;
  offset: number;
}

/** Teacher-safe comment listing for one task (GET /teacher/tasks/{id}/comments/moderation). */
export function listModerationComments(
  taskId: string,
  query: PageQuery = {},
): Promise<ModerationCommentPageDto> {
  return apiRequest<ModerationCommentPageDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/comments/moderation${pagination(query)}`,
    { signal: query.signal },
  );
}

/** Per-task report queue (GET /teacher/tasks/{id}/reports). */
export function listTaskReports(
  taskId: string,
  query: PageQuery = {},
): Promise<ModerationReportPageDto> {
  return apiRequest<ModerationReportPageDto>(
    `/api/v1/teacher/tasks/${encodeURIComponent(taskId)}/reports${pagination(query)}`,
    { signal: query.signal },
  );
}

/** Moderation soft delete (DELETE /teacher/comments/{id}; reason mandatory, audited). */
export function moderateDeleteComment(
  commentId: string,
  reason: string,
): Promise<void> {
  return apiRequest<void>(
    `/api/v1/teacher/comments/${encodeURIComponent(commentId)}`,
    { method: "DELETE", body: { reason } },
  );
}
