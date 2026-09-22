"use client";
/**
 * Reward status panel for the claim view (spec §11.2/§42; patterns §3):
 * renders the SERVER view model from `rewardView` verbatim — this
 * component owns layout only, never reward math. No tier percentages,
 * no ladders: numbers on screen are server values.
 */
import { rewardStatusView, type RewardClaimFacts } from "./rewardView";

export function RewardStatus({ claim, nowMs }: { claim: RewardClaimFacts; nowMs: number }) {
  const view = rewardStatusView(claim, nowMs);
  return (
    <section className="section reward-status" aria-label="奖励状态">
      <h2 className="section-title">奖励</h2>
      <div className={`reward-lines reward-${view.tone}`} role="status">
        {view.lines.map((line) => (
          <p key={line} className="reward-line">
            {line}
          </p>
        ))}
      </div>
    </section>
  );
}
