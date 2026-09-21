"use client";
/**
 * Claim interaction (spec §8.3, §42; patterns §6/§7): ONE bodyless POST —
 * the SERVER allocates the random assignment, and only the response's
 * platform/keyword are ever shown. There is no selectable assignment list
 * and no assignment-id input anywhere in this flow (the request contract
 * cannot even carry one; §42).
 *
 * No optimistic update (patterns §7): the button stays busy until the
 * authoritative 201 arrives, then the after-claim panel renders the
 * assigned unit. Conflicts render the typed copy per envelope code and
 * leave the button enabled — a retry is always possible; the server is
 * the sole verdict. On success the owning view refetches server state at
 * this boundary (availability, my_claim; patterns §3).
 */
import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  countdownFrom,
  formatDeadlineDateTime,
  formatDeadlineSummary,
  parseServerInstant,
} from "@/lib/time";

import { claimTask, type ClaimDto } from "./api";
import { describeClaimError } from "./claimErrors";
import { claimRewardLine, claimStatusView } from "./display";
import { useNow } from "./useNow";

export interface ClaimButtonProps {
  taskId: string;
  /** The viewer's own non-terminal claim, when the detail already has one. */
  existingClaim: ClaimDto | null;
  /** Boundary hook: refetch owning server state after a successful claim. */
  onClaimed?: () => void;
}

export function ClaimButton({
  taskId,
  existingClaim,
  onClaimed,
}: ClaimButtonProps) {
  const router = useRouter();
  const [claim, setClaim] = useState<ClaimDto | null>(existingClaim ?? null);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState<{
    message: string;
    requestId: string | null;
  } | null>(null);
  const now = useNow(1_000);

  async function onClaim() {
    setBusy(true);
    setConflict(null);
    try {
      const allocated = await claimTask(taskId);
      setClaim(allocated);
      onClaimed?.();
      // Re-render any server components above this island with new state.
      router.refresh();
    } catch (error) {
      setConflict(describeClaimError(error));
    } finally {
      setBusy(false);
    }
  }

  if (claim !== null) {
    return <AssignedClaimPanel claim={claim} nowMs={now} />;
  }

  return (
    <div className="section">
      <button
        type="button"
        className="btn btn-primary btn-block"
        onClick={() => void onClaim()}
        disabled={busy}
        aria-busy={busy}
      >
        {busy ? <span className="spinner" aria-hidden="true" /> : null}
        <span>领取任务</span>
      </button>
      <p className="field-hint">
        领取后系统会随机分配一个任务单元，并展示分配给你的平台与关键词。
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
    </div>
  );
}

/**
 * After-allocation panel — the ONLY student surface that shows the
 * assigned platform/keyword (spec §42). Deadline text derives from the
 * server-snapshotted instants in the claim response; the ticking clock is
 * display-only (§42; the authoritative boundary check is the backend's).
 */
function AssignedClaimPanel({ claim, nowMs }: { claim: ClaimDto; nowMs: number }) {
  const status = claimStatusView(claim.status);
  const deadlineMs = parseServerInstant(claim.deadline_at);
  const graceMs = parseServerInstant(claim.grace_deadline_at);
  const parts = countdownFrom(deadlineMs, nowMs);
  const urgency = nowMs >= graceMs ? "closed" : parts.expired ? "closed" : parts.totalMs <= 4 * 60 * 60 * 1000 ? "near" : "none";

  return (
    <section className="claim-panel" role="status" aria-label="已分配的任务单元">
      <h2 className="claim-panel-title">已为你分配任务单元</h2>
      <div className="assignment-grid">
        <div className="assignment-item">
          <span className="assignment-label">平台</span>
          <span className="assignment-value">{claim.platform}</span>
        </div>
        <div className="assignment-item">
          <span className="assignment-label">关键词</span>
          <span className="assignment-value mono">{claim.keyword}</span>
        </div>
        <div className="assignment-item">
          <span className="assignment-label">状态</span>
          <span className="assignment-value">
            <span className={`badge badge-${status.tone}`}>{status.label}</span>
          </span>
        </div>
      </div>
      <p className="deadline-line" data-urgency={urgency} suppressHydrationWarning>
        截止 {formatDeadlineSummary(deadlineMs, nowMs)}
      </p>
      <p className="progress-note" suppressHydrationWarning>
        {claimRewardLine(claim.base_reward_points_snapshot, deadlineMs, graceMs, nowMs)}
      </p>
      <p className="progress-note" suppressHydrationWarning>
        超过截止时间后至 {formatDeadlineDateTime(graceMs)} 仍可提交，积分按实际提交时间结算。
      </p>
    </section>
  );
}
