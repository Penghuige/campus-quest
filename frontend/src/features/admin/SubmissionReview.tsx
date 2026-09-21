"use client";
/**
 * SubmissionReview — the teacher review surface (spec §11.3, §12.4 UI
 * list, §14; design §8 Teacher review archetype; patterns §8
 * master/detail on wide, stacked on narrow).
 *
 * Sanctioned context (spec §40/§42): the queue rows carry the claim's
 * platform/keyword, the locked tier, the validation report and the
 * download path — exactly the reviewer's judging context. The DTO
 * carries NO student identity (not even a user id), and this component
 * adds none.
 *
 * Decisions are never optimistic (patterns §7): each action waits for
 * the server verdict, renders its outcome (points granted / revision
 * deadline / lock cancelled), then drops the decided row — the queue is
 * VALIDATED-but-undecided by construction, so the row cannot stay.
 * Approve shows an explicit confirm (a grant leaves the system);
 * revision-required blocks on a mandatory note; invalidate-reward-lock
 * blocks on a mandatory reason and leads with the prominent warning.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { hasMorePages, mergeOffsetPage } from "@/lib/offsetPages";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { ValidationReport } from "@/features/submissions/ValidationReport";
import type { ValidationReportDto } from "@/features/submissions/api";
import { claimStatusView } from "@/features/tasks/display";

import {
  approveSubmission,
  invalidateRewardLock,
  listReviewQueue,
  mintSubmissionDownload,
  requireRevision,
  type ApproveResultDto,
  type ReviewQueueItemDto,
  type RevisionResultDto,
} from "./teacherApi";
import {
  fileFactsText,
  INVALIDATE_WARNING_TEXT,
  lockedTierText,
  reviewStatusView,
  reviewTextReady,
  versionText,
} from "./teacherView";

const PAGE_SIZE = 20;

type Outcome =
  | { kind: "approved"; submissionId: string; result: ApproveResultDto }
  | { kind: "revision"; submissionId: string; result: RevisionResultDto }
  | { kind: "invalidated"; submissionId: string; result: RevisionResultDto };

export function SubmissionReview() {
  const [items, setItems] = useState<ReviewQueueItemDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  useEffect(() => {
    let cancelled = false;
    listReviewQueue({ limit: PAGE_SIZE, offset: 0 }).then(
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
  }, [reloadSeed]);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listReviewQueue({ limit: PAGE_SIZE, offset: items.length });
      setItems((previous) =>
        mergeOffsetPage(previous, page.items, (row) => row.submission_id),
      );
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [items.length, loadingMore]);

  /** Apply a decision: the row leaves the queue; the verdict renders above. */
  const onDecided = useCallback(
    (decision: Outcome) => {
      const remaining = items.filter(
        (item) => item.submission_id !== decision.submissionId,
      );
      setItems(remaining);
      setTotal((previous) => Math.max(previous - 1, 0));
      if (selectedId === decision.submissionId) {
        setSelectedId(remaining[0]?.submission_id ?? null);
      }
      setOutcome(decision);
    },
    [items, selectedId],
  );

  const selected = useMemo(
    () => items.find((item) => item.submission_id === selectedId) ?? null,
    [items, selectedId],
  );
  /** Submission history versions: same claim, other queue rows (loaded page). */
  const versions = useMemo(
    () =>
      selected === null
        ? []
        : items.filter(
            (item) =>
              item.claim_id === selected.claim_id &&
              item.submission_id !== selected.submission_id,
          ),
    [items, selected],
  );

  return (
    <>
      {outcome !== null ? <OutcomeAlert outcome={outcome} onDismiss={() => setOutcome(null)} /> : null}
      {phase === "loading" ? (
        <SectionSkeleton lines={6} />
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
        <EmptyState
          title="审核队列为空"
          hint="学生提交通过机器校验后会进入这里等待人工审核"
        />
      ) : (
        <div className="review-layout">
          <div className="review-queue" aria-label="待审核提交">
            {items.map((item) => (
              <QueueRow
                key={item.submission_id}
                item={item}
                selected={item.submission_id === selectedId}
                onSelect={() => setSelectedId(item.submission_id)}
              />
            ))}
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
                    加载更多（{items.length}/{total}）
                  </span>
                </button>
                {moreError !== null ? (
                  <SectionError error={moreError} onRetry={() => void loadMore()} />
                ) : null}
              </div>
            ) : null}
          </div>
          {selected !== null ? (
            <ReviewDetail
              key={selected.submission_id}
              item={selected}
              versions={versions}
              onDecided={onDecided}
            />
          ) : (
            <div className="review-detail review-detail-empty">
              <p className="empty-hint">从左侧选择一条提交开始审核。</p>
            </div>
          )}
        </div>
      )}
    </>
  );
}

