"use client";
/**
 * Redemption confirm dialog (spec §16.1; patterns §6/§7/§10).
 *
 * A short focused confirmation over a native `<dialog>`: showModal()
 * gives the browser's own focus containment, Escape handling, and
 * inert-background semantics without a bespoke modal (patterns §19's
 * ban is on hand-rolled traps WHEN a primitive exists; no dialog
 * primitive is installed, and the native element is the platform's).
 *
 * Mutation rules (patterns §7): NO optimistic anything — the confirm
 * button stays disabled until the server answers; success renders the
 * REQUESTED (pending-review) state from the RESPONSE; every typed
 * 409/422 gate failure renders code-keyed copy and re-arms the confirm
 * so another attempt is possible (with a FRESH Idempotency-Key — see
 * redeemView.newIdempotencyKey).
 */
import { useEffect, useRef, useState, type MouseEvent } from "react";

import { redeemReward, type RedemptionDto, type RewardItemDto } from "@/features/points/api";
import {
  describeRedeemError,
  newIdempotencyKey,
  redemptionStatusView,
} from "@/features/rewards/redeemView";

export interface RedeemDialogProps {
  /** The item being confirmed (the card that opened the dialog). */
  reward: RewardItemDto;
  /** The wallet's spendable figure at open time (display only — the server re-decides). */
  spendablePoints: number | null;
  open: boolean;
  onClose: () => void;
  /**
   * Boundary hook: fires with the server's redemption on success so the
   * page refetches wallet + shelf (patterns §3: refetch at boundaries).
   */
  onRedeemed: (redemption: RedemptionDto, item: RewardItemDto) => void;
}

export function RedeemDialog({
  reward,
  spendablePoints,
  open,
  onClose,
  onRedeemed,
}: RedeemDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [redemption, setRedemption] = useState<RedemptionDto | null>(null);

  // Controlled open/close over the native dialog: the parent flips
  // `open`; every fresh open resets the attempt state.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    if (open && !dialog.open) {
      setSubmitting(false);
      setError(null);
      setRedemption(null);
      dialog.showModal();
    }
    if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  // Escape/cancel: keep React the source of truth instead of letting
  // the native close race the controlled state.
  function onCancel(event: React.SyntheticEvent<HTMLDialogElement>) {
    event.preventDefault();
    onClose();
  }

  // Backdrop click closes (the dialog element itself is the backdrop).
  function onBackdropClick(event: MouseEvent<HTMLDialogElement>) {
    if (event.target === dialogRef.current) {
      onClose();
    }
  }

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const result = await redeemReward(reward.id, newIdempotencyKey());
      setRedemption(result);
      onRedeemed(result, reward);
    } catch (cause) {
      setError(cause);
    } finally {
      setSubmitting(false);
    }
  }

  const conflict = error === null ? null : describeRedeemError(error);
  const done = redemption !== null;
  const status = done ? redemptionStatusView(redemption.status) : null;

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="redeem-dialog-title"
      onCancel={onCancel}
      onClick={onBackdropClick}
    >
      <div className="dialog-body">
        <h2 id="redeem-dialog-title" className="dialog-title">
          {done ? "兑换申请已提交" : "确认兑换"}
        </h2>

        {!done ? (
          <>
            <dl className="fact-rows">
              <div className="fact-row">
                <dt className="fact-label">奖励</dt>
                <dd className="fact-value">{reward.name}</dd>
              </div>
              <div className="fact-row">
                <dt className="fact-label">消耗积分</dt>
                <dd className="fact-value">{reward.point_cost}</dd>
              </div>
              <div className="fact-row">
                <dt className="fact-label">当前可花费</dt>
                <dd className="fact-value">
                  {spendablePoints === null ? "—" : spendablePoints}
                </dd>
              </div>
            </dl>
            <p className="field-hint">
              确认后积分将冻结并等待老师审核；审核通过前这部分积分不能再用于其他兑换，未通过会全额退回。
            </p>
            {conflict !== null ? (
              <div className="alert alert-error" role="alert">
                <p>
                  <span className="alert-marker" aria-hidden="true">
                    !
                  </span>
                  {conflict.message}
                </p>
                {conflict.requestId !== null ? (
                  <p className="req-id">请求 ID：{conflict.requestId}</p>
                ) : null}
              </div>
            ) : null}
            <div className="dialog-actions">
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void submit()}
                disabled={submitting}
                aria-busy={submitting}
                autoFocus
              >
                {submitting ? <span className="spinner" aria-hidden="true" /> : null}
                <span>确认兑换</span>
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={onClose}
                disabled={submitting}
              >
                取消
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="alert alert-success" role="status">
              <p>
                「{reward.name}」兑换申请已提交，{redemption.points} 积分已冻结。
              </p>
              <p className="field-hint">
                老师审核通过后即可领取；进度以「最近兑换」和积分余额为准。
              </p>
            </div>
            {status !== null ? (
              <p className="redeem-status-line">
                当前状态：
                <span className={`badge badge-${status.tone}`}>{status.label}</span>
              </p>
            ) : null}
            <div className="dialog-actions">
              <button type="button" className="btn btn-primary" onClick={onClose} autoFocus>
                完成
              </button>
            </div>
          </>
        )}
      </div>
    </dialog>
  );
}
