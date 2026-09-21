"use client";
/**
 * Student dashboard island (spec §42; patterns §8 Student Dashboard
 * archetype): work requiring attention first (active + revision claims),
 * then points and nearest-reward progress beside the monthly rank
 * snapshot, then task discovery — deliberately NOT a wall of equal KPI
 * cards (design-system §8).
 *
 * Sections fetch INDEPENDENTLY and each renders its own §10 triad
 * (loading skeleton / empty / error + retry), so one failing endpoint
 * never blanks the page (patterns §5: parallel fetching, no waterfalls).
 * All figures are server state; nothing here recomputes a business rule.
 */
import Link from "next/link";

import {
  EmptyState,
  SectionCardsSkeleton,
  SectionError,
  SectionHeading,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import {
  listRewards,
  myWallet,
  type RewardItemDto,
  type WalletDto,
} from "@/features/points/api";
import { monthlyBoard, type BoardDto } from "@/features/rankings/api";
import { formatDeadlineSummary, parseServerInstant } from "@/lib/time";
import { listMyClaims, listTasks, type MyClaimDto } from "@/features/tasks/api";
import { claimStatusView } from "@/features/tasks/display";
import { TaskCard } from "@/features/tasks/TaskCard";
import { useNow } from "@/features/tasks/useNow";

import {
  claimsSummaryView,
  pointsProgressView,
  rankSnapshotView,
} from "./sections";

/** Own-claims page size for the summary. V1 caps ACTIVE claims at 3
 * (spec §8.2), so the first page of 10 always covers every open claim. */
const CLAIMS_PAGE_LIMIT = 10;
const TASKS_PREVIEW_LIMIT = 3;

export function DashboardView() {
  const now = useNow(60_000);

  return (
    <>
      <ClaimsSection now={now} />
      <div className="dashboard-columns">
        <PointsProgressSection />
        <RankSection />
      </div>
      <TasksPreviewSection now={now} />
    </>
  );
}

// --- 进行中 / 待修改 claims ------------------------------------------------------

function ClaimsSection({ now }: { now: number }) {
  const { state, retry } = useSection(() =>
    listMyClaims({ limit: CLAIMS_PAGE_LIMIT }),
  );

  return (
    <section className="section" aria-label="我的任务">
      <SectionHeading title="我的任务" />
      {state.status === "loading" ? <SectionSkeleton lines={3} /> : null}
      {state.status === "error" ? (
        <SectionError error={state.error} onRetry={retry} />
      ) : null}
      {state.status === "ready" ? (
        <ClaimsBody claims={state.data.items} now={now} />
      ) : null}
    </section>
  );
}

function ClaimsBody({ claims, now }: { claims: MyClaimDto[]; now: number }) {
  const { active, revision } = claimsSummaryView(claims);
  if (active.length === 0 && revision.length === 0) {
    return (
      <EmptyState
        title="暂无进行中的任务"
        hint="去任务广场看看，领取第一个任务开始攒积分"
      >
        <Link className="link" href="/tasks">
          浏览任务
        </Link>
      </EmptyState>
    );
  }
  return (
    <div className="claim-rows">
      {revision.map((claim) => (
        <ClaimRow key={claim.claim_id} claim={claim} now={now} revision />
      ))}
      {active.map((claim) => (
        <ClaimRow key={claim.claim_id} claim={claim} now={now} />
      ))}
    </div>
  );
}

function ClaimRow({
  claim,
  now,
  revision = false,
}: {
  claim: MyClaimDto;
  now: number;
  revision?: boolean;
}) {
  const status = claimStatusView(claim.status);
  return (
    // The row links into the claim's own detail page (T4 submission
    // surface); the layout stays a row, so the whole row is the target.
    <Link className="claim-row-link" href={`/claims/${claim.claim_id}`}>
      <div className="claim-row">
        <div className="claim-row-top">
          <span className="claim-title">{claim.task_title}</span>
          <span className={`badge badge-${status.tone}`}>{status.label}</span>
        </div>
        {revision ? (
          // Design §14 preferred copy; the revision deadline is not part of
          // the /me/claims DTO yet.
          <p className="claim-deadline">老师已退回修改，奖励档位已保留</p>
        ) : (
          <p className="claim-deadline" suppressHydrationWarning>
            截止 {formatDeadlineSummary(parseServerInstant(claim.deadline_at), now)}
          </p>
        )}
      </div>
    </Link>
  );
}

// --- points + nearest-reward progress -------------------------------------------

function PointsProgressSection() {
  const wallet = useSection(() => myWallet());
  const rewards = useSection(() => listRewards());

  return (
    <section className="section" aria-label="积分与奖励">
      <SectionHeading title="积分与奖励" />
      {wallet.state.status === "error" ? (
        <SectionError error={wallet.state.error} onRetry={wallet.retry} />
      ) : null}
      {rewards.state.status === "error" ? (
        <SectionError error={rewards.state.error} onRetry={rewards.retry} />
      ) : null}
      {wallet.state.status === "loading" ? <SectionSkeleton lines={3} /> : null}
      {wallet.state.status === "ready" ? (
        <WalletBody
          wallet={wallet.state.data}
          rewards={
            rewards.state.status === "ready"
              ? { status: "ready", items: rewards.state.data.items }
              : { status: rewards.state.status, items: [] }
          }
        />
      ) : null}
    </section>
  );
}

/**
 * The rewards shelf may still be loading (or failed) while the wallet is
 * ready. The empty copy ("暂无可兑换的奖励") is a SERVER-VERDICTED fact —
 * no purchasable item exists — so it renders ONLY on the ready shelf;
 * pending renders a skeleton line and a failed load a muted note (the
 * section error + retry already rendered above).
 */
function WalletBody({
  wallet,
  rewards,
}: {
  wallet: WalletDto;
  rewards: { status: "loading" | "error" | "ready"; items: RewardItemDto[] };
}) {
  const view = pointsProgressView(wallet, rewards.items);
  return (
    <div className="panel">
      <div className="metric-row">
        <div className="metric">
          <span className="metric-label">可用积分</span>
          <span className="metric-value">{view.availablePoints}</span>
        </div>
        <div className="metric">
          <span className="metric-label">累计获得</span>
          <span className="metric-value">{view.earnedPoints}</span>
        </div>
      </div>
      {rewards.status === "loading" ? (
        <span className="skeleton skeleton-line" data-width="narrow" aria-label="正在加载可兑换奖励" />
      ) : null}
      {rewards.status === "error" ? (
        <p className="progress-note">暂时无法获取可兑换奖励，请稍后重试</p>
      ) : null}
      {rewards.status === "ready" &&
      view.status === "ready" &&
      view.rewardName !== null &&
      view.rewardCost !== null ? (
        <div className="progress">
          <div
            className="progress-track"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={view.rewardCost}
            aria-valuenow={Math.min(view.availablePoints, view.rewardCost)}
            aria-label={`距离兑换「${view.rewardName}」的进度`}
          >
            <div
              className="progress-fill"
              style={{ width: `${Math.round(view.ratio * 100)}%` }}
            />
          </div>
          <p className="progress-note">
            {view.remainingPoints === null
              ? `「${view.rewardName}」（${view.rewardCost} 积分）现在就可以兑换`
              : `距兑换「${view.rewardName}」还差 ${view.remainingPoints} 积分`}
          </p>
        </div>
      ) : null}
      {rewards.status === "ready" && view.status === "empty" ? (
        <p className="progress-note">暂无可兑换的奖励，完成任务先攒积分吧</p>
      ) : null}
      {view.frozenPoints > 0 ? (
        <p className="progress-note">
          有 {view.frozenPoints} 积分冻结在兑换申请中，兑换以可花费余额为准
        </p>
      ) : null}
    </div>
  );
}

// --- monthly rank snapshot --------------------------------------------------------

function RankSection() {
  const { state, retry } = useSection(() => monthlyBoard(5));

  return (
    <section className="section" aria-label="本月排名">
      <SectionHeading title="本月排名" />
      {state.status === "loading" ? <SectionSkeleton lines={3} /> : null}
      {state.status === "error" ? (
        <SectionError error={state.error} onRetry={retry} />
      ) : null}
      {state.status === "ready" ? <RankBody board={state.data} /> : null}
    </section>
  );
}

function RankBody({ board }: { board: BoardDto }) {
  const view = rankSnapshotView(board);
  if (view.status === "empty") {
    return (
      <div className="panel">
        <EmptyState
          title="本月暂无排名"
          hint="完成任务获得积分后即可登上月榜"
        />
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="metric-row">
        <div className="metric">
          <span className="metric-label">我的月榜名次</span>
          <span className="metric-value">
            第 {view.rank} 名
            <span className="metric-unit"> · {view.score} 积分</span>
          </span>
        </div>
      </div>
      {view.top.length > 0 ? (
        <div className="claim-rows">
          {view.top.map((entry) => (
            <div key={entry.rank} className="claim-row-top">
              <span className="claim-deadline">
                #{entry.rank} {entry.nickname}
                {entry.displayHonor !== null ? ` · ${entry.displayHonor}` : ""}
              </span>
              <span className="claim-deadline meta-num">{entry.score}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// --- task discovery preview --------------------------------------------------------

function TasksPreviewSection({ now }: { now: number }) {
  const { state, retry } = useSection(() =>
    listTasks({ limit: TASKS_PREVIEW_LIMIT }),
  );

  return (
    <section className="section" aria-label="最新任务">
      <SectionHeading
        title="最新任务"
        action={
          <Link className="link section-link" href="/tasks">
            查看全部
          </Link>
        }
      />
      {state.status === "loading" ? <SectionCardsSkeleton cards={3} /> : null}
      {state.status === "error" ? (
        <SectionError error={state.error} onRetry={retry} />
      ) : null}
      {state.status === "ready" ? (
        state.data.items.length === 0 ? (
          <EmptyState
            title="暂无可领取的任务"
            hint="老师还没有发布可领取的任务，稍后再来看看"
          />
        ) : (
          <ul className="task-grid">
            {state.data.items.map((card) => (
              <li key={card.id}>
                <TaskCard card={card} nowMs={now} />
              </li>
            ))}
          </ul>
        )
      ) : null}
    </section>
  );
}
