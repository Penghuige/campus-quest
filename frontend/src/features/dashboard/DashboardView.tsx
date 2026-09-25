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
import { useSection, type SectionState } from "@/components/ui/useSection";
import { listRewards, myWallet, type WalletDto } from "@/features/points/api";
import { monthlyBoard, type BoardDto } from "@/features/rankings/api";
import { listNotifications } from "@/features/notifications/api";
import { formatDeadlineSummary, parseServerInstant } from "@/lib/time";
import {
  listMyClaims,
  listTasks,
  type MyClaimDto,
  type MyClaimsPageDto,
  type TaskCardDto,
} from "@/features/tasks/api";
import { claimStatusView, claimStepView, currentStepLabel } from "@/features/tasks/display";
import { TaskCard } from "@/features/tasks/TaskCard";
import { useNow } from "@/features/tasks/useNow";

import {
  claimsSummaryView,
  nextActionView,
  rankSnapshotView,
  walletShelfView,
  withoutClaimedTasks,
  type NextActionView,
  type RewardsShelf,
} from "./sections";

/** Own-claims page size for the summary. V1 caps ACTIVE claims at 3
 * (spec §8.2), so the first page of 10 always covers every open claim. */
const CLAIMS_PAGE_LIMIT = 10;
const TASKS_PREVIEW_LIMIT = 3;
const NOTIFICATIONS_PREVIEW_LIMIT = 3;

export function DashboardView() {
  const now = useNow(60_000);
  // Claims feed BOTH the hero and the discovery dedup, so the fetch
  // lives here (one request, one triad) instead of inside a section.
  const claims = useSection(() => listMyClaims({ limit: CLAIMS_PAGE_LIMIT }));

  return (
    <>
      <HeroSection state={claims.state} retry={claims.retry} now={now} />
      {/* Signature motif 3 (achievement): ONE asymmetric story on the
          page ground — the points/next-reward rail dominates (2fr),
          the rank snapshot rides compact (1fr). No equal card duel. */}
      <div className="dash-band">
        <PointsProgressSection />
        <RankSection />
      </div>
      <ClaimsSection state={claims.state} retry={claims.retry} now={now} />
      <TasksPreviewSection
        now={now}
        claims={claims.state.status === "ready" ? claims.state.data.items : []}
      />
      <NotificationsPreviewSection />
    </>
  );
}

// --- hero: the one "当前最重要" next action ---------------------------------------

function HeroSection({
  state,
  retry,
  now,
}: {
  state: SectionState<MyClaimsPageDto>;
  retry: () => void;
  now: number;
}) {
  if (state.status === "loading") {
    return (
      <section className="hero hero-loading" aria-label="当前最重要" aria-busy="true">
        <span className="skeleton skeleton-line" style={{ width: "30%" }} />
        <span className="skeleton skeleton-line" style={{ width: "55%" }} />
        <span className="skeleton skeleton-line" data-width="narrow" />
      </section>
    );
  }
  if (state.status === "error") {
    return (
      <section className="hero" aria-label="当前最重要">
        <SectionError error={state.error} onRetry={retry} />
      </section>
    );
  }
  const next = nextActionView(state.data.items);
  if (next === null) {
    return (
      <section className="hero" aria-label="当前最重要">
        <p className="hero-eyebrow">开始今天的学习</p>
        <h2 className="hero-title">领取第一个任务，开始攒积分</h2>
        <p className="hero-line">完成任务获得积分，兑换你想要的奖励</p>
        <Link className="btn btn-primary hero-cta" href="/tasks">
          浏览任务
        </Link>
      </section>
    );
  }
  return <HeroAction next={next} now={now} />;
}

