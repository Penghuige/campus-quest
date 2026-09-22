"use client";
/**
 * RedemptionsAdmin — the redemption review queue (spec §16.2; plan Task
 * 10 step 3): the pending queue (REQUESTED/UNDER_REVIEW, oldest first)
 * with approve / reject / fulfill decisions.
 *
 * Queue-shape honesty (the frozen contract): the listing endpoint
 * returns the PENDING set only — there is no APPROVED-but-unfulfilled
 * listing. Decisions therefore replace the row with its SERVER verdict
 * (the row stays visible, correctly badged, until the next refresh),
 * and the fulfill action is offered exactly on APPROVED rows — most
 * commonly the one just approved in this session.
 *
 * Reject blocks on a mandatory reason (the transport mirror); approve
 * and fulfill show explicit confirms (approve releases points from the
 * system; fulfill marks physical delivery complete).
 */
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { hasMorePages, mergeOffsetPage } from "@/lib/offsetPages";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import {
  approveRedemption,
  fulfillRedemption,
  listRedemptionQueue,
  rejectRedemption,
  type RedemptionReviewDto,
} from "./adminApi";
import {
  adminReasonReady,
  describeAdminMutationError,
  redemptionActions,
  redemptionStatusView,
} from "./adminView";

const PAGE_LIMIT = 20;

