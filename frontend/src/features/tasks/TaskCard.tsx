/**
 * Task Card (spec §42; design-system §8/§9): title, rarity accent,
 * base reward, deadline mode + remaining time, availability COUNT
 * (never an assignment list), and aggregate rating.
 *
 * The title link is visually stretched across the card so the hover
 * affordance matches the real hit area. Rarity remains a text + color
 * cue; color never carries meaning alone.
 */
import Link from "next/link";

import { ClockIcon, SlotsIcon } from "@/components/shell/navIcons";

import type { TaskCardDto } from "./api";
import { availabilityText, deadlineView, ratingText, rarityView } from "./display";

export interface TaskCardProps {
  card: TaskCardDto;
  /** Island clock (epoch ms) — display-only countdown source. */
  nowMs: number;
}

export function TaskCard({ card, nowMs }: TaskCardProps) {
  const rarity = rarityView(card.rarity);
  const deadline = deadlineView(card, nowMs);
  return (
    <article className="task-card" data-rarity={rarity.rarity}>
      <div className="task-card-head">
        <h3 className="task-card-title">
          <Link href={`/tasks/${card.id}`}>{card.title}</Link>
        </h3>
        <span className="task-card-rarity" data-rarity={rarity.rarity}>
          {rarity.label}
        </span>
      </div>

      <p className="task-card-reward">
        <span>基础</span>
        <strong>{card.base_reward_points}</strong>
        <span className="task-card-reward-unit">积分</span>
      </p>

      <div className="task-card-meta">
        <span className="task-card-meta-item" suppressHydrationWarning>
          <ClockIcon />
          {deadline.line}
        </span>
        <span className="task-card-meta-item meta-num">
          <SlotsIcon />
          {availabilityText(card.assignments_available)}
        </span>
        <span suppressHydrationWarning>{ratingText(card.rating)}</span>
      </div>

      {deadline.urgency === "near" ? (
        <span className="badge badge-warning">临近截止</span>
      ) : null}
      {deadline.urgency === "closed" ? (
        <span className="badge badge-danger">已截止</span>
      ) : null}
    </article>
  );
}
