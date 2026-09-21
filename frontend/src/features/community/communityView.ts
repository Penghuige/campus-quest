/**
 * Pure community views (spec §21-§24; patterns §3/§4/§12/§15).
 *
 * PRIVACY PIN (spec §21.4/§40, patterns §12): a rendered comment row
 * carries EXACTLY the public display fields — author_display (the
 * server already resolved 匿名用户/该评论已删除), is_anonymous, content,
 * timing, edited, deleted. The DTO has no user-id/contact field to
 * leak, and this module derives none; `commentRowView` is the single
 * choke point every rendered row goes through and the unit tests pin
 * its shape (Object.keys) plus the serialized bytes.
 *
 * XSS (spec §33.1 所有用户文本按纯文本处理): content passes through
 * VERBATIM as a plain string — nothing here escapes, strips, or
 * interprets markup, and no component renders it as anything but a
 * React text node (no dangerouslySetInnerHTML anywhere; pinned by
 * `__tests__/community-view.test.ts` + a repo grep in the task gate).
 *
 * TWO-LEVEL THREADING (spec §21.2): the server returns one flat
 * ordered page; `commentThreadView` groups replies under their ROOT
 * comment. A reply-to-a-reply stays in the root's group at the same
 * second visual level — indentation never grows past two levels.
 * Deleted parents that still anchor replies arrive as tombstones
 * (`deleted`, null content) and keep their children (spec §21.3).
 */
import { isApiError, describeSectionError } from "@/lib/errors";

import type { CommentDto, CommentSortKey } from "./api";
import { REPORT_CATEGORY_OPTIONS } from "./api";

// --- identity choice (spec §21.4: anonymous is per-comment, explicit) ---------------

export type IdentityMode = "named" | "anonymous";

/**
 * V1 RULING: the composer defaults to 公开昵称 — anonymity is the
 * deliberate opt-in, not the default posture (an explicit choice must
 * precede posting either way; design-system §9 "anonymous mode must be
 * explicit before posting").
 */
export const DEFAULT_IDENTITY_MODE: IdentityMode = "named";

export const IDENTITY_MODE_OPTIONS: readonly {
  key: IdentityMode;
  label: string;
}[] = [
  { key: "named", label: "公开昵称" },
  { key: "anonymous", label: "匿名" },
];

export function isIdentityMode(value: string): value is IdentityMode {
  return value === "named" || value === "anonymous";
}

/**
 * The clear preview of the chosen identity mode (brief step 2): what
 * the posted comment's author line will say. The nickname is the
 * session's own display value — passing it in only ever makes the
 * preview MORE explicit; the server still resolves the stored display.
 */
export function identityPreview(
  mode: IdentityMode,
  nickname: string | null,
): string {
  if (mode === "anonymous") {
    return "将以「匿名用户」身份发布，不显示你的昵称";
  }
  return nickname !== null && nickname.length > 0
    ? `将以公开昵称「${nickname}」发布，所有同学可见`
    : "将以公开昵称发布，所有同学可见";
}

// --- composer content mirror (spec §21.1; backend normalize_comment_content) --------

export type CommentDraft =
  | { ok: true; value: string }
  | { ok: false; reason: "empty" | "too-long"; maxLength: number };

/** Code-point length — the unit the backend cap is measured in (Python len). */
export function codePointLength(text: string): number {
  return Array.from(text).length;
}

// Control characters (Unicode Cc) except \n and \t — the backend keeps
// those two so multi-line comments stay expressible; NUL/CR/ESC/DEL
// disappear (log/terminal-forging material), and \r removal normalizes
// CRLF to LF.
function stripControlCharacters(text: string): string {
  return text.replace(/[\p{Cc}]/gu, (ch) =>
    ch === "\n" || ch === "\t" ? ch : "",
  );
}

/**
 * Client mirror of the backend normalizer (patterns §6: convenience
 * validation; the server re-validates and stays the authority):
 * strip dangerous control characters -> trim -> reject whitespace-only
 * -> enforce the cap on the NORMALIZED text, boundary inclusive.
 */