export function RedemptionsAdmin() {
  const [items, setItems] = useState<RedemptionReviewDto[]>([]);
  const [total, setTotal] = useState(0);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [reloadSeed, setReloadSeed] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<RedemptionReviewDto | null>(null);
  const [fulfilling, setFulfilling] = useState<RedemptionReviewDto | null>(null);
  const [approving, setApproving] = useState<RedemptionReviewDto | null>(null);

  useEffect(() => {
    let cancelled = false;
    listRedemptionQueue({ limit: PAGE_LIMIT }).then(
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
      const page = await listRedemptionQueue({ limit: PAGE_LIMIT, offset: items.length });
      setItems((previous) => mergeOffsetPage(previous, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [items.length, loadingMore]);

  /** Replace the decided row with its server verdict (patterns §3). */
  const onVerdict = useCallback((verdict: RedemptionReviewDto) => {
    setApproving(null);
    setRejecting(null);
    setFulfilling(null);
    setItems((previous) =>
      previous.map((row) => (row.id === verdict.id ? verdict : row)),
    );
  }, []);

  const selected = useMemo(
    () => items.find((row) => row.id === selectedId) ?? null,
    [items, selectedId],
  );

  return (
    <section className="section" aria-label="兑换审核队列">
      <div className="section-head">
        <h2 className="section-title">兑换审核队列</h2>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => {
            setPhase("loading");
            setReloadSeed((seed) => seed + 1);
          }}
        >
          刷新队列
        </button>
      </div>
      <p className="field-hint">
        队列按时间先后列出待审核的兑换申请；审核通过后即可标记发放完成。
        拒绝必须填写原因（将通过通知送达申请者）。
      </p>
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
        <EmptyState title="队列为空" hint="学生提交的兑换申请会进入这里等待审核" />
      ) : (
        <div className="review-layout">
          <div className="review-queue" aria-label="待处理兑换">
            {items.map((row) => (
              <QueueRow
                key={row.id}
                row={row}
                selected={row.id === selectedId}
                onSelect={() => setSelectedId(row.id)}
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
                  <span>加载更多（{items.length}/{total}）</span>
                </button>
                {moreError !== null ? (
                  <SectionError error={moreError} onRetry={() => void loadMore()} />
                ) : null}
              </div>
            ) : null}
          </div>
          {selected !== null ? (
            <RedemptionDetail
              key={selected.id}
              row={selected}
              onApprove={() => setApproving(selected)}
              onReject={() => setRejecting(selected)}
              onFulfill={() => setFulfilling(selected)}
            />
          ) : (
            <div className="review-detail review-detail-empty">
              <p className="empty-hint">从左侧选择一条兑换申请开始处理。</p>
            </div>
          )}
        </div>
      )}

      {approving !== null ? (
        <ApproveDialog row={approving} onDone={onVerdict} onCancel={() => setApproving(null)} />
      ) : null}
      {rejecting !== null ? (
        <RejectDialog row={rejecting} onDone={onVerdict} onCancel={() => setRejecting(null)} />
      ) : null}
      {fulfilling !== null ? (
        <FulfillDialog row={fulfilling} onDone={onVerdict} onCancel={() => setFulfilling(null)} />
      ) : null}
    </section>
  );
}

function QueueRow({
  row,
  selected,
  onSelect,
}: {
  row: RedemptionReviewDto;
  selected: boolean;
  onSelect: () => void;
}) {
  const status = redemptionStatusView(row.status);
  return (
    <button
      type="button"
      className="review-item"
      aria-pressed={selected}
      data-selected={selected ? "true" : "false"}
      onClick={onSelect}
    >
      <span className="review-item-head">
        <strong className="review-item-title">{row.item_name}</strong>
        <span className="meta-num review-tier">{row.points} 积分</span>
      </span>
      <span className="review-item-meta">
        <span className={`badge badge-${status.tone}`}>{status.label}</span>
        <span>申请人：{row.requester_nickname ?? "—"}</span>
        <time className="notif-time" dateTime={row.created_at}>
          {formatDeadlineDateTime(parseServerInstant(row.created_at))}
        </time>
      </span>
    </button>
  );
}

function RedemptionDetail({
  row,
  onApprove,
  onReject,
  onFulfill,
}: {
  row: RedemptionReviewDto;
  onApprove: () => void;
  onReject: () => void;
  onFulfill: () => void;
}) {
  const status = redemptionStatusView(row.status);
  const actions = redemptionActions(row.status);
  return (
    <div className="review-detail panel">
      <h3 className="section-title">兑换详情</h3>
      <div className="fact-rows">
        <div className="fact-row">
          <span className="fact-label">奖励</span>
          <span className="fact-value">{row.item_name}</span>
        </div>
        <div className="fact-row">
          <span className="fact-label">申请人</span>
          <span className="fact-value">{row.requester_nickname ?? "—"}</span>
        </div>
        <div className="fact-row">
          <span className="fact-label">消耗积分</span>
          <span className="fact-value meta-num">{row.points}</span>
        </div>
        <div className="fact-row">
          <span className="fact-label">学期快照</span>
          <span className="fact-value mono">{row.term_key}</span>
        </div>
        <div className="fact-row">
          <span className="fact-label">申请时间</span>
          <span className="fact-value">
            <time dateTime={row.created_at}>
              {formatDeadlineDateTime(parseServerInstant(row.created_at))}
            </time>
          </span>
        </div>
        <div className="fact-row">
          <span className="fact-label">兑换 ID</span>
          <span className="fact-value mono">{row.id}</span>
        </div>
        <div className="fact-row">
          <span className="fact-label">状态</span>
          <span className="fact-value">
            <span className={`badge badge-${status.tone}`}>{status.label}</span>
          </span>
        </div>
        {row.rejection_reason !== null ? (
          <div className="fact-row">
            <span className="fact-label">拒绝原因</span>
            <span className="fact-value">{row.rejection_reason}</span>
          </div>
        ) : null}
        {row.fulfilled_at != null ? (
          <div className="fact-row">
            <span className="fact-label">发放时间</span>
            <span className="fact-value">
              <time dateTime={row.fulfilled_at}>
                {formatDeadlineDateTime(parseServerInstant(row.fulfilled_at))}
              </time>
            </span>
          </div>
        ) : null}
        {row.fulfillment_note !== null ? (
          <div className="fact-row">
            <span className="fact-label">发放备注</span>
            <span className="fact-value">{row.fulfillment_note}</span>
          </div>
        ) : null}
      </div>
      {actions.length > 0 ? (
        <div className="review-actions">
          {actions.includes("approve") ? (
            <button type="button" className="btn btn-primary" onClick={onApprove}>
              通过并扣减积分
            </button>
          ) : null}
          {actions.includes("reject") ? (
            <button type="button" className="btn btn-danger" onClick={onReject}>
              拒绝申请
            </button>
          ) : null}
          {actions.includes("fulfill") ? (
            <button type="button" className="btn btn-primary" onClick={onFulfill}>
              标记已发放
            </button>
          ) : null}
        </div>
      ) : (
        <p className="field-hint">该兑换已处理完成。</p>
      )}
    </div>
  );
}

/** Approve: explicit confirm (points leave the system; no transport reason). */
function ApproveDialog({
  row,
  onDone,
  onCancel,
}: {
  row: RedemptionReviewDto;
  onDone: (verdict: RedemptionReviewDto) => void;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

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
      onDone(await approveRedemption(row.id));
    } catch (cause) {
      setError(cause);
      setBusy(false);
    }
  }

  const errorView =
    error !== null ? describeAdminMutationError(error, "操作失败，请稍后重试") : null;

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="redemption-approve-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <div className="dialog-body">
        <h3 id="redemption-approve-title" className="dialog-title">
          通过兑换申请
        </h3>
        <p className="report-target">
          确认后「{row.item_name}」（{row.points} 积分，申请人{" "}
          {row.requester_nickname ?? "—"}）将扣减积分并进入待发放状态；
          之后需要在详情中标记发放完成。
        </p>
        {errorView !== null ? (
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              {errorView.message}
            </p>
          </div>
        ) : null}
        <div className="dialog-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void onConfirm()}
            disabled={busy}
            aria-busy={busy}
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认通过</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </dialog>
  );
}

