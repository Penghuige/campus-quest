"use client";
/**
 * Claim detail island (patterns §8 "Claim and Submission detail"
 * archetype): task title + the ASSIGNED platform/keyword, deadline
 * countdown, reward status, the upload/version area with its validation
 * report, and the revision banner. Own-claims-only surface: the claim is
 * resolved from the owner-scoped `/me/claims` history — a claim id that
 * is not the viewer's own simply never appears (404-shaped empty state),
 * and a non-student account gets the 403-shaped explanation.
 *
 * SERVER-AUTHORITATIVE boundaries (spec §9.3; patterns §3/§14):
 * - the countdown is display-only; when it crosses zero the view
 *   REFETCHES the claim instead of locally setting EXPIRED;
 * - every status/reward figure renders from server fields verbatim.
 *
 * Contract note: `/me/claims` rows carry no revision deadline, teacher
 * note, or reward-lock projection yet — the revision banner shows the
 * §42/§11.3 copy the DTO can support; those fields render verbatim the
 * day the DTO grows them.
 */
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { EmptyState, SectionError } from "@/components/ui/sectionStates";
import { isApiError } from "@/lib/errors";
import {
  countdownFrom,
  formatDeadlineDateTime,
  formatDeadlineSummary,
  parseServerInstant,
} from "@/lib/time";
import { abandonClaim, listMyClaims, type MyClaimDto } from "@/features/tasks/api";
import {
  claimStatusView,
  isRevisionClaim,
  NEAR_CUTOFF_MS,
} from "@/features/tasks/display";
import { useNow } from "@/features/tasks/useNow";

import { describeSubmissionError } from "./submissionErrors";
import { ClaimSteps } from "./ClaimSteps";
import { RewardStatus } from "./RewardStatus";
import { UploadPanel } from "./UploadPanel";

/** History page size and walk bound for resolving one claim by id. */
const CLAIMS_PAGE_LIMIT = 50;
const MAX_CLAIM_PAGES = 10;

/** The server's own student-abandonable set (backend ABANDONABLE_STATUSES). */
function isAbandonable(status: string): boolean {
  return status === "CLAIMED" || status === "REVISION_REQUIRED";
}

/** The server's own submittable set (backend SUBMITTABLE_STATUSES). */
function isSubmittable(status: string): boolean {
  return status === "CLAIMED" || status === "REVISION_REQUIRED";
}

/** Resolve one own claim from the /me/claims history (bounded walk). */
async function findOwnClaim(claimId: string): Promise<MyClaimDto | null> {
  for (let page = 0; page < MAX_CLAIM_PAGES; page += 1) {
    const result = await listMyClaims({
      limit: CLAIMS_PAGE_LIMIT,
      offset: page * CLAIMS_PAGE_LIMIT,
    });
    const found = result.items.find((claim) => claim.claim_id === claimId);
    if (found !== undefined) {
      return found;
    }
    if ((page + 1) * CLAIMS_PAGE_LIMIT >= result.total) {
      return null;
    }
  }
  return null;
}