export function normalizeCommentDraft(
  raw: string,
  maxLength: number,
): CommentDraft {
  const normalized = stripControlCharacters(raw).trim();
  if (normalized.length === 0) {
    return { ok: false, reason: "empty", maxLength };
  }
  if (codePointLength(normalized) > maxLength) {
    return { ok: false, reason: "too-long", maxLength };
  }
  return { ok: true, value: normalized };
}

/** Muted excerpt for the reply-context preview (parent quote). */
export function commentExcerpt(
  content: string | null,
  maxChars = 60,
): string {
  if (content === null) {
    return "该评论已删除";
  }
  const chars = Array.from(content);
  return chars.length <= maxChars
    ? content
    : `${chars.slice(0, maxChars).join("")}…`;
}

// --- comment rows (the privacy pin) -------------------------------------------------

/** Uniform tombstone display (spec §21.3 该评论已删除; server sends content null). */
export const TOMBSTONE_TEXT = "该评论已删除";

/**
 * EXACTLY the public display fields — the privacy choke point. Content
 * rides verbatim as a plain string (XSS pin): a `<script>` payload is
 * data, rendered as text by the component layer.
 */
export interface CommentRowView {
  id: string;
  authorDisplay: string;
  isAnonymous: boolean;
  /** null only on tombstones; a deleted row NEVER exposes stored text. */
  content: string | null;
  createdAtMs: number;
  edited: boolean;
  deleted: boolean;
}

export function commentRowView(dto: CommentDto): CommentRowView {
  return {
    id: dto.id,
    authorDisplay: dto.author_display,
    isAnonymous: dto.is_anonymous,
    content: dto.deleted ? null : dto.content,
    createdAtMs: Date.parse(dto.created_at),
    edited: dto.edited,
    deleted: dto.deleted,
  };
}

// --- two-level thread grouping (spec §21.2/§21.3) -----------------------------------

export interface CommentThreadGroup {
  /** Root row (a tombstone when the deleted parent still anchors replies). */
  root: CommentRowView;
  /** ALL descendants, flattened to the second visual level, oldest first. */
  replies: CommentRowView[];
  /** True when the root's own parent is outside the loaded pages — the
   * flat server page can split a thread across page boundaries; the UI
   * hints instead of silently pretending it is a root comment. */
  orphanRoot: boolean;
}

export interface ThreadView {
  groups: CommentThreadGroup[];
  /** Count of comments whose ancestors are not all loaded. */
  orphanCount: number;
}

/**
 * Group one accumulated flat page-set into two-level threads.
 *
 * - roots: `parent_id` null, or a parent NOT in the loaded set (the
 *   page boundary case — flagged `orphanRoot`, never silently demoted);
 * - replies: every descendant of a root, BFS order, then re-sorted
 *   oldest-first so the conversation reads top-down within the group;
 * - cross-group order: the server's own page order (latest or hot) —
 *   the view never re-sorts roots (patterns §3: server ordering is the
 *   verdict, esp. for hot).
 */
export function commentThreadView(dtos: CommentDto[]): ThreadView {
  const byId = new Map(dtos.map((dto) => [dto.id, dto]));
  const childrenOf = new Map<string, CommentDto[]>();
  const groups: CommentThreadGroup[] = [];
  let orphanCount = 0;

  for (const dto of dtos) {
    if (dto.parent_id !== null && byId.has(dto.parent_id)) {
      const bucket = childrenOf.get(dto.parent_id);
      if (bucket === undefined) {
        childrenOf.set(dto.parent_id, [dto]);
      } else {
        bucket.push(dto);
      }
    }
  }

  const collectDescendants = (rootId: string): CommentDto[] => {
    const collected: CommentDto[] = [];
    const queue = [rootId];
    while (queue.length > 0) {
      const current = queue.shift()!;
      for (const child of childrenOf.get(current) ?? []) {
        collected.push(child);
        queue.push(child.id);
      }
    }
    // Oldest first inside the group (conversation order); id is the
    // stable tie-break mirroring the server's ordering discipline.
    return collected.sort((a, b) => {
      const byTime = Date.parse(a.created_at) - Date.parse(b.created_at);
      return byTime !== 0 ? byTime : (a.id < b.id ? -1 : 1);
    });
  };

  for (const dto of dtos) {
    const isRoot = dto.parent_id === null || !byId.has(dto.parent_id);
    if (!isRoot) {
      continue;
    }
    const orphanRoot = dto.parent_id !== null;
    if (orphanRoot) {
      orphanCount += 1;
    }
    groups.push({
      root: commentRowView(dto),
      replies: collectDescendants(dto.id).map(commentRowView),
      orphanRoot,
    });
  }

  return { groups, orphanCount };
}

