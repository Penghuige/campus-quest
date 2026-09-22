/**
 * Task Card (spec §42; design-system §8/§9): title, rarity badge (a LOCAL
 * accent — never a page-wide treatment), base reward, deadline mode +
 * remaining time, availability COUNT (never an assignment list), and the
 * aggregate rating, with the display-only near-cutoff hint.
 *
 * Presentational and time-explicit: `nowMs` comes from the parent island.
 * Text derived from `now` opts out of hydration comparison via
 * `suppressHydrationWarning` (server and client legitimately render at
 * different instants; the values are display-only per §42).
 */
import Link from "next/link";

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
    <article className="task-card">
      <div className="task-card-top">
        <h3 className="task-card-title">
          <Link href={`/tasks/${card.id}`}>{card.title}</Link>
        </h3>
        <span className="rarity-badge" data-rarity={rarity.rarity}>
          {rarity.label}
        </span>
      </div>
      <p className="task-card-reward">基础 {card.base_reward_points} 积分</p>
      <div className="task-card-meta">
        <span suppressHydrationWarning>{deadline.line}</span>
        <span className="meta-num">{availabilityText(card.assignments_available)}</span>
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