export function ClaimDetailView({ claimId }: { claimId: string }) {
  const [claim, setClaim] = useState<MyClaimDto | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "missing" | "denied" | "error">(
    "loading",
  );
  const [error, setError] = useState<unknown>(null);
  const [reloadAttempt, setReloadAttempt] = useState(0);
  const now = useNow(1_000);

  // The effect only STARTS the load (setState lands in async callbacks —
  // the lint-shaped pattern `useSection` documents); the loading phase is
  // the useState initializer on mount, and `retry` re-enters it from the
  // event handler.
  useEffect(() => {
    let cancelled = false;
    findOwnClaim(claimId).then(
      (found) => {
        if (cancelled) {
          return;
        }
        if (found !== null) {
          setClaim(found);
          setPhase("ready");
        } else {
          setPhase("missing");
        }
      },
      (cause: unknown) => {
        if (cancelled) {
          return;
        }
        setError(cause);
        setPhase(
          isApiError(cause) &&
            (cause.code === "PERMISSION_DENIED" || cause.code === "AUTHENTICATION_REQUIRED")
            ? "denied"
            : "error",
        );
      },
    );
    return () => {
      cancelled = true;
    };
  }, [claimId, reloadAttempt]);

  /** Silent boundary refetch (deadline cross, upload transitions, abandon). */
  const refetchClaim = useCallback(() => {
    findOwnClaim(claimId).then(
      (found) => {
        if (found !== null) {
          setClaim(found);
          setPhase("ready");
        }
      },
      () => {},
    );
  }, [claimId]);

  // Step 5: when the local countdown crosses the grace deadline, refetch
  // the authoritative claim — never locally set EXPIRED (spec §9.3).
  const graceExpiredRef = useRef(false);
  const graceDeadlineMs =
    claim !== null ? parseServerInstant(claim.grace_deadline_at) : null;
  useEffect(() => {
    if (graceDeadlineMs === null || phase !== "ready") {
      return;
    }
    const expired = countdownFrom(graceDeadlineMs, now).expired;
    if (expired && !graceExpiredRef.current) {
      graceExpiredRef.current = true;
      refetchClaim();
    }
  }, [now, graceDeadlineMs, phase, refetchClaim]);

  if (phase === "loading") {
    return (
      <div className="claim-detail">
        <span className="skeleton skeleton-line" style={{ width: "55%" }} />
        <span className="skeleton skeleton-line" data-width="narrow" />
        <span className="skeleton skeleton-block" />
      </div>
    );
  }

  if (phase === "denied") {
    return (
      <div className="claim-detail">
        <EmptyState
          title="仅学生账号可查看任务领取记录"
          hint="这个页面展示的是学生的任务领取与提交进度"
        >
          <Link className="link" href="/">
            返回首页
          </Link>
        </EmptyState>
      </div>
    );
  }

  if (phase === "missing") {
    return (
      <div className="claim-detail">
        <EmptyState
          title="未找到这条领取记录"
          hint="它可能不属于当前登录的账号，或链接有误"
        >
          <Link className="link" href="/">
            返回首页
          </Link>
        </EmptyState>
      </div>
    );
  }

  if (phase === "error" || claim === null) {
    return (
      <div className="claim-detail">
        <SectionError
          error={error}
          onRetry={() => {
            // The handler (not the effect) re-enters the loading phase.
            setPhase("loading");
            setReloadAttempt((value) => value + 1);
          }}
          retryLabel="重新加载"
        />
        <Link className="link back-link" href="/">
          返回首页
        </Link>
      </div>
    );
  }

  return (
    <ClaimDetailReady
      claim={claim}
      nowMs={now}
      onClaimChanged={refetchClaim}
    />
  );
}

function ClaimDetailReady({
  claim,
  nowMs,
  onClaimChanged,
}: {
  claim: MyClaimDto;
  nowMs: number;
  onClaimChanged: () => void;
}) {
  const status = claimStatusView(claim.status);
  const deadlineMs = parseServerInstant(claim.deadline_at);
  const graceMs = parseServerInstant(claim.grace_deadline_at);
  const parts = countdownFrom(deadlineMs, nowMs);
  const urgency = nowMs >= graceMs ? "closed" : parts.expired ? "closed" : parts.totalMs <= NEAR_CUTOFF_MS ? "near" : "none";
  const revision = isRevisionClaim(claim.status);

  return (
    <div className="claim-detail">
      <Link className="link back-link" href="/tasks">
        返回任务列表
      </Link>
      <div>
        <h1 className="task-detail-title">{claim.task_title}</h1>
        <div className="task-detail-badges">
          <span className={`badge badge-${status.tone}`}>{status.label}</span>
          {urgency === "near" && !revision ? (
            <span className="badge badge-warning">临近截止</span>
          ) : null}
          {urgency === "closed" && !revision ? (
            <span className="badge badge-danger">已截止</span>
          ) : null}
        </div>
        <ClaimSteps status={claim.status} />
      </div>

      <section className="claim-panel" aria-label="分配给你的任务单元">
        <h2 className="claim-panel-title">分配给你的任务单元</h2>
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
            <span className="assignment-label">领取时间</span>
            <span className="assignment-value">
              {formatDeadlineDateTime(parseServerInstant(claim.claimed_at))}
            </span>
          </div>
        </div>
        <p className="deadline-line" data-urgency={urgency} suppressHydrationWarning>
          截止 {formatDeadlineSummary(deadlineMs, nowMs)}
        </p>
        <p className="progress-note" suppressHydrationWarning>
          超过截止时间后至 {formatDeadlineDateTime(graceMs)} 仍可提交，积分按实际提交时间结算。
        </p>
      </section>

      {revision ? <RevisionBanner /> : null}

      <RewardStatus claim={claim} nowMs={nowMs} />

      {isSubmittable(claim.status) ? (
        <UploadPanel claimId={claim.claim_id} onClaimChanged={onClaimChanged} />
      ) : (
        <ClosedStateNote status={claim.status} />
      )}

      {isAbandonable(claim.status) ? (
        <AbandonControl claimId={claim.claim_id} onAbandoned={onClaimChanged} />
      ) : null}
    </div>
  );
}