function HeroAction({ next, now }: { next: NextActionView; now: number }) {
  // Signature motif 1 (quest/progress): the hero carries the claim's
  // step position as a compact segment rail — the same node+segment
  // grammar as the claim page's strip, miniaturized. The visible
  // current-step text is the accessible name (no aria-label on a
  // generic <p> — AT ignores it there).
  const steps = claimStepView(next.status);
  return (
    <section className="hero" aria-label="当前最重要">
      <p className="hero-eyebrow">
        {next.kind === "revision" ? "老师退回修改，奖励档位已保留" : "进行中 · 最早截止"}
      </p>
      <h2 className="hero-title">{next.taskTitle}</h2>
      <div className="hero-row">
        {next.kind === "active" && next.deadlineAt !== null ? (
          <p className="hero-line" suppressHydrationWarning>
            截止 {formatDeadlineSummary(parseServerInstant(next.deadlineAt), now)}
          </p>
        ) : null}
        {steps.linear ? (
          <p className="hero-line">
            <span className="mini-rail" aria-hidden="true">
              {steps.steps.map((step) => (
                <span key={step.label} className="mini-rail-seg" data-state={step.state} />
              ))}
            </span>
            {currentStepLabel(steps)}
          </p>
        ) : null}
      </div>
      <Link
        className="btn btn-primary hero-cta"
        href={`/claims/${next.claimId}`}
      >
        {next.kind === "revision" ? "去修改提交" : "继续提交"}
      </Link>
    </section>
  );
}

// --- 进行中 / 待修改 claims（hero 之后剩余的行）----------------------------------

