"use client";
/**
 * Rewards page island (spec §16/§16.2, §42; patterns §8): the wallet
 * strip (available / earned / spendable with the freeze note), the
 * reward shelf (server-side window/stock verdicts), and the redeem
 * dialog orchestration.
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
} from "@/features/points/api";
import { RedeemDialog } from "@/features/rewards/RedeemDialog";
import {
  redemptionStatusView,
  rewardShelfView,
} from "@/features/rewards/redeemView";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

/** One session-local redemption receipt (item name + server response). */
interface LatestRedemption {
  redemption: RedemptionDto;
  itemName: string;
}

export function RewardsView() {
  const wallet = useSection(() => myWallet());
  const shelf = useSection(() => listRewards());
  const [dialogReward, setDialogReward] = useState<RewardItemDto | null>(null);
  const [latest, setLatest] = useState<LatestRedemption | null>(null);

  return (
    <>
      <WalletSection
        status={wallet.state.status}
        error={wallet.state.status === "error" ? wallet.state.error : null}
        data={wallet.state.status === "ready" ? wallet.state.data : null}
        retry={wallet.retry}
      />
      {latest !== null ? <LatestRedemptionPanel latest={latest} /> : null}
      <ShelfSection
        status={shelf.state.status}
        error={shelf.state.status === "error" ? shelf.state.error : null}
        items={shelf.state.status === "ready" ? shelf.state.data.items : null}
        retry={shelf.retry}
        onRedeem={setDialogReward}
      />
      {dialogReward !== null ? (
        <RedeemDialog
          key={dialogReward.id}
          reward={dialogReward}
          spendablePoints={
            wallet.state.status === "ready"
              ? wallet.state.data.spendable_points
              : null
          }
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
    </>
  );
}

// --- wallet strip -------------------------------------------------------------------

function WalletSection({
  status,
  error,
  data,
  retry,
}: {
  status: "loading" | "ready" | "error";
  error: unknown;
  data: {
    available_points: number;
    earned_points: number;
    spendable_points: number;
    point_debt: number;
  } | null;
  retry: () => void;
}) {
  const frozen =
    data === null
      ? 0
      : Math.max(data.available_points - data.spendable_points, 0);
  return (
    <section className="section" aria-label="积分余额">
      <SectionHeading title="积分余额" />
      {status === "loading" ? <SectionSkeleton lines={2} /> : null}
      {status === "error" ? <SectionError error={error} onRetry={retry} /> : null}
      {status === "ready" && data !== null ? (
        <div className="panel">
          <div className="metric-row">
            <div className="metric">
              <span className="metric-label">可用积分</span>
              <span className="metric-value">{data.available_points}</span>
            </div>
            <div className="metric">
              <span className="metric-label">累计获得</span>
              <span className="metric-value">{data.earned_points}</span>
            </div>
            <div className="metric">
              <span className="metric-label">可花费</span>
              <span className="metric-value">{data.spendable_points}</span>
            </div>
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
        </div>
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
  onRedeem,
}: {
  status: "loading" | "ready" | "error";
  error: unknown;
  items: RewardItemDto[] | null;
  retry: () => void;
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
              <RewardCard key={item.id} item={item} onRedeem={onRedeem} />
            ))}
          </ul>
        )
      ) : null}
    </section>
  );
}

function RewardCard({
  item,
  onRedeem,
}: {
  item: RewardItemDto;
  onRedeem: (item: RewardItemDto) => void;
}) {
  const view = rewardShelfView(item);
  return (
    <li className="reward-card">
      <div className="reward-card-top">
        <h3 className="reward-card-title">{item.name}</h3>
        <span
          className={
            view.state === "redeemable" ? "badge badge-success" : "badge"
          }
        >
          {view.stateLabel}
        </span>
      </div>
      {item.description !== null ? (
        <p className="reward-card-desc">{item.description}</p>
      ) : null}
      <div className="reward-card-meta">
        <span className="reward-cost meta-num">{item.point_cost} 积分</span>
        {view.stockLabel !== null ? <span>{view.stockLabel}</span> : null}
        {view.windowLabel !== null ? <span>{view.windowLabel}</span> : null}
      </div>
      {/* Spendability is the server's call (patterns §3): the button
          stays enabled on redeemable items and typed conflicts teach. */}
      <button
        type="button"
        className="btn btn-primary"
        onClick={() => onRedeem(item)}
        disabled={view.state !== "redeemable"}
      >
        兑换
      </button>
    </li>
  );
}
