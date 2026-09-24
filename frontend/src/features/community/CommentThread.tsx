"use client";
/**
 * Task community thread (spec §21-§24; patterns §8 "Task detail"
 * archetype's community section; brief steps 3-4).
 *
 * Fetches one offset page at a time (server sort `latest|hot`, §24 —
 * hot ordering is the server's, never recomputed here) and ACCUMULATES
 * pages client-side so `commentThreadView` can group the flat list
 * into two-level threads (deep replies stay in the root group, §21.2)
 * across page boundaries; a reply whose parent sits on an unloaded
 * page renders at root level behind an 上级评论未加载 hint instead of
 * being silently demoted.
 *
 * Publishes insert LOCALLY from the 201 response (the response IS that
 * comment's server state) and re-group immediately; cross-comment
 * ordering reconciles on the next page load or sort switch (patterns
 * §3). Tombstones render the uniform 该评论已删除 line with surviving
 * children below (§21.3); replying to a tombstone is server-refused,
 * so tombstone rows expose no reply affordance at all.
 */
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { mergeOffsetPage } from "@/lib/offsetPages";
import { formatDeadlineDateTime } from "@/lib/time";

import {
  COMMENT_PAGE_LIMIT,
  listTaskComments,
  REPORT_CATEGORY_OPTIONS,
  reportComment,
  DEFAULT_REPORT_NOTE_MAX_LENGTH,
  type CommentDto,
  type CommentSortKey,
} from "./api";
import { CommentComposer } from "./CommentComposer";
import {
  commentExcerpt,
  commentThreadView,
  COMMENT_SORTS,
  describeCommunityError,
  reportDraftView,
  TOMBSTONE_TEXT,
  type CommentRowView,
} from "./communityView";
import { CommentReactions, CommentVotes } from "./Reactions";

export interface CommentThreadProps {
  taskId: string;
  /** Server sort (patterns §4: the tab rides the URL; the page remounts per tab). */
  sort: CommentSortKey;
}

