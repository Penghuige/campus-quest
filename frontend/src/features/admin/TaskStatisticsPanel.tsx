"use client";
/**
 * Task statistics panel (spec §41; design §8 Teacher): the counts-only
 * workbench aggregate. The rating block renders the REAL
 * `RatingSummaryPort` values the backend adapter now wires (average +
 * count, or 暂无评分 while count is zero) — the frontend never derives a
 * rating figure itself (patterns §3).
 *
 * `submission_counts` is the backend's documented future seam (empty
 * until the submission axes are projected in) — the section renders only
 * when the server sends rows, so the panel stays honest under both
 * contract states.
 */
import { useEffect, useState } from "react";

import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { ratingText } from "@/features/tasks/display";

import { getTaskStatistics, type TaskStatisticsDto } from "./teacherApi";

export interface TaskStatisticsPanelProps {
  taskId: string;
  /** Bump to refetch (e.g. after a confirmed assignment import). */
  reloadSeed: number;
}

export function TaskStatisticsPanel({ taskId, reloadSeed }: TaskStatisticsPanelProps) {
  const [refetchSeed, setRefetchSeed] = useState(0);
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ready"; data: TaskStatisticsDto } | { kind: "error"; error: unknown }
  >({ kind: "loading" });

  useEffect(() => {
    // Lint-driven state shape (the codebase's island rule): the effect
    // only STARTS the fetch; refetches keep the previous data visible
    // until the answer lands (stale-while-revalidate), and `retry()` —
    // an event handler — is what re-enters the loading state.
    let cancelled = false;
    getTaskStatistics(taskId).then(
      (data) => {
        if (!cancelled) {
          setState({ kind: "ready", data });
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setState({ kind: "error", error });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [taskId, reloadSeed, refetchSeed]);

  return (
    <section className="section" aria-label="任务统计">
      <div className="section-head">
        <h3 className="section-title">任务统计</h3>
      </div>
      {state.kind === "loading" ? (
        <SectionSkeleton lines={3} />
      ) : state.kind === "error" ? (
        <SectionError
          error={state.error}
          onRetry={() => {
            setState({ kind: "loading" });
            setRefetchSeed((seed) => seed + 1);
          }}
          retryLabel="重新加载统计"
        />
      ) : (
        <>
          <div className="metric-row">
            <Metric label="可领取单元" value={state.data.assignments_available} />
            <Metric label="进行中领取" value={state.data.active_claims} />
            <Metric label="占用中单元" value={state.data.assignments_occupied} />
            <Metric label="已完成单元" value={state.data.assignments_completed} />
            <Metric
              label="完成率"
              value={`${Math.round(state.data.completion_rate * 100)}%`}
            />
          </div>
          <dl className="fact-rows">
            <div className="fact-row">
              <dt className="fact-label">学生评分</dt>
              <dd className="fact-value">{ratingText(state.data.rating)}</dd>
            </div>
            <div className="fact-row">
              <dt className="fact-label">已退役单元</dt>
              <dd className="fact-value">{state.data.assignments_retired}</dd>
            </div>
            {Object.keys(state.data.submission_counts).length > 0 ? (
              <div className="fact-row">
                <dt className="fact-label">提交统计</dt>
                <dd className="fact-value">
                  {Object.entries(state.data.submission_counts)
                    .map(([axis, count]) => `${axis} ${count}`)
                    .join("、")}
                </dd>
              </div>
            ) : null}
          </dl>
        </>
      )}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className="metric-value">{value}</span>
    </div>
  );
}