/**
 * Revision banner (spec §11.3/§11.4): the teacher returned the claim;
 * the PROVISIONAL reward lock SURVIVES (server behavior) and a corrected
 * version uploads through the normal panel. The revision deadline and
 * teacher note are not part of the /me/claims contract yet.
 */
function RevisionBanner() {
  return (
    <div className="alert alert-warning" role="status">
      <p>
        <span className="alert-marker" aria-hidden="true">
          !
        </span>
        老师已退回修改，奖励档位已保留。
      </p>
      <p className="field-hint">请在修改期限内上传修正后的新版本，审核以最新版本为准。</p>
    </div>
  );
}

function ClosedStateNote({ status }: { status: string }) {
  if (status === "UNDER_REVIEW" || status === "VALIDATING") {
    return (
      <section className="section" aria-label="提交状态">
        <h2 className="section-title">提交状态</h2>
        <p className="progress-note">已提交，等待老师审核。审核结果会更新任务状态。</p>
      </section>
    );
  }
  if (status === "COMPLETED") {
    return (
      <section className="section" aria-label="提交状态">
        <h2 className="section-title">提交状态</h2>
        <p className="progress-note">任务已完成，积分已发放。</p>
      </section>
    );
  }
  return (
    <section className="section" aria-label="提交状态">
      <h2 className="section-title">提交状态</h2>
      <p className="progress-note">任务已结束，无法再提交。</p>
    </section>
  );
}

/**
 * Abandon action (spec §8.5): explicit two-step confirmation, and the
 * claim leaves the UI only when the SERVER confirms ABANDONED (patterns
 * §7 — no optimistic removal). ABANDON_LIMIT_REACHED renders typed copy
 * and the claim stays exactly as it was.
 */
function AbandonControl({
  claimId,
  onAbandoned,
}: {
  claimId: string;
  onAbandoned: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId: string | null } | null>(
    null,
  );

  const onConfirm = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await abandonClaim(claimId);
      setConfirming(false);
      // Authoritative confirmation arrived — refetch the claim state.
      onAbandoned();
    } catch (error) {
      setFailure(describeSubmissionError(error));
    } finally {
      setBusy(false);
    }
  }, [claimId, onAbandoned]);

  return (
    <section className="section abandon-block" aria-label="放弃任务">
      {confirming ? (
        // Inline two-step confirm (patterns §10: short focused
        // confirmation; no modal layer is installed, and the buttons
        // themselves carry the semantics).
        <div className="alert alert-warning">
          <p>确认放弃这个任务？放弃会释放任务单元，今天放弃次数有限。</p>
          <p>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void onConfirm()}
              disabled={busy}
              aria-busy={busy}
            >
              {busy ? <span className="spinner" aria-hidden="true" /> : null}
              确认放弃
            </button>{" "}
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              取消
            </button>
          </p>
        </div>
      ) : (
        <p>
          <button type="button" className="btn btn-secondary" onClick={() => setConfirming(true)}>
            放弃任务
          </button>
        </p>
      )}
      {failure !== null ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">
              !
            </span>
            {failure.message}
          </p>
          {failure.requestId !== null ? (
            <p className="req-id">请求 ID：{failure.requestId}</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
