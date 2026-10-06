"use client";
/**
 * Reward tile × CTA-state fixtures for the dev gallery (plan-13 T1).
 *
 * The seeded e2e world carries exactly one stocked reward and a student
 * who can afford it, so the 积分不足 / 缺货 / 窗口关闭 tile states never
 * render there — this composition makes every state inspectable and
 * pixel-baselined WITHOUT touching world seeding (the gallery's own
 * rules apply: existing classes + the real RewardCard only, fully
 * deterministic fixtures — the closed window's bound is a fixed ISO
 * instant through the pinned business timezone).
 *
 * Client boundary: RewardCard renders a real button with a handler, and
 * event handlers cannot cross from the server gallery page — the no-op
 * lives here.
 */
import type { RewardItemDto } from "@/features/points/api";
import { RewardCard } from "@/features/rewards/RewardsView";

function fixture(overrides: Partial<RewardItemDto>): RewardItemDto {
  return {
    id: "00000000-0000-4000-8000-0000000000a1",
    name: "文创帆布包",
    description: "示例奖励描述，安静的一行说明文字。",
    point_cost: 50,
    stock: 10,
    per_user_term_limit: null,
    available_from: null,
    available_until: null,
    window_open: true,
    ...overrides,
  };
}

const REDEEMABLE = fixture({});
const INSUFFICIENT = fixture({ id: "00000000-0000-4000-8000-0000000000a2" });
const OUT_OF_STOCK = fixture({
  id: "00000000-0000-4000-8000-0000000000a3",
  stock: 0,
});
const WINDOW_CLOSED = fixture({
  id: "00000000-0000-4000-8000-0000000000a4",
  window_open: false,
  available_until: "2026-10-20T12:00:00Z",
});

const noop = () => {};

export function RewardTileGallery() {
  return (
    <section className="section" aria-label="奖励卡片">
      <h2 className="section-title">奖励卡片 × 兑换状态</h2>
      <p className="progress-note">可花费 120 积分 · 成本 50 积分 → 可兑换 / 缺货 / 窗口关闭</p>
      <ul className="reward-grid">
        <RewardCard item={REDEEMABLE} spendablePoints={120} onRedeem={noop} />
        <RewardCard item={OUT_OF_STOCK} spendablePoints={120} onRedeem={noop} />
        <RewardCard item={WINDOW_CLOSED} spendablePoints={120} onRedeem={noop} />
      </ul>
      <p className="progress-note">可花费 30 积分 · 成本 50 积分 → 积分不足（禁用 + 还差提示）</p>
      <ul className="reward-grid">
        <RewardCard item={INSUFFICIENT} spendablePoints={30} onRedeem={noop} />
      </ul>
    </section>
  );
}
