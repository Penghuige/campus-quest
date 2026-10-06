"use client";
/**
 * Redemption confirm dialog (spec §16.1; patterns §6/§7/§10).
 *
 * A short focused confirmation on the plan-14 Dialog primitive
 * (components/ui/dialog): Radix supplies role=dialog, aria-modal, the
 * focus trap, Escape, and outside-click close; the visual shell is the
 * `.cq-dialog*` replication of the legacy `.dialog` values.
 *
 * Mutation rules (patterns §7): NO optimistic anything — the confirm
 * button stays disabled until the server answers; success renders the
 * REQUESTED (pending-review) state from the RESPONSE; every typed
 * 409/422 gate failure renders code-keyed copy and re-arms the confirm
 * so another attempt is possible (with a FRESH Idempotency-Key — see
 * redeemView.newIdempotencyKey).
 */
import { useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
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
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          onClose();
        }
      }}
    >
      <DialogContent aria-labelledby="redeem-dialog-title">
        {/* The primitive unmounts the content on close, so the body's
            attempt state starts fresh on every open — the legacy reset-
            on-open effect's job, without an effect. */}
        <RedeemDialogBody
          reward={reward}
          spendablePoints={spendablePoints}
          onClose={onClose}
          onRedeemed={onRedeemed}
        />
      </DialogContent>
    </Dialog>
  );
}

function RedeemDialogBody({
  reward,
  spendablePoints,
  onClose,
  onRedeemed,
}: Omit<RedeemDialogProps, "open">) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [redemption, setRedemption] = useState<RedemptionDto | null>(null);

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
    <>
      <DialogTitle id="redeem-dialog-title">
        {done ? "兑换申请已提交" : "确认兑换"}
      </DialogTitle>

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
            <DialogFooter>
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
            </DialogFooter>
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
            <DialogFooter>
              <button type="button" className="btn btn-primary" onClick={onClose} autoFocus>
                完成
              </button>
            </DialogFooter>
          </>
        )}
    </>
  );
}
