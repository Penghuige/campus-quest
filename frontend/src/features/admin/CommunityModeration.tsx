"use client";
/**
 * Teacher community moderation for one task (spec §21.4/§23; brief step 4).
 *
 * PRIVACY BY CONSTRUCTION (the binding constraint): every rendered
 * comment row goes through `moderationRowView` — the single choke point
 * whose shape carries ONLY display fields. An anonymous row renders
 * 匿名用户 plus the PSEUDONYMOUS moderation key; no student number,
 * phone, email, or login identifier has any field to land in (identity
 * reveal is the separate, Admin-only audited surface — not this page).
 *
 * The moderation delete is reason-mandatory (spec §21.4): the dialog
 * blocks on a blank reason exactly like the transport does. Reporter
 * nickname/id ride the report rows — the §23 moderator surface — and go
 * no further than this section.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { hasMorePages, mergeOffsetPage } from "@/lib/offsetPages";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import {
  listModerationComments,
  listTaskReports,
  moderateDeleteComment,
  type ModerationCommentDto,
  type ModerationReportDto,
} from "./teacherApi";
import {
  moderationRowView,
  reportStatusView,
  type ModerationRowView,
} from "./teacherView";

const PAGE_LIMIT = 20;

export function CommunityModeration({ taskId }: { taskId: string }) {
  return (
    <section className="section" aria-label="社区与举报">
      <div className="section-head">
        <h3 className="section-title">社区与举报</h3>
      </div>
      <div className="workbench-columns">
        <ModerationComments taskId={taskId} />
        <ReportQueue taskId={taskId} />
      </div>
    </section>
  );
}

// --- moderation comment listing -------------------------------------------------------

function ModerationComments({ taskId }: { taskId: string }) {
  const [items, setItems] = useState<ModerationCommentDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);
  const [deleting, setDeleting] = useState<ModerationRowView | null>(null);

  useEffect(() => {
    let cancelled = false;
    listModerationComments(taskId, { limit: PAGE_LIMIT }).then(
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
  }, [taskId, reloadSeed]);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listModerationComments(taskId, {
        limit: PAGE_LIMIT,
        offset: items.length,
      });
      setItems((previous) => mergeOffsetPage(previous, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [taskId, items.length, loadingMore]);

  /** After a moderation delete, refetch page one (the row's flags change server-side). */
  const onDeleted = useCallback(() => {
    setDeleting(null);
    setPhase("loading");
    setReloadSeed((seed) => seed + 1);
  }, []);

  return (
    <div className="panel moderation-panel">
      <h4 className="section-title">评论管理</h4>
      <p className="field-hint">
        匿名评论仅显示化名标识（匿名用户 + 追溯键）；本页面不提供任何身份信息。
      </p>
      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError
          error={error}
          onRetry={() => {
            setPhase("loading");
            setReloadSeed((seed) => seed + 1);
          }}
          retryLabel="重新加载"
        />
      ) : items.length === 0 ? (
        <EmptyState title="暂无评论" hint="该任务下还没有评论" />
      ) : (
        <>
          <ul className="moderation-list">
            {items.map((comment) => {
              const row = moderationRowView(comment, parseServerInstant);
              return (
                <ModerationRow
                  key={row.id}
                  row={row}
                  onDelete={() => setDeleting(row)}
                />
              );
            })}
          </ul>
          {hasMorePages(items.length, total) ? (
            <div className="load-more">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                aria-busy={loadingMore}
              >
                {loadingMore ? <span className="spinner" aria-hidden="true" /> : null}
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
      {deleting !== null ? (
        <ModerationDeleteDialog row={deleting} onDone={onDeleted} onCancel={() => setDeleting(null)} />
      ) : null}
    </div>
  );
}

function ModerationRow({
  row,
  onDelete,
}: {
  row: ModerationRowView;
  onDelete: () => void;
}) {
  return (
    <li className="moderation-item" data-deleted={row.deleted ? "true" : "false"}>
      <div className="comment-meta">
        <span className="comment-author">{row.authorDisplay}</span>
        {row.isAnonymous ? <span className="badge badge-info">匿名</span> : null}
        {row.deleted ? <span className="badge badge-warning">已删除</span> : null}
        {row.hardHidden ? <span className="badge badge-danger">已隐藏</span> : null}
        {row.edited ? <span className="comment-edited">已编辑</span> : null}
        <time className="notif-time" dateTime={new Date(row.createdAtMs).toISOString()}>
          {formatDeadlineDateTime(row.createdAtMs)}
        </time>
      </div>
      {row.content !== null ? (
        <p className="comment-content">{row.content}</p>
      ) : (
        <p className="comment-content">该评论已删除</p>
      )}
      <div className="comment-actions">
        {row.moderationKey !== null ? (
          <span className="mono moderation-key" title="匿名追溯键（非身份信息）">
            {row.moderationKey}
          </span>
        ) : null}
        {!row.deleted ? (
          <button type="button" className="btn btn-ghost comment-action" onClick={onDelete}>
            删除评论
          </button>
        ) : null}
      </div>
    </li>
  );
}

/** Reason-mandatory moderation delete (spec §21.4; audited server-side). */
function ModerationDeleteDialog({
  row,
  onDone,
  onCancel,
}: {
  row: ModerationRowView;
  onDone: () => void;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (reason.trim().length === 0) {
      setFieldError("删除原因必填（将记入审计日志）");
      return;
    }
    setFieldError(null);
    setBusy(true);
    try {
      await moderateDeleteComment(row.id, reason.trim());
      onDone();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="moderation-delete-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="moderation-delete-title" className="dialog-title">
          删除评论
        </h3>
        <p className="report-target">
          {row.authorDisplay} 的评论将以删除状态保留（学生端显示为已删除）；删除原因必填并记入审计日志。
        </p>
        <div className="field">
          <label className="field-label" htmlFor="moderation-reason">
            删除原因
          </label>
          <textarea
            id="moderation-reason"
            className="input"
            rows={3}
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              if (fieldError !== null) {
                setFieldError(null);
              }
            }}
            aria-invalid={fieldError !== null}
            disabled={busy}
            required
          />
          {fieldError !== null ? <p className="field-error">{fieldError}</p> : null}
        </div>
        {error !== null ? <SectionError error={error} /> : null}
        <div className="dialog-actions">
          <button type="submit" className="btn btn-danger" disabled={busy} aria-busy={busy}>
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认删除</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}

// --- report queue ----------------------------------------------------------------------

function ReportQueue({ taskId }: { taskId: string }) {
  const [items, setItems] = useState<ModerationReportDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);

  useEffect(() => {
    let cancelled = false;
    listTaskReports(taskId, { limit: PAGE_LIMIT }).then(
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
  }, [taskId, reloadSeed]);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listTaskReports(taskId, {
        limit: PAGE_LIMIT,
        offset: items.length,
      });
      setItems((previous) => mergeOffsetPage(previous, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [taskId, items.length, loadingMore]);

  return (
    <div className="panel moderation-panel">
      <h4 className="section-title">举报队列</h4>
      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError
          error={error}
          onRetry={() => {
            setPhase("loading");
            setReloadSeed((seed) => seed + 1);
          }}
          retryLabel="重新加载"
        />
      ) : items.length === 0 ? (
        <EmptyState title="暂无举报" hint="学生提交的举报会出现在这里" />
      ) : (
        <>
          <ul className="moderation-list">
            {items.map((report) => (
              <ReportRow key={report.id} report={report} />
            ))}
          </ul>
          {hasMorePages(items.length, total) ? (
            <div className="load-more">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                aria-busy={loadingMore}
              >
                {loadingMore ? <span className="spinner" aria-hidden="true" /> : null}
                <span>
                  加载更多举报（{items.length}/{total}）
                </span>
              </button>
              {moreError !== null ? (
                <SectionError error={moreError} onRetry={() => void loadMore()} />
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

function ReportRow({ report }: { report: ModerationReportDto }) {
  const status = reportStatusView(report.status);
  const comment = moderationRowView(report.comment, parseServerInstant);
  return (
    <li className="moderation-item">
      <div className="comment-meta">
        <span className={`badge badge-${status.tone}`}>{status.label}</span>
        <span className="comment-type">{report.category}</span>
        <time className="notif-time" dateTime={new Date(comment.createdAtMs).toISOString()}>
          {formatDeadlineDateTime(comment.createdAtMs)}
        </time>
      </div>
      <p className="comment-content">
        {comment.content !== null ? comment.content : "该评论已删除"}
      </p>
      <p className="report-meta">
        举报人：{report.reporter_nickname ?? "—"}
        {report.note !== null ? ` · 备注：${report.note}` : ""}
      </p>    </li>
  );
}