/** Reject: the reason is mandatory (spec §16.2; blank is not a reason). */
function RejectDialog({
  row,
  onDone,
  onCancel,
}: {
  row: RedemptionReviewDto;
  onDone: (verdict: RedemptionReviewDto) => void;
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
    if (!adminReasonReady(reason)) {
      setFieldError("拒绝原因必填（将通过通知送达申请者）");
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      onDone(await rejectRedemption(row.id, reason.trim()));
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const errorView =
    error !== null ? describeAdminMutationError(error, "操作失败，请稍后重试") : null;

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="redemption-reject-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="redemption-reject-title" className="dialog-title">
          拒绝兑换申请
        </h3>
        <p className="report-target">
          拒绝后「{row.item_name}」的占用将释放，积分退回申请者可用余额；
          拒绝原因会随通知送达申请者。
        </p>
        <div className="field">
          <label className="field-label" htmlFor="redemption-reject-reason">
            拒绝原因
          </label>
          <textarea
            id="redemption-reject-reason"
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
        {errorView !== null ? (
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              {errorView.message}
            </p>
          </div>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="btn btn-danger" disabled={busy} aria-busy={busy}>
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认拒绝</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}

/** Fulfill: marks the physical delivery complete; the note is optional (spec §16.2). */
function FulfillDialog({
  row,
  onDone,
  onCancel,
}: {
  row: RedemptionReviewDto;
  onDone: (verdict: RedemptionReviewDto) => void;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [note, setNote] = useState("");
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
    setBusy(true);
    setError(null);
    try {
      onDone(
        await fulfillRedemption(row.id, adminReasonReady(note) ? note.trim() : null),
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const errorView =
    error !== null ? describeAdminMutationError(error, "操作失败，请稍后重试") : null;

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="redemption-fulfill-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="redemption-fulfill-title" className="dialog-title">
          标记已发放
        </h3>
        <p className="report-target">
          确认「{row.item_name}」（申请人 {row.requester_nickname ?? "—"}）已完成发放；
          该操作将记录发放时间，不可撤销。
        </p>
        <div className="field">
          <label className="field-label" htmlFor="redemption-fulfill-note">
            发放备注（选填）
          </label>
          <textarea
            id="redemption-fulfill-note"
            className="input"
            rows={2}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            disabled={busy}
          />
        </div>
        {errorView !== null ? (
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              {errorView.message}
            </p>
          </div>
        ) : null}
        <div className="dialog-actions">
          <button type="submit" className="btn btn-primary" disabled={busy} aria-busy={busy}>
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>确认发放</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}