// --- one queue row (§12.4 UI list) ------------------------------------------------------

function QueueRow({
  item,
  selected,
  onSelect,
}: {
  item: ReviewQueueItemDto;
  selected: boolean;
  onSelect: () => void;
}) {
  const claim = claimStatusView(item.claim_status);
  const review = reviewStatusView(item.review_status);
  const validation = validationSummary(item.validation);
  return (
    <button
      type="button"
      className="review-item"
      aria-pressed={selected}
      data-selected={selected ? "true" : "false"}
      onClick={onSelect}
    >
      <span className="review-item-head">
        <strong className="review-item-title">{item.task_title}</strong>
        <span className="mono review-pair">
          {item.platform} / {item.keyword}
        </span>
      </span>
      <span className="review-item-meta">
        <span className={`badge badge-${claim.tone === "muted" ? "info" : claim.tone}`}>
          {claim.label}
        </span>
        <span className={`badge badge-${review.tone}`}>{review.label}</span>
        <span className="badge badge-info">{versionText(item.version)}</span>
        <span className={`badge badge-${validation.tone}`}>{validation.label}</span>
        <span className="meta-num review-tier">{lockedTierText(item)}</span>
        <time className="notif-time" dateTime={item.submitted_at}>
          {formatDeadlineDateTime(parseServerInstant(item.submitted_at))}
        </time>
      </span>
    </button>
  );
}

function validationSummary(
  report: ValidationReportDto | null,
): { label: string; tone: "success" | "warning" | "danger" } {
  if (report === null) {
    return { label: "校验报告生成中", tone: "warning" };
  }
  // Queue rows are VALIDATED (machine check passed); the report may still
  // carry warnings, and an error list renders honestly if contract drift
  // ever lands one here.
  if (report.errors.length > 0) {
    return { label: `校验存在 ${report.errors.length} 项问题`, tone: "danger" };
  }
  if (report.warnings.length > 0) {
    return { label: `校验通过（${report.warnings.length} 项提示）`, tone: "warning" };
  }
  return { label: "校验通过", tone: "success" };
}

// --- the decision detail ------------------------------------------------------------------

