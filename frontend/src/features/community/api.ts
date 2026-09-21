/**
 * Typed wrappers for the community endpoints (spec §20-§24; backend
 * `app/modules/community/router.py` + `schemas.py`, Stream S2).
 *
 * HAND-WRITTEN CONTRACT, NOT GENERATED: `openapi.snapshot.json` on this
 * branch predates the community module (it ends at the S1/S3 surface),
 * so these DTOs are transcribed field-by-field from the S2 router's
 * Pydantic models. MERGE-TIME STEP: when the S2 stream merges, point
 * `npm run api:types` at the combined backend and replace these local
 * shapes with `components["schemas"]["CommentPublicResponse"]` etc.
 * (`git grep CommentPublicResponse src/features/community` finds every
 * seam). The unit tests in `__tests__/community-api.test.ts` pin the
 * wire paths/methods/bodies against the S2 router so drift is caught
 * at that boundary.
 *
 * PRIVACY BY CONSTRUCTION (spec §21.4/§40): `CommentDto` carries NO
 * author identity beyond `author_display` (nickname for named comments,
 * 匿名用户 for anonymous ones) — no user id, student number, phone, or
 * email field exists in the wire shape to leak. Tombstones carry
 * `content: null` with the uniform 该评论已删除 display.
 */
import { apiRequest } from "@/lib/api";

// --- DTOs (transcribed from S2 `router.py` wire schemas) ---------------------------

/** `CommentPublicResponse` — exactly the privacy-safe public shape. */
export interface CommentDto {
  id: string;
  task_id: string;
  parent_id: string | null;
  /** null exactly on tombstones (deleted parents kept for anchoring). */
  content: string | null;
  is_anonymous: boolean;
  /** nickname | 匿名用户 | 该评论已删除 (server-resolved display only). */
  author_display: string;
  created_at: string;
  updated_at: string;
  edited: boolean;
  deleted: boolean;
}

/** `CommentListResponse` — one offset page of the public thread. */
export interface CommentPageDto {
  items: CommentDto[];
  total: number;
  limit: number;
  offset: number;
}

/** Sort keys the backend accepts (spec §24; hot is server-computed). */
export type CommentSortKey = "latest" | "hot";

/** `VoteResponse` — the caller's post-transition stance + comment totals. */
export interface VoteResultDto {
  current_value: number; // 1 | 0 | -1 AFTER the transition
  likes: number;
  dislikes: number;
}

/** `ReactionResponse` — toggle verdict + the comment's per-emoji counts. */
export interface ReactionResultDto {
  emoji: string;
  added: boolean;
  counts: Record<string, number>;
}

/** `ReportResponse` — the caller's OWN report row (201). */
export interface ReportDto {
  id: string;
  comment_id: string;
  category: string;
  note: string | null;
  status: string;
  created_at: string;
}

/** `RatingResponse` — the rater's own rating echo (public stays aggregate-only). */
export interface RatingResultDto {
  task_id: string;
  rating: number;
  created_at: string;
  updated_at: string;
}

// --- closed sets (backend-owned; mirrored for the UI) ------------------------------

/**
 * Spec §22 default emoji whitelist — the eight V1 ships with. The
 * backend's set is Admin-configurable (`EmojiWhitelistPort`); a reaction
 * outside it answers the typed VALIDATION_ERROR copy, so the buttons
 * render from this mirror and the server stays the authority.
 */
export const DEFAULT_EMOJI_WHITELIST: readonly string[] = [
  "👍",
  "❤️",
  "😂",
  "🎉",
  "😭",
  "👀",
  "🤔",
  "🔥",
] as const;

/**
 * Closed report-category set (spec §23; backend `ReportCategory` enum,
 * exact-string membership). Labels are product wording; the VALUE sent
 * is the enum string.
 */
export const REPORT_CATEGORY_OPTIONS: readonly {
  value: string;
  label: string;
}[] = [
  { value: "SPAM", label: "垃圾信息" },
  { value: "HARASSMENT", label: "骚扰辱骂" },
  { value: "PRIVACY", label: "侵犯隐私" },
  { value: "OTHER", label: "其他" },
];