// --- sort tabs (patterns §4: shareable tab state rides the URL) ----------------------

export const COMMENT_SORTS: readonly { key: CommentSortKey; label: string }[] =
  [
    { key: "latest", label: "最新" },
    { key: "hot", label: "热门" },
  ];

export const DEFAULT_COMMENT_SORT: CommentSortKey = "latest";

export function parseCommentSort(
  value: string | undefined,
  fallback: CommentSortKey = DEFAULT_COMMENT_SORT,
): CommentSortKey {
  return value === "latest" || value === "hot" ? value : fallback;
}

// --- report draft (spec §23: closed categories, optional bounded note) ---------------

export type ReportDraft =
  | { ok: true; category: string; note: string | null }
  | {
      ok: false;
      reason: "category-required" | "category-invalid" | "note-too-long";
      maxLength: number;
    };

/**
 * Mirror of the backend rules: the category must be the exact enum
 * string from the closed §23 set; the note is trimmed, blank means
 * none, and the trimmed text is capped (backend default 500).
 */
export function reportDraftView(
  category: string,
  note: string,
  noteMaxLength: number,
): ReportDraft {
  const known = REPORT_CATEGORY_OPTIONS.some(
    (option) => option.value === category,
  );
  if (category.length === 0) {
    return { ok: false, reason: "category-required", maxLength: noteMaxLength };
  }
  if (!known) {
    return { ok: false, reason: "category-invalid", maxLength: noteMaxLength };
  }
  const trimmed = note.trim();
  if (codePointLength(trimmed) > noteMaxLength) {
    return { ok: false, reason: "note-too-long", maxLength: noteMaxLength };
  }
  return {
    ok: true,
    category,
    note: trimmed.length === 0 ? null : trimmed,
  };
}

// --- rating (spec §20: completer-only, aggregate public) -----------------------------

export const RATING_STEPS: readonly number[] = [1, 2, 3, 4, 5] as const;

export const RATING_NOT_ELIGIBLE_COPY = "完成任务后才能评价该任务";

// --- votes (spec §22: 1 like / -1 dislike / 0 remove; same value toggles off) --------

export type VoteDirection = 1 | -1;

/**
 * The value to SEND for one tap: pressing the button matching the
 * caller's current stance REMOVES the vote (0); otherwise it sets that
 * direction. The POST response is authoritative (patterns §7 — the
 * echo's `current_value`/`likes`/`dislikes` overwrite local state).
 */
export function nextVoteValue(
  current: number,
  direction: VoteDirection,
): 1 | 0 | -1 {
  return current === direction ? 0 : direction;
}

// --- mutation error copy (patterns §15: branch on error.code) ------------------------

export interface CommunityErrorView {
  message: string;
  requestId: string | null;
}

const COMMUNITY_ERROR_COPY: Record<string, string> = {
  RATING_NOT_ELIGIBLE: RATING_NOT_ELIGIBLE_COPY,
  RATE_LIMITED: "操作过于频繁，请稍后再试",
  VALIDATION_ERROR: "内容不符合要求，请检查后重试",
  PERMISSION_DENIED: "没有权限执行该操作",
};

/**
 * Community-specific typed copy first (code-keyed), then the shared
 * section tiers (network / system + request id / known-code server
 * message / registry-drift generic). Callers render `message` beside
 * the control that failed; `requestId` rides unexpected failures.
 */
export function describeCommunityError(error: unknown): CommunityErrorView {
  if (isApiError(error)) {
    const copy = COMMUNITY_ERROR_COPY[error.code];
    if (copy !== undefined) {
      return { message: copy, requestId: null };
    }
  }
  return describeSectionError(error);
}