function ReviewDetail({
  item,
  versions,
  onDecided,
}: {
  item: ReviewQueueItemDto;
  versions: ReviewQueueItemDto[];
  onDecided: (decision: Outcome) => void;
}) {
  const [dialog, setDialog] = useState<"approve" | "revision" | "invalidate" | null>(
    null,
  );

  const onDownload = useCallback(async () => {
    // Mint a short-lived presigned GET on click (spec §33.3): a URL
    // embedded in the page would expire under the reviewer.
    const grant = await mintSubmissionDownload(item.submission_id);
    window.open(grant.url, "_blank", "noopener");
  }, [item.submission_id]);

  return (
    <div className="review-detail">
      <div className="section-head">
        <h2 className="section-title">审核提交</h2>
        <button type="button" className="btn btn-ghost" onClick={() => void onDownload()}>
          下载文件
        </button>
      </div>

      <dl className="fact-rows">
        <div className="fact-row">
          <dt className="fact-label">任务</dt>
          <dd className="fact-value">{item.task_title}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">平台 / 关键词</dt>
          <dd className="fact-value mono">
            {item.platform} / {item.keyword}
          </dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">提交版本</dt>
          <dd className="fact-value">{versionText(item.version)}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">锁定奖励</dt>
          <dd className="fact-value meta-num">{lockedTierText(item)}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">文件</dt>
          <dd className="fact-value mono">{fileFactsText(item)}</dd>
        </div>
        <div className="fact-row">
          <dt className="fact-label">提交时间</dt>
          <dd className="fact-value">
            {formatDeadlineDateTime(parseServerInstant(item.submitted_at))}
          </dd>
        </div>
      </dl>

      {item.validation !== null ? (
        <ValidationReport report={item.validation} />
      ) : (
        <p className="field-hint">校验报告尚未生成。</p>
      )}

      {versions.length > 0 ? (
        <section className="section" aria-label="历史版本">
          <div className="section-head">
            <h3 className="section-title">该领取的历史版本</h3>
          </div>
          <ul className="moderation-list">
            {versions.map((version) => (
              <li key={version.submission_id} className="moderation-item">
                <div className="comment-meta">
                  <span className="badge badge-info">{versionText(version.version)}</span>
                  <span className="mono review-pair">
                    {version.platform} / {version.keyword}
                  </span>
                  <time className="notif-time" dateTime={version.submitted_at}>
                    {formatDeadlineDateTime(parseServerInstant(version.submitted_at))}
                  </time>
                </div>
                <p className="field-hint">{fileFactsText(version)}</p>
              </li>
            ))}
          </ul>
          <p className="field-hint">历史版本同样待审核时会作为独立条目出现在队列中。</p>
        </section>
      ) : null}

      <div className="dialog-actions review-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => setDialog("approve")}
        >
          通过并发放奖励
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => setDialog("revision")}
        >
          退回修改
        </button>
        <button
          type="button"
          className="btn btn-danger"
          onClick={() => setDialog("invalidate")}
        >
          判无效
        </button>
      </div>

      {dialog === "approve" ? (
        <ApproveDialog
          item={item}
          onClose={() => setDialog(null)}
          onDone={(result) => {
            setDialog(null);
            onDecided({ kind: "approved", submissionId: item.submission_id, result });
          }}
        />
      ) : null}
      {dialog === "revision" ? (
        <RevisionDialog
          mode="revision"
          item={item}
          onClose={() => setDialog(null)}
          onDone={(result) => {
            setDialog(null);
            onDecided({ kind: "revision", submissionId: item.submission_id, result });
          }}
        />
      ) : null}
      {dialog === "invalidate" ? (
        <RevisionDialog
          mode="invalidate"
          item={item}
          onClose={() => setDialog(null)}
          onDone={(result) => {
            setDialog(null);
            onDecided({ kind: "invalidated", submissionId: item.submission_id, result });
          }}
        />
      ) : null}
    </div>
  );
}

/** Approve confirm: states the grant + the completed claim (spec §14). */
function ApproveDialog({
  item,
  onClose,
  onDone,
}: {
  item: ReviewQueueItemDto;
  onClose: () => void;
  onDone: (result: ApproveResultDto) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  async function onConfirm() {
    if (busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await approveSubmission(item.submission_id);
      onDone(result);
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
      aria-labelledby="approve-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onClose();
        }
      }}
      onClick={(event) => {
        if (event.target === dialogRef.current && !busy) {
          onClose();
        }
      }}
    >
      <div className="dialog-body">
        <h3 id="approve-title" className="dialog-title">
          通过该提交
        </h3>
        <p className="field-hint">
          确认后该领取将标记为已完成
          {item.locked_reward_points !== null
            ? `，并向学生发放锁定的 ${item.locked_reward_points} 积分`
            : "，奖励按服务端结算发放"}
          ；此操作会立即生效并记入积分流水。
        </p>
        {error !== null ? <SectionError error={error} /> : null}
        <div className="dialog-actions">
          <button type="button" className="btn btn-primary" onClick={() => void onConfirm()} disabled={busy} aria-busy={busy}>
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认通过</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </dialog>
  );
}

/**
 * 退回修改 / 判无效 dialog: the note/reason is MANDATORY (the transport
 * rule, spec §11.3). 判无效 leads with the prominent warning — the
 * cancelled lock is the consequence the reviewer must read first.
 */
