"use client";
/**
 * Rewards page island (spec §16/§16.2, §42; patterns §8): the wallet
 * hero (ONE dominant spendable balance + the next-reward progress strip;
 * plan-13 T1 / plan-11 P2 ruling), the reward shelf (server-side
 * window/stock verdicts), and the redeem dialog orchestration.
 *
 * Hierarchy contract (plan-11 P2, owner 2026-09-24): the spendable
 * figure is the page's first read; 可用/累计/冻结/负债 ride as quiet
 * metadata; each shelf tile leads with icon + name + cost, keeps
 * availability/stock quiet, and its CTA carries the state — primary 兑换
 * / disabled-with-distance 积分不足 / quiet unavailable 缺货·窗口关闭.
 *
 * Boundary rule (patterns §3/§7): a successful redemption NEVER mutates
 * local wallet/shelf figures — `onRedeemed` refetches both endpoints
 * and the display reflects the server's answer (frozen spendability
 * included). The "最近兑换" panel is session-local state because the
 * student surface has no my-redemptions LIST endpoint in this snapshot
 * (documented gap); its status row renders the server response's own
 * status verbatim through the lifecycle labels.
 */
import { useState } from "react";

import { GiftIcon } from "@/components/shell/navIcons";
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
  type RedemptionDto,
  type RewardItemDto,
  type WalletDto,
} from "@/features/points/api";
import { RedeemDialog } from "@/features/rewards/RedeemDialog";
import {
  nextRewardView,
  redemptionStatusView,
  rewardCtaView,
  rewardShelfView,
} from "@/features/rewards/redeemView";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

/** One session-local redemption receipt (item name + server response). */
interface LatestRedemption {
  redemption: RedemptionDto;
  itemName: string;
}
import { Button } from "@/components/ui/button";

export function RewardsView() {
  const wallet = useSection(() => myWallet(), "GET /api/v1/points/me");
  const shelf = useSection(() => listRewards(), "GET /api/v1/rewards");
  const [dialogReward, setDialogReward] = useState<RewardItemDto | null>(null);
  const [latest, setLatest] = useState<LatestRedemption | null>(null);

  const spendablePoints =
    wallet.state.status === "ready" ? wallet.state.data.spendable_points : null;

  return (
    <div className="rewards-view">
      <WalletSection
        status={wallet.state.status}
        error={wallet.state.status === "error" ? wallet.state.error : null}
        data={wallet.state.status === "ready" ? wallet.state.data : null}
        retry={wallet.retry}
        shelfItems={shelf.state.status === "ready" ? shelf.state.data.items : null}
      />
      {latest !== null ? <LatestRedemptionPanel latest={latest} /> : null}
      <ShelfSection
        status={shelf.state.status}
        error={shelf.state.status === "error" ? shelf.state.error : null}
        items={shelf.state.status === "ready" ? shelf.state.data.items : null}
        retry={shelf.retry}
        spendablePoints={spendablePoints}
        onRedeem={setDialogReward}
      />
      {dialogReward !== null ? (
        <RedeemDialog
          key={dialogReward.id}
          reward={dialogReward}
          spendablePoints={spendablePoints}
          open
          onClose={() => setDialogReward(null)}
          onRedeemed={(redemption, item) => {
            setLatest({ redemption, itemName: item.name });
            // Authoritative refetch at the boundary (patterns §3): the
            // wallet shows the new freeze, the shelf the new stock.
            wallet.retry();
            shelf.retry();
          }}
        />
      ) : null}
    </div>
  );
}

// --- wallet hero ------------------------------------------------------------------