/** Spec §21.1 default comment cap (`Settings.comment_max_length`, 2000). */
export const DEFAULT_COMMENT_MAX_LENGTH = 2000;

/** Backend default report-note cap (`DEFAULT_REPORT_NOTE_MAX_LENGTH`). */
export const DEFAULT_REPORT_NOTE_MAX_LENGTH = 500;

/** Backend comment-page bounds (router: limit 1..50, default 20). */
export const COMMENT_PAGE_LIMIT = 20;

// --- endpoints ---------------------------------------------------------------------

export interface CommentListQuery {
  sort?: CommentSortKey;
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}

/** One offset page of a task's public comments (GET /tasks/{id}/comments). */
export function listTaskComments(
  taskId: string,
  query: CommentListQuery = {},
): Promise<CommentPageDto> {
  const params = new URLSearchParams();
  if (query.sort !== undefined) {
    params.set("sort", query.sort);
  }
  if (query.limit !== undefined) {
    params.set("limit", String(query.limit));
  }
  if (query.offset !== undefined) {
    params.set("offset", String(query.offset));
  }
  const encoded = params.toString();
  return apiRequest<CommentPageDto>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/comments${encoded.length > 0 ? `?${encoded}` : ""}`,
    { signal: query.signal },
  );
}

export interface CreateCommentBody {
  content: string;
  /** null/absent means a root comment (spec §21.2). */
  parent_id?: string | null;
  /** display attribute of THIS comment only (spec §21.4). */
  is_anonymous?: boolean;
}

/** Publish one comment or reply immediately (POST, 201; spec §21.1). */
export function createTaskComment(
  taskId: string,
  body: CreateCommentBody,
): Promise<CommentDto> {
  return apiRequest<CommentDto>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/comments`,
    { method: "POST", body },
  );
}

/** Owner edit, last-write-wins (PATCH /comments/{id}; spec §21.3). */
export function editOwnComment(
  commentId: string,
  content: string,
): Promise<CommentDto> {
  return apiRequest<CommentDto>(
    `/api/v1/comments/${encodeURIComponent(commentId)}`,
    { method: "PATCH", body: { content } },
  );
}

/** Owner soft delete (DELETE /comments/{id}, 204; tombstone rules are the server's). */
export function deleteOwnComment(commentId: string): Promise<void> {
  return apiRequest<void>(
    `/api/v1/comments/${encodeURIComponent(commentId)}`,
    { method: "DELETE" },
  );
}

/** Set the caller's stance (POST /comments/{id}/vote; 1 like, 0 remove, -1 dislike). */
export function castCommentVote(
  commentId: string,
  value: 1 | 0 | -1,
): Promise<VoteResultDto> {
  return apiRequest<VoteResultDto>(
    `/api/v1/comments/${encodeURIComponent(commentId)}/vote`,
    { method: "POST", body: { value } },
  );
}

/** Toggle one whitelisted emoji (POST /comments/{id}/reactions; spec §22). */
export function toggleCommentReaction(
  commentId: string,
  emoji: string,
): Promise<ReactionResultDto> {
  return apiRequest<ReactionResultDto>(
    `/api/v1/comments/${encodeURIComponent(commentId)}/reactions`,
    { method: "POST", body: { emoji } },
  );
}

export interface ReportBody {
  category: string;
  note?: string | null;
}

/** File one report into the moderation queue (POST, 201; spec §23). */
export function reportComment(
  commentId: string,
  body: ReportBody,
): Promise<ReportDto> {
  return apiRequest<ReportDto>(
    `/api/v1/comments/${encodeURIComponent(commentId)}/reports`,
    { method: "POST", body },
  );
}

/** Completer rating upsert (PUT /tasks/{id}/rating; RATING_NOT_ELIGIBLE on 403). */
export function putTaskRating(
  taskId: string,
  rating: number,
): Promise<RatingResultDto> {
  return apiRequest<RatingResultDto>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/rating`,
    { method: "PUT", body: { rating } },
  );
}
