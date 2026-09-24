"use client";
/**
 * Task detail island (patterns §8 "Task detail" archetype): title + rarity
 * + rating, description, reward/deadline/availability facts, then the
 * Claim action. Assignment payloads are hidden before claim BY
 * CONSTRUCTION — the detail DTO carries only an availability COUNT
 * (spec §42); the assigned platform/keyword first appear in
 * ClaimButton's after-allocation panel.
 *
 * State-shape note (lint-driven): the fetcher is setState-free; the mount
 * effect only starts it and applies results in async callbacks, so
 * effects never cascade renders synchronously.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { SectionError } from "@/components/ui/sectionStates";
import { isApiError } from "@/lib/errors";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import { getTask, type TaskDetailDto } from "./api";
import { ClaimButton } from "./ClaimButton";
import { availabilityText, deadlineView, ratingText, rarityView } from "./display";
import { useNow } from "./useNow";

export function TaskDetailView({ taskId }: { taskId: string }) {
  const [detail, setDetail] = useState<TaskDetailDto | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const now = useNow(30_000);

  const fetchDetail = useCallback(() => getTask(taskId), [taskId]);

  const applyDetail = useCallback((loaded: TaskDetailDto) => {
    setDetail(loaded);
    setPhase("ready");
  }, []);

  const applyError = useCallback((cause: unknown) => {
    setError(cause);
    setPhase("error");
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchDetail().then(
      (loaded) => {
        if (!cancelled) {
          applyDetail(loaded);
        }
      },
      (cause: unknown) => {
        if (!cancelled) {
          applyError(cause);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [fetchDetail, applyDetail, applyError]);

  const retry = useCallback(() => {
    setPhase("loading");
    fetchDetail().then(applyDetail, applyError);
  }, [fetchDetail, applyDetail, applyError]);

  // Boundary refetch after a successful claim (patterns §3): refresh the
  // server's availability/my_claim without re-entering the loading phase —
  // the after-claim panel is already authoritative from the 201 response.
  const refreshAfterClaim = useCallback(() => {
    fetchDetail().then(applyDetail, () => {});
  }, [fetchDetail, applyDetail]);

  if (phase === "loading") {
    return (
      <div className="task-detail">
        <span className="skeleton skeleton-line" style={{ width: "60%" }} />
        <span className="skeleton skeleton-line" data-width="narrow" />
        <span className="skeleton skeleton-block" />
      </div>
    );
  }

  if (phase === "error" || detail === null) {
    if (isApiError(error) && error.code === "NOT_FOUND") {
      return (
        <div className="task-detail">
          <div className="empty-state">
            <p className="empty-title">任务不存在或已下架</p>
            <p className="empty-hint">它可能已被老师关闭或归档</p>
            <Link className="link" href="/tasks">
              返回任务列表
            </Link>
          </div>
        </div>
      );
    }
    return (
      <div className="task-detail">
        <SectionError error={error} onRetry={retry} retryLabel="重新加载" />
        <Link className="link back-link" href="/tasks">
          返回任务列表
        </Link>
      </div>
    );
  }

  const rarity = rarityView(detail.rarity);
  const deadline = deadlineView(detail, now);
  const fixedDeadline =
    detail.fixed_deadline_at === null
      ? null
      : formatDeadlineDateTime(parseServerInstant(detail.fixed_deadline_at));

  return (
    <div className="task-detail">
      <Link className="link back-link" href="/tasks">
        返回任务列表
      </Link>
      <div>
        <h1 className="task-detail-title">{detail.title}</h1>
        <div className="task-detail-badges">
          <span className="rarity-badge" data-rarity={rarity.rarity}>
            {rarity.label}
          </span>
          <span className="badge" suppressHydrationWarning>
            {ratingText(detail.rating)}
          </span>
          {deadline.urgency === "near" ? (
            <span className="badge badge-warning">临近截止</span>
          ) : null}
          {deadline.urgency === "closed" ? (
            <span className="badge badge-danger">已截止</span>
          ) : null}
        </div>
      </div>
      {/* Plan 11 mid-pass review P1: the decision information comes
          BEFORE the claim action (patterns §8 archetype). Source order
          is the narrow reading order — 说明 → 事实 → CTA; the wide grid
          places the decision column beside the description. */}
      <div className="task-detail-body">
        <section className="section" aria-label="任务说明">
          <h2 className="section-title">任务说明</h2>
          <p className="task-description">{detail.description}</p>
        </section>
        <div className="task-decision">
          <dl className="task-facts">
            <div className="fact-row">
              <dt className="fact-label">基础奖励</dt>
              <dd className="fact-value">{detail.base_reward_points} 积分</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">截止方式</dt>
              <dd className="fact-value">{deadline.modeLabel}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">
                {fixedDeadline !== null ? "截止时间" : "提交时限"}
              </dt>
              <dd className="fact-value">
                <span
                  className="deadline-line"
                  data-urgency={deadline.urgency}
                  suppressHydrationWarning
                >
                  {deadline.line}
                </span>
              </dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">可领取</dt>
              <dd className="fact-value">{availabilityText(detail.assignments_available)}</dd>
            </div>
          </dl>
          <ClaimButton
            taskId={detail.id}
            existingClaim={detail.my_claim}
            onClaimed={refreshAfterClaim}
          />
        </div>
      </div>
    </div>
  );
}
