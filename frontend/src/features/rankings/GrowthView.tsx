"use client";
/**
 * Growth island (spec §19/§42; design §9 Leaderboard-adjacent growth
 * summary): month points/rank, total earned, completed count, on-time
 * ratio, current streak, best historical monthly rank, and the owned
 * honors list.
 *
 * Display-honor selector: NOT implementable against this snapshot — no
 * endpoint selects the display honor and /growth/me carries no selected
 * field (see growthView.ts's documented gap). The honors list is
 * read-only until that API lands.
 */
import {
  EmptyState,
  SectionError,
  SectionHeading,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { myGrowth } from "@/features/rankings/api";
import { growthView } from "@/features/rankings/growthView";

export function GrowthView() {
  const { state, retry } = useSection(() => myGrowth());

  return (
    <section className="section" aria-label="我的成长">
      <SectionHeading title="我的成长" />
      {state.status === "loading" ? <SectionSkeleton lines={5} /> : null}
      {state.status === "error" ? (
        <SectionError error={state.error} onRetry={retry} />
      ) : null}
      {state.status === "ready" ? <GrowthBody data={state.data} /> : null}
    </section>
  );
}

function GrowthBody({ data }: { data: Parameters<typeof growthView>[0] }) {
  const view = growthView(data);
  return (
    <>
      <div className="panel">
        <div className="metric-row">
          <div className="metric">
            <span className="metric-label">本月积分</span>
            <span className="metric-value">{view.summary.monthPoints}</span>
          </div>
          <div className="metric">
            <span className="metric-label">本月排名</span>
            <span className="metric-value">{view.summary.monthRankText}</span>
          </div>
          <div className="metric">
            <span className="metric-label">累计获得</span>
            <span className="metric-value">{view.summary.totalEarnedPoints}</span>
          </div>
        </div>
        <div className="metric-row">
          <div className="metric">
            <span className="metric-label">完成任务</span>
            <span className="metric-value">{view.summary.completedCount}</span>
          </div>
          <div className="metric">
            <span className="metric-label">按时完成</span>
            <span className="metric-value">{view.summary.onTimeText}</span>
          </div>
          <div className="metric">
            <span className="metric-label">连续按时</span>
            <span className="metric-value">{view.summary.streakText}</span>
          </div>
        </div>
        <p className="progress-note">
          历史最好月榜名次：
          {view.summary.bestRankText === null ? "暂无" : view.summary.bestRankText}
        </p>
      </div>

      <section className="section" aria-label="获得的荣誉">
        <SectionHeading title="获得的荣誉" />
        {view.honors.length === 0 ? (
          <EmptyState
            title="还没有获得荣誉"
            hint="完成任务、保持按时提交或登上榜单即可获得荣誉"
          />
        ) : (
          <ul className="honor-list">
            {view.honors.map((honor) => (
              <li key={honor.honorId} className="honor-row">
                <span className="honor-name">{honor.name}</span>
                <span className="honor-meta">
                  <span className="honor-chip">{honor.typeLabel}</span>
                  {honor.period !== null ? <span>{honor.period}</span> : null}
                  <span>{honor.grantedLabel}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