function ClaimsSection({
  state,
  retry,
  now,
}: {
  state: SectionState<MyClaimsPageDto>;
  retry: () => void;
  now: number;
}) {
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
  const hero = nextActionView(claims);
  // The hero already carries the most urgent claim; the rows show the rest.
  const rest = claims.filter(
    (claim) => hero === null || claim.claim_id !== hero.claimId,
  );
  if (rest.length === 0) {
    return (
      <EmptyState
        title="没有其他进行中的任务"
        hint="去任务广场看看还有什么可领取的"
      >
        <Link className="link" href="/tasks">
          浏览任务
        </Link>
      </EmptyState>
    );
  }
  const { active, revision } = claimsSummaryView(rest);
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
    // Review round 2 motif 1: claims as a TIMELINE, not cards — the
    // status node on the left rail carries the state (color supplements
    // the badge text, never replaces it).
    <Link className="claim-row-link" href={`/claims/${claim.claim_id}`}>
      <div className="claim-row" data-tone={status.tone}>
        <span className="claim-node" aria-hidden="true" />
        <div className="claim-row-main">
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
  rewards: RewardsShelf;
}) {
  const shelf = walletShelfView(wallet, rewards);
  return (
    // Review round 2: the stat band lives on the PAGE GROUND — no card.
    // One dominant balance, the next-reward progress as the quest rail
    // (motif 1), earned/frozen/debt as quiet secondary facts.
    <div className="stat-block">
      <span className="metric-label">可用积分</span>
      <span className="stat-focus">{wallet.available_points}</span>
      <span className="metric-label stat-sub-label">累计获得 {wallet.earned_points}</span>
      {shelf.shelf === "loading" ? (
        <span className="skeleton skeleton-line" data-width="narrow" aria-label="正在加载可兑换奖励" />
      ) : null}
      {shelf.shelf === "unavailable" ? (
        <p className="progress-note">暂时无法获取可兑换奖励，请稍后重试</p>
      ) : null}
      {shelf.shelf === "verdict" &&
      shelf.progress.status === "ready" &&
      shelf.progress.rewardName !== null &&
      shelf.progress.rewardCost !== null ? (
        <div className="goal-rail-block">
          <div
            className="goal-rail"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={shelf.progress.rewardCost}
            aria-valuenow={Math.min(shelf.progress.availablePoints, shelf.progress.rewardCost)}
            aria-label={`距离兑换「${shelf.progress.rewardName}」的进度`}
          >
            <div
              className="goal-rail-fill"
              style={{ width: `${Math.round(shelf.progress.ratio * 100)}%` }}
            />
            <span
              className="goal-rail-node"
              data-reached={shelf.progress.remainingPoints === null}
              aria-hidden="true"
            />
          </div>
          <p className="progress-note">
            {shelf.progress.remainingPoints === null
              ? `「${shelf.progress.rewardName}」（${shelf.progress.rewardCost} 积分）现在就可以兑换`
              : `距兑换「${shelf.progress.rewardName}」还差 ${shelf.progress.remainingPoints} 积分`}
          </p>
        </div>
      ) : null}
      {shelf.shelf === "verdict" && shelf.progress.status === "empty" ? (
        <p className="progress-note">暂无可兑换的奖励，完成任务先攒积分吧</p>
      ) : null}
      {shelf.pointDebt > 0 ? (
        <p className="progress-note">
          当前积分透支 {shelf.pointDebt}（可用与可花费已按 0 显示），新获得的积分会先偿还透支部分
        </p>
      ) : null}
      {shelf.frozenPoints > 0 ? (
        <p className="progress-note">
          有 {shelf.frozenPoints} 积分冻结在兑换申请中，兑换以可花费余额为准
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
      <EmptyState
        title="本月暂无排名"
        hint="完成任务获得积分后即可登上月榜"
      />
    );
  }
  // Compact snapshot (motif 3): rank + score as the anchor, a mini
  // top-3 underneath — no card, no equal-weight duel with the points.
  return (
    <div className="stat-block">
      <span className="metric-label">本月排名</span>
      <span className="stat-focus stat-focus-rank">
        第 {view.rank}
        <span className="metric-unit"> 名 · {view.score} 分</span>
      </span>
      {view.top.length > 0 ? (
        <ol className="rank-mini">
          {view.top.map((entry) => (
            <li key={entry.rank}>
              <span className="rank-mini-n">#{entry.rank}</span>
              <span className="rank-mini-name">{entry.nickname}</span>
              <span className="rank-mini-score meta-num">{entry.score}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

// --- task discovery preview (deduped vs open claims) -------------------------------

function TasksPreviewSection({
  now,
  claims,
}: {
  now: number;
  claims: MyClaimDto[];
}) {
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
          <TaskPreviewBody cards={state.data.items} claims={claims} now={now} />
        )
      ) : null}
    </section>
  );
}

function TaskPreviewBody({
  cards,
  claims,
  now,
}: {
  cards: TaskCardDto[];
  claims: MyClaimDto[];
  now: number;
}) {
  // Dedup rides ONLY the ready claims (a loading claims section shows
  // the raw cards rather than a duplicate-free guess).
  const deduped = withoutClaimedTasks(cards, claims);
  if (deduped.length === 0) {
    return (
      <EmptyState
        title="最新任务都在你手上"
        hint="你已领取了最新发布的任务，去任务广场看看其他机会"
      >
        <Link className="link" href="/tasks">
          浏览全部任务
        </Link>
      </EmptyState>
    );
  }
  return (
    <ul className="task-grid">
      {deduped.map((card) => (
        <li key={card.id}>
          <TaskCard card={card} nowMs={now} />
        </li>
      ))}
    </ul>
  );
}

// --- recent notifications preview --------------------------------------------------

function NotificationsPreviewSection() {
  const { state, retry } = useSection(() =>
    listNotifications({ limit: NOTIFICATIONS_PREVIEW_LIMIT }),
  );

  return (
    <section className="section" aria-label="最近通知">
      <SectionHeading
        title="最近通知"
        action={
          <Link className="link section-link" href="/notifications">
            查看全部
          </Link>
        }
      />
      {state.status === "loading" ? <SectionSkeleton lines={3} /> : null}
      {state.status === "error" ? (
        <SectionError error={state.error} onRetry={retry} />
      ) : null}
      {state.status === "ready" ? (
        state.data.items.length === 0 ? (
          <EmptyState title="暂无通知" hint="任务与审核的进展会出现在这里" />
        ) : (
          <ul className="dash-notes">
            {state.data.items.map((item) => (
              <li key={item.id} className="dash-note" data-unread={item.read_at === null}>
                <Link className="dash-note-link" href="/notifications">
                  <span className="dash-note-title">{item.title}</span>
                  <span className="dash-note-time" suppressHydrationWarning>
                    {formatRelativeHint(item.created_at)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </section>
  );
}

/** Coarse relative hint for preview rows (the inbox page owns precise formatting). */
function formatRelativeHint(iso: string): string {
  const minutes = Math.round((Date.now() - parseServerInstant(iso)) / 60_000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} 天前`;
  return "更早";
}
