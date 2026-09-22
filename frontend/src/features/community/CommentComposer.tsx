"use client";
/**
 * Comment composer (spec §21.1/§21.4; design-system §9 Comments; brief
 * step 2): an EXPLICIT identity choice must precede posting, with a
 * clear preview of the chosen mode.
 *
 * - Identity default is 公开昵称 — the documented V1 ruling
 *   (`DEFAULT_IDENTITY_MODE` in communityView): anonymity is a
 *   deliberate per-comment opt-in, and the toggle + preview keep the
 *   choice explicit either way. The chosen mode persists across posts
 *   within the session for convenience but is never implied.
 * - Content rules MIRROR the backend normalizer (strip Cc except
 *   \n/\t, trim, reject whitespace-only, 2000 code-point cap — the
 *   spec §21.1 configurable default). Client validation is feedback
 *   only; the server re-validates (patterns §6).
 * - Pessimistic submit (patterns §7): the comment appears only from
 *   the 201 response; typed failures (RATE_LIMITED, VALIDATION_ERROR)
 *   render code-keyed copy beside the form and the draft is kept.
 */
import { useId, useState } from "react";

import { useSession } from "@/features/auth/session";

import {
  createTaskComment,
  DEFAULT_COMMENT_MAX_LENGTH,
  type CommentDto,
} from "./api";
import {
  codePointLength,
  DEFAULT_IDENTITY_MODE,
  describeCommunityError,
  identityPreview,
  IDENTITY_MODE_OPTIONS,
  normalizeCommentDraft,
  type IdentityMode,
} from "./communityView";

export interface CommentComposerParent {
  id: string;
  authorDisplay: string;
  excerpt: string;
}

export interface CommentComposerProps {
  taskId: string;
  /** Reply context; absent means a root comment. */
  parent?: CommentComposerParent;
  /** Called with the 201 response after a successful publish. */
  onPublished: (comment: CommentDto) => void;
  /** Reply mode: cancel returns to the plain thread view. */
  onCancel?: () => void;
}

export function CommentComposer({
  taskId,
  parent,
  onPublished,
  onCancel,
}: CommentComposerProps) {
  const contentId = useId();
  const identityId = useId();
  const session = useSession();
  const [mode, setMode] = useState<IdentityMode>(DEFAULT_IDENTITY_MODE);
  const [content, setContent] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<{
    message: string;
    requestId: string | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);

  const nickname =
    session.state.status === "authenticated" ? session.state.me.nickname : null;
  const count = codePointLength(content);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    setSubmitError(null);
    const draft = normalizeCommentDraft(content, DEFAULT_COMMENT_MAX_LENGTH);
    if (!draft.ok) {
      setFieldError(
        draft.reason === "empty"
          ? "评论内容不能为空"
          : `评论内容不能超过 ${draft.maxLength} 个字符`,
      );
      return;
    }
    setFieldError(null);
    setBusy(true);
    try {
      const published = await createTaskComment(taskId, {
        content: draft.value,
        parent_id: parent?.id ?? null,
        is_anonymous: mode === "anonymous",
      });
      setContent("");
      onPublished(published);
    } catch (error) {
      const view = describeCommunityError(error);
      setSubmitError(view);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="composer" onSubmit={onSubmit}>
      {parent !== undefined ? (
        <div className="reply-context">
          <p className="reply-context-line">
            回复 <span className="reply-context-author">{parent.authorDisplay}</span>
            ：{parent.excerpt}
          </p>
          {onCancel !== undefined ? (
            <button type="button" className="btn btn-ghost" onClick={onCancel}>
              取消回复
            </button>
          ) : null}
        </div>
      ) : null}

      <fieldset className="identity-toggle">
        <legend id={identityId}>发布身份</legend>
        <div className="identity-options" role="radiogroup" aria-labelledby={identityId}>
          {IDENTITY_MODE_OPTIONS.map((option) => (
            <label key={option.key} className="identity-option">
              <input
                type="radio"
                name={`${identityId}-mode`}
                value={option.key}
                checked={mode === option.key}
                onChange={() => setMode(option.key)}
              />
              <span>{option.label}</span>
            </label>
          ))}
        </div>
        <p className="identity-preview" aria-live="polite">
          {identityPreview(mode, nickname)}
        </p>
      </fieldset>

      <div className="field">
        <label className="field-label" htmlFor={contentId}>
          {parent !== undefined ? "回复内容" : "评论内容"}
        </label>
        <textarea
          id={contentId}
          className="input composer-input"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          rows={3}
          maxLength={DEFAULT_COMMENT_MAX_LENGTH * 2}
          aria-invalid={fieldError !== null}
          aria-describedby={
            fieldError !== null ? `${contentId}-error` : undefined
          }
          placeholder={
            parent !== undefined ? "回复这条评论…" : "说说你完成这个任务的体验…"
          }
        />
        <div className="composer-meta">
          {fieldError !== null ? (
            <p className="field-error" id={`${contentId}-error`} role="alert">
              {fieldError}
            </p>
          ) : (
            <p className="field-hint">支持多行文本，发布后立即公开可见。</p>
          )}
          <span className="field-counter" data-over={count > DEFAULT_COMMENT_MAX_LENGTH}>
            {count}/{DEFAULT_COMMENT_MAX_LENGTH}
          </span>
        </div>
      </div>

      {submitError !== null ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">!</span>
            {submitError.message}
          </p>
          {submitError.requestId !== null ? (
            <p className="req-id">请求 ID：{submitError.requestId}</p>
          ) : null}
        </div>
      ) : null}

      <div className="composer-actions">
        <button
          type="submit"
          className="btn btn-primary"
          disabled={busy}
          aria-busy={busy}
        >
          {busy ? <span className="spinner" aria-hidden="true" /> : null}
          <span>{parent !== undefined ? "发布回复" : "发布评论"}</span>
        </button>
      </div>
    </form>
  );
}