export function CommentThread({ taskId, sort }: CommentThreadProps) {
  const [items, setItems] = useState<CommentDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  // FOLD (T7 review): a failed load-more used to vanish silently; the
  // inline message + retry below keep the failure observable.
  const [moreError, setMoreError] = useState<unknown>(null);
  const [replyTo, setReplyTo] = useState<CommentRowView | null>(null);
  const [reportTarget, setReportTarget] = useState<CommentRowView | null>(null);
  const [reloadSeed, setReloadSeed] = useState(0);

  useEffect(() => {
    // Lint-driven state shape (the TaskDetailView note): the effect only
    // STARTS the fetch and applies results in async callbacks — phase
    // transitions into "loading" happen in state initializers or event
    // handlers, never synchronously in the effect body.
    let cancelled = false;
    listTaskComments(taskId, { sort, limit: COMMENT_PAGE_LIMIT, offset: 0 })
      .then(
        (page) => {
          if (!cancelled) {
            setItems(page.items);
            setTotal(page.total);
            setPhase("ready");
          }
        },
        (cause: unknown) => {
          if (!cancelled) {
            setError(cause);
            setPhase("error");
          }
        },
      );
    return () => {
      cancelled = true;
    };
  }, [taskId, sort, reloadSeed]);

  const retry = useCallback(() => {
    setPhase("loading");
    setReloadSeed((seed) => seed + 1);
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listTaskComments(taskId, {
        sort,
        limit: COMMENT_PAGE_LIMIT,
        offset: items.length,
      });
      // Offset pages can overlap under concurrent writes; merge by id
      // (the shared pinned helper — see lib/offsetPages).
      setItems(mergeOffsetPage(items, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      // Non-fatal (the loaded thread stays usable), but no longer SILENT:
      // the mapped inline message + retry render below the button (T8 fold).
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [taskId, sort, items, loadingMore]);

  const onPublished = useCallback((published: CommentDto) => {
    setItems((previous) => [published, ...previous]);
    setTotal((previous) => previous + 1);
    setReplyTo(null);
  }, []);

  const view = phase === "ready" ? commentThreadView(items) : null;

  return (
    <section className="section community-section" aria-label="任务评论">
      <div className="section-head">
        <h2 className="section-title">评论区</h2>
        <nav className="tab-bar" aria-label="评论排序">
          {COMMENT_SORTS.map((option) => (
            <Link
              key={option.key}
              className="tab-link"
              href={`/tasks/${encodeURIComponent(taskId)}?comments=${option.key}`}
              aria-current={option.key === sort ? "page" : undefined}
            >
              {option.label}
            </Link>
          ))}
        </nav>
      </div>

      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError error={error} onRetry={retry} retryLabel="重新加载评论" />
      ) : (
        <>
          {replyTo === null ? (
            <CommentComposer taskId={taskId} onPublished={onPublished} />
          ) : null}

          {view !== null && view.groups.length === 0 ? (
            <EmptyState
              title="还没有评论"
              hint="领取并完成任务后，来分享你的经验和 tips 吧"
            />
          ) : null}

          <div className="comment-list">
            {view?.groups.map((group) => (
              <div
                key={group.root.id}
                className={
                  group.replies.length > 0
                    ? "comment-thread"
                    : "comment-thread comment-thread-leaf"
                }
              >
                <CommentRowItem
                  taskId={taskId}
                  row={group.root}
                  depth={1}
                  orphanRoot={group.orphanRoot}
                  replyTo={replyTo}
                  onReply={setReplyTo}
                  onReport={setReportTarget}
                  onPublished={onPublished}
                />
                {group.replies.map((reply) => (
                  <CommentRowItem
                    key={reply.id}
                    taskId={taskId}
                    row={reply}
                    depth={2}
                    replyTo={replyTo}
                    onReply={setReplyTo}
                    onReport={setReportTarget}
                    onPublished={onPublished}
                  />
                ))}
              </div>
            ))}
          </div>

          {items.length < total ? (
            <div className="load-more">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                aria-busy={loadingMore}
              >
                {loadingMore ? (
                  <span className="spinner" aria-hidden="true" />
                ) : null}
                <span>
                  加载更多评论（{items.length}/{total}）
                </span>
              </button>
              {moreError !== null ? (
                <SectionError error={moreError} onRetry={() => void loadMore()} />
              ) : null}
            </div>
          ) : null}
        </>
      )}

      {reportTarget !== null ? (
        <ReportDialog comment={reportTarget} onClose={() => setReportTarget(null)} />
      ) : null}
    </section>
  );
}

// --- one comment row ----------------------------------------------------------------

interface CommentRowItemProps {
  taskId: string;
  row: CommentRowView;
  /** 1 = root level, 2 = the fixed reply level (§21.2 two-level visual). */
  depth: 1 | 2;
  orphanRoot?: boolean;
  replyTo: CommentRowView | null;
  onReply: (row: CommentRowView | null) => void;
  onReport: (row: CommentRowView) => void;
  onPublished: (comment: CommentDto) => void;
}

/**
 * One rendered comment. Content renders as a React text node — user
 * text is always plain text (spec §33.1), so an XSS payload in content
 * displays verbatim and never executes (no dangerouslySetInnerHTML
 * anywhere in this tree; `white-space: pre-wrap` keeps line breaks).
 * Tombstones show the uniform marker only — no actions, no text.
 */
function CommentRowItem({
  taskId,
  row,
  depth,
  orphanRoot,
  replyTo,
  onReply,
  onReport,
  onPublished,
}: CommentRowItemProps) {
  const isTombstone = row.deleted;
  const replying = replyTo !== null && replyTo.id === row.id;

  return (
    <article
      className={
        isTombstone
          ? `comment comment-depth-${depth} comment-tombstone`
          : `comment comment-depth-${depth}`
      }
    >
      <header className="comment-meta">
        <span className="comment-author">{row.authorDisplay}</span>
        <time dateTime={new Date(row.createdAtMs).toISOString()}>
          {formatDeadlineDateTime(row.createdAtMs)}
        </time>
        {row.edited ? <span className="comment-edited">已编辑</span> : null}
      </header>
      {isTombstone ? (
        <p className="comment-content comment-tombstone-text">{TOMBSTONE_TEXT}</p>
      ) : (
        <>
          <p className="comment-content">{row.content}</p>
          <footer className="comment-actions">
            <button
              type="button"
              className="btn btn-ghost comment-action"
              aria-expanded={replying}
              onClick={() => onReply(replying ? null : row)}
            >
              回复
            </button>
            <CommentVotes commentId={row.id} />
            <CommentReactions commentId={row.id} />
            <button
              type="button"
              className="btn btn-ghost comment-action"
              onClick={() => onReport(row)}
            >
              举报
            </button>
          </footer>
          {replying ? (
            <div className="comment-reply-composer">
              <CommentComposer
                taskId={taskId}
                parent={{
                  id: row.id,
                  authorDisplay: row.authorDisplay,
                  excerpt: commentExcerpt(row.content),
                }}
                onPublished={onPublished}
                onCancel={() => onReply(null)}
              />
            </div>
          ) : null}
        </>
      )}
      {orphanRoot ? (
        <p className="comment-orphan-hint">这条回复的上级评论未在当前加载范围内</p>
      ) : null}
    </article>
  );
}

// --- report dialog (spec §23: closed categories; filing removes nothing) ------------

/**
 * Report form over a NATIVE `<dialog>` (the T5 RedeemDialog pattern:
 * platform focus containment + Escape; no dialog primitive is
 * installed, so no bespoke trap). The submitted state is deliberately
 * NON-DESTRUCTIVE: the reported comment stays visible while moderation
 * reviews — the confirmation says so.
 */
function ReportDialog({
  comment,
  onClose,
}: {
  comment: CommentRowView;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [category, setCategory] = useState("");
  const [note, setNote] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    setSubmitError(null);
    const draft = reportDraftView(
      category,
      note,
      DEFAULT_REPORT_NOTE_MAX_LENGTH,
    );
    if (!draft.ok) {
      setFieldError(
        draft.reason === "category-required"
          ? "请选择举报类别"
          : draft.reason === "category-invalid"
            ? "举报类别无效"
            : `补充说明不能超过 ${draft.maxLength} 个字符`,
      );
      return;
    }
    setFieldError(null);
    setBusy(true);
    try {
      // Send ONLY the wire fields: `draft` is the validated view object
      // and carries its `ok: true` discriminator, which the backend's
      // extra="forbid" report body rejects with 422 (found by the
      // plan-10 e2e report flow — the first run against the real API).
      await reportComment(comment.id, {
        category: draft.category,
        note: draft.note,
      });
      setDone(true);
    } catch (error) {
      const view = describeCommunityError(error);
      setSubmitError(
        view.requestId !== null
          ? `${view.message}（请求 ID：${view.requestId}）`
          : view.message,
      );
    } finally {
      setBusy(false);
    }
  }

  function close() {
    const dialog = dialogRef.current;
    if (dialog !== null) {
      dialog.close();
    }
    onClose();
  }

  return (
    <dialog className="dialog" ref={dialogRef} onClose={close} aria-label="举报评论">
      {done ? (
        <div className="dialog-body">
          <h3 className="dialog-title">举报已提交</h3>
          <p className="report-confirmation" role="status">
            感谢你的反馈。该评论在审核期间保持可见，审核结果不会通知举报人。
          </p>
          <div className="dialog-actions">
            <button type="button" className="btn btn-primary" onClick={close}>
              完成
            </button>
          </div>
        </div>
      ) : (
        <form className="dialog-body" onSubmit={onSubmit}>
          <h3 className="dialog-title">举报评论</h3>
          <p className="report-target">
            举报 {comment.authorDisplay} 的评论：
            「{commentExcerpt(comment.content)}」
          </p>
          <fieldset className="field">
            <legend className="field-label">举报类别</legend>
            <div className="report-categories" role="radiogroup" aria-label="举报类别">
              {REPORT_CATEGORY_OPTIONS.map((option) => (
                <label key={option.value} className="identity-option">
                  <input
                    type="radio"
                    name="report-category"
                    value={option.value}
                    checked={category === option.value}
                    onChange={() => setCategory(option.value)}
                  />
                  <span>{option.label}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="field">
            <label className="field-label" htmlFor="report-note">
              补充说明（可选）
            </label>
            <textarea
              id="report-note"
              className="input"
              rows={3}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              aria-invalid={fieldError !== null}
              aria-describedby={fieldError !== null ? "report-note-error" : undefined}
            />
            {fieldError !== null ? (
              <p className="field-error" id="report-note-error" role="alert">
                {fieldError}
              </p>
            ) : (
              <p className="field-hint">举报会进入老师的审核队列，评论不会被立即隐藏。</p>
            )}
          </div>
          {submitError !== null ? (
            <div className="alert alert-error" role="alert">
              <p>
                <span className="alert-marker" aria-hidden="true">!</span>
                {submitError}
              </p>
            </div>
          ) : null}
          <div className="dialog-actions">
            <button type="button" className="btn btn-secondary" onClick={close}>
              取消
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={busy}
              aria-busy={busy}
            >
              {busy ? <span className="spinner" aria-hidden="true" /> : null}
              <span>提交举报</span>
            </button>
          </div>
        </form>
      )}
    </dialog>
  );
}
