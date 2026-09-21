"use client";
/**
 * Task rating (spec §20; brief step 4): a 1-5 star picker plus the
 * PUBLIC aggregate. Only completers may rate — eligibility is the
 * SERVER's verdict (patterns §3): the contract exposes no eligibility
 * read, so the picker stays operable and the typed 403
 * `RATING_NOT_ELIGIBLE` copy ("完成任务后才能评价该任务") is the
 * denial surface.
 *
 * Aggregate source: the task detail's `rating` summary ({average,
 * count} — §40 keeps rater identity out). This island reads it with
 * its own `getTask` call and REFETCHES after a successful rating (the
 * boundary-refetch pattern, §3) — one extra GET rather than
 * duplicating the detail-fetch machinery across components; merging
 * the two reads is a fold for when the section grows.
 *
 * Pessimistic submit (patterns §7): stars confirm from the 201/200
 * echo only; no optimistic state.
 */
import { useCallback, useEffect, useState } from "react";

import { getTask } from "@/features/tasks/api";
import { ratingText } from "@/features/tasks/display";

import { putTaskRating, type RatingResultDto } from "./api";
import {
  describeCommunityError,
  RATING_NOT_ELIGIBLE_COPY,
  RATING_STEPS,
} from "./communityView";

export interface TaskRatingProps {
  taskId: string;
}

export function TaskRating({ taskId }: TaskRatingProps) {
  const [aggregate, setAggregate] = useState<{ average: number; count: number } | null>(
    null,
  );
  const [aggregatePhase, setAggregatePhase] = useState<
    "loading" | "ready" | "error"
  >("loading");
  const [myRating, setMyRating] = useState<number | null>(null);
  const [busyValue, setBusyValue] = useState<number | null>(null);
  const [outcome, setOutcome] = useState<
    { tone: "ok" | "error"; message: string; requestId: string | null } | null
  >(null);

  const loadAggregate = useCallback(() => getTask(taskId), [taskId]);

  useEffect(() => {
    // Only STARTS the fetch; results land in async callbacks (the
    // lint-driven state shape — no synchronous setState in the body).
    let cancelled = false;
    loadAggregate().then(
      (detail) => {
        if (!cancelled) {
          setAggregate(detail.rating);
          setAggregatePhase("ready");
        }
      },
      () => {
        if (!cancelled) {
          setAggregatePhase("error");
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [loadAggregate]);

  async function rate(value: number) {
    if (busyValue !== null) {
      return;
    }
    setOutcome(null);
    setBusyValue(value);
    try {
      const result: RatingResultDto = await putTaskRating(taskId, value);
      setMyRating(result.rating);
      setOutcome({
        tone: "ok",
        message: `已提交评分：${result.rating} 星（可随时修改）`,
        requestId: null,
      });
      // Boundary refetch (patterns §3): the aggregate is server state.
      loadAggregate().then(
        (detail) => setAggregate(detail.rating),
        () => {},
      );
    } catch (error) {
      setOutcome({
        tone: "error",
        message: describeCommunityError(error).message,
        requestId: describeCommunityError(error).requestId,
      });
    } finally {
      setBusyValue(null);
    }
  }

  return (
    <section className="section task-rating" aria-label="任务评分">
      <div className="section-head">
        <h2 className="section-title">任务评分</h2>
        {aggregatePhase === "ready" ? (
          <span className="rating-aggregate">{ratingText(aggregate)}</span>
        ) : aggregatePhase === "loading" ? (
          <span className="rating-aggregate" aria-hidden="true">
            …
          </span>
        ) : null}
      </div>
      {aggregatePhase === "error" ? (
        <p className="field-hint">评分汇总暂时无法加载，仍可直接评分。</p>
      ) : null}
      <fieldset className="star-picker">
        <legend className="field-label">完成过这个任务？给它打个分</legend>
        <div className="star-options" role="radiogroup" aria-label="选择星级">
          {RATING_STEPS.map((value) => (
            <label key={value} className="star-option">
              <input
                type="radio"
                name="task-rating"
                value={value}
                checked={myRating === value}
                onChange={() => void rate(value)}
                disabled={busyValue !== null}
                aria-label={`${value} 星`}
              />
              <span
                className="star-glyph"
                data-active={myRating !== null && value <= myRating}
                aria-hidden="true"
              >
                ★
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      {outcome !== null ? (
        <p
          className={outcome.tone === "ok" ? "rating-outcome-ok" : "rating-outcome-error"}
          role={outcome.tone === "ok" ? "status" : "alert"}
        >
          {outcome.message}
          {outcome.requestId !== null ? `（请求 ID：${outcome.requestId}）` : ""}
        </p>
      ) : null}
      <p className="field-hint">
        仅完成过该任务的同学可评分；评分对他人只显示汇总结果。{RATING_NOT_ELIGIBLE_COPY}
        时会在这里提示。
      </p>
    </section>
  );
}