function RevisionDialog({
  mode,
  item,
  onClose,
  onDone,
}: {
  mode: "revision" | "invalidate";
  item: ReviewQueueItemDto;
  onClose: () => void;
  onDone: (result: RevisionResultDto) => void;
}) {
  const [text, setText] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  const invalidate = mode === "invalidate";
  const dialogTitle = invalidate ? "判无效（取消奖励锁定）" : "退回修改";
  const label = invalidate ? "判无效原因（必填，将记入审计）" : "退回说明（必填，学生会看到）";
  const placeholder = invalidate
    ? "例如：提交内容与分配的平台/关键词不符…"
    : "例如：缺少 keyword 列，请补充后重新提交…";

  async function onSubmit() {
    if (busy) {
      return;
    }
    if (!reviewTextReady(text)) {
      setFieldError(invalidate ? "判无效原因不能为空" : "退回说明不能为空");
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      const result = invalidate
        ? await invalidateRewardLock(item.submission_id, text.trim())
        : await requireRevision(item.submission_id, text.trim());
      onDone(result);
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
      aria-labelledby={invalidate ? "invalidate-title" : "revision-title"}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onClose();
        }
      }}
    >
      <div className="dialog-body">
        <h3
          id={invalidate ? "invalidate-title" : "revision-title"}
          className="dialog-title"
        >
          {dialogTitle}
        </h3>
        {invalidate ? (
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              {INVALIDATE_WARNING_TEXT}
            </p>
          </div>
        ) : (
          <p className="field-hint">
            退回后学生需在修订时限内重新提交；当前锁定的奖励档位会保留。
          </p>
        )}
        <div className="field">
          <label className="field-label" htmlFor={invalidate ? "invalidate-reason" : "revision-note"}>
            {label}
          </label>
          <textarea
            id={invalidate ? "invalidate-reason" : "revision-note"}
            className="input"
            rows={3}
            maxLength={2000}
            placeholder={placeholder}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              if (fieldError !== null) {
                setFieldError(null);
              }
            }}
            aria-invalid={fieldError !== null}
            aria-describedby={fieldError !== null ? "revision-field-error" : undefined}
            disabled={busy}
            required
          />
          {fieldError !== null ? (
            <p className="field-error" id="revision-field-error">
              {fieldError}
            </p>
          ) : null}
        </div>
        {error !== null ? <SectionError error={error} /> : null}
        <div className="dialog-actions">
          <button
            type="button"
            className={`btn ${invalidate ? "btn-danger" : "btn-primary"}`}
            onClick={() => void onSubmit()}
            disabled={busy}
            aria-busy={busy}
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>{invalidate ? "确认判无效" : "确认退回"}</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </dialog>
  );
}

// --- decision outcome -----------------------------------------------------------------------

function OutcomeAlert({
  outcome,
  onDismiss,
}: {
  outcome: Outcome;
  onDismiss: () => void;
}) {
  const revisionDeadline =
    outcome.kind === "approved"
      ? null
      : formatDeadlineDateTime(parseServerInstant(outcome.result.revision_deadline_at));
  return (
    <div
      className={`alert ${outcome.kind === "approved" ? "alert-success" : outcome.kind === "revision" ? "alert-warning" : "alert-error"}`}
      role="status"
    >
      {outcome.kind === "approved" ? (
        <p>
          已通过该提交；领取状态 {outcome.result.claim_status}
          {outcome.result.points_granted !== null
            ? `，已发放 ${outcome.result.points_granted} 积分`
            : ""}
          {outcome.result.already_reviewed ? "（该领取此前已审核，未重复发放）" : ""}。
        </p>
      ) : outcome.kind === "revision" ? (
        <p>
          已退回修改；学生需在 {revisionDeadline} 前重新提交，奖励档位已保留。
        </p>
      ) : (
        <p>
          已判无效；奖励锁定已取消（{outcome.result.reward_lock_status}），学生需在{" "}
          {revisionDeadline} 前重新提交并重新核算。
        </p>
      )}
      <p>
        <button type="button" className="btn btn-ghost" onClick={onDismiss}>
          知道了
        </button>
      </p>
    </div>
  );
}