function WalletSection({
  status,
  error,
  data,
  retry,
  shelfItems,
}: {
  status: "loading" | "ready" | "error";
  error: unknown;
  data: WalletDto | null;
  retry: () => void;
  /** Ready-shelf items feeding the next-reward strip; null until the
   * shelf's own verdict arrives (no strip without it — the absence of a
   * purchasable item is a server-verdicted fact). */
  shelfItems: RewardItemDto[] | null;
}) {
  const frozen =
    data === null
      ? 0
      : Math.max(data.available_points - data.spendable_points, 0);
  const goal =
    data !== null && shelfItems !== null
      ? nextRewardView(data.spendable_points, shelfItems)
      : null;
  return (
    <section className="section" aria-label="积分余额">
      <SectionHeading title="积分余额" />
      {status === "loading" ? <SectionSkeleton lines={2} /> : null}
      {status === "error" ? <SectionError error={error} onRetry={retry} /> : null}
      {status === "ready" && data !== null ? (
        <>
          {/* The hero lives on the PAGE GROUND (the dashboard stat band's
              grammar): one dominant number, quiet secondary facts — no
              equal-weight metric row, no card. */}
          <div
            className={
              goal !== null ? "balance-hero balance-hero-with-goal" : "balance-hero"
            }
          >
            <div className="balance-hero-main">
              <p className="balance-hero-stat">
                <span className="metric-label">可花费</span>
                <span className="stat-focus">{data.spendable_points}</span>
                <span className="balance-hero-unit">积分</span>
              </p>
              <p className="balance-quiet">
                可用积分 {data.available_points} · 累计获得 {data.earned_points}
              </p>
            </div>
            {goal !== null ? (
              <div className="goal-rail-block balance-hero-goal">
                <div
                  className="goal-rail"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={goal.rewardCost}
                  aria-valuenow={Math.min(data.spendable_points, goal.rewardCost)}
                  aria-label={`距离兑换「${goal.rewardName}」的进度`}
                >
                  <div
                    className="goal-rail-fill"
                    style={{ width: `${Math.round(goal.ratio * 100)}%` }}
                  />
                  <span
                    className="goal-rail-node"
                    data-reached={goal.remainingPoints === null}
                    aria-hidden="true"
                  />
                </div>
                <p className="progress-note">
                  {goal.remainingPoints === null
                    ? `「${goal.rewardName}」（${goal.rewardCost} 积分）现在就可以兑换`
                    : `距兑换「${goal.rewardName}」还差 ${goal.remainingPoints} 积分`}
                </p>
              </div>
            ) : null}
          </div>
          {data.point_debt > 0 ? (
            <p className="progress-note">
              当前积分透支 {data.point_debt}（可用与可花费已按 0 显示），新获得的积分会先偿还透支部分
            </p>
          ) : null}
          {frozen > 0 ? (
            <p className="progress-note">
              有 {frozen} 积分冻结在兑换申请中，兑换以可花费余额为准
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

// --- latest redemption (session-local receipt) --------------------------------------

function LatestRedemptionPanel({ latest }: { latest: LatestRedemption }) {
  const status = redemptionStatusView(latest.redemption.status);
  return (
    <section className="section" aria-label="最近兑换">
      <SectionHeading title="最近兑换" />
      <div className="panel">
        <div className="claim-row-top">
          <span className="claim-title">{latest.itemName}</span>
          <span className={`badge badge-${status.tone}`}>{status.label}</span>
        </div>
        <p className="claim-deadline">
          消耗 {latest.redemption.points} 积分 ·{" "}
          {formatDeadlineDateTime(
            parseServerInstant(latest.redemption.created_at),
          )}
        </p>
        <p className="field-hint">
          审核通过前积分保持冻结；如未通过将全额退回。历史兑换记录页面将在后续版本提供。
        </p>
      </div>
    </section>
  );
}

// --- reward shelf -------------------------------------------------------------------

function ShelfSection({
  status,
  error,
  items,
  retry,
  spendablePoints,
  onRedeem,
}: {
  status: "loading" | "ready" | "error";
  error: unknown;
  items: RewardItemDto[] | null;
  retry: () => void;
  /** The wallet's server-verbatim spendable figure; null until the
   * wallet loads (tiles then keep the pre-plan-13 enabled behavior —
   * the server's typed conflict teaches). */
  spendablePoints: number | null;
  onRedeem: (item: RewardItemDto) => void;
}) {
  return (
    <section className="section" aria-label="奖励货架">
      <SectionHeading title="奖励货架" />
      {status === "loading" ? <SectionCardsSkeleton cards={3} /> : null}
      {status === "error" ? <SectionError error={error} onRetry={retry} /> : null}
      {status === "ready" && items !== null ? (
        items.length === 0 ? (
          <EmptyState
            title="暂无可兑换的奖励"
            hint="老师还没有上架奖励，完成任务先攒积分吧"
          />
        ) : (
          <ul className="reward-grid">
            {items.map((item) => (
              <RewardCard
                key={item.id}
                item={item}
                spendablePoints={spendablePoints}
                onRedeem={onRedeem}
              />
            ))}
          </ul>
        )
      ) : null}
    </section>
  );
}

/**
 * One shelf tile. Exported for the dev-only component gallery's fixture
 * composition of the CTA states (plan-13 T1): the e2e world's single
 * stocked item can never show 积分不足 / 缺货, so those variants are
 * reviewable (and pixel-baselined) only through the gallery.
 */
export function RewardCard({
  item,
  spendablePoints,
  onRedeem,
}: {
  item: RewardItemDto;
  spendablePoints: number | null;
  onRedeem: (item: RewardItemDto) => void;
}) {
  const view = rewardShelfView(item);
  const cta = rewardCtaView(item, spendablePoints);
  return (
    <li className="reward-card">
      <div className="reward-card-head">
        <span className="reward-icon" aria-hidden="true">
          <GiftIcon />
        </span>
        <h3 className="reward-card-title">{item.name}</h3>
      </div>
      {item.description !== null ? (
        <p className="reward-card-desc">{item.description}</p>
      ) : null}
      {/* Cost is the tile's primary signal; stock/window stay quiet
          metadata (both are the server's own verdicts verbatim). */}
      <p className="reward-price">
        <span className="reward-cost meta-num">{item.point_cost} 积分</span>
      </p>
      {view.stockLabel !== null || view.windowLabel !== null ? (
        <div className="reward-card-meta">
          {view.stockLabel !== null ? <span>{view.stockLabel}</span> : null}
          {view.windowLabel !== null ? <span>{view.windowLabel}</span> : null}
        </div>
      ) : null}
      {cta.kind === "unavailable" ? (
        // 缺货 / 窗口关闭: quiet unavailable treatment — no button to
        // knock on; the state text + the quiet metadata carry it.
        <p className="reward-unavailable">{view.stateLabel}</p>
      ) : (
        <>
          {/* Spendability is the server's call (patterns §3): only the
              wallet's OWN spendable figure disables the button, and any
              stale-wallet attempt still meets the typed conflict. */}
          <Button
            variant="primary"
            onClick={() => onRedeem(item)}
            disabled={cta.kind === "insufficient"}
          >
            兑换
          </Button>
          {cta.kind === "insufficient" ? (
            <p className="reward-cta-note">还差 {cta.missingPoints} 积分</p>
          ) : null}
        </>
      )}
    </li>
  );
}
