/**
 * Task Card (spec §42; design-system §8/§9): title, rarity accent,
 * base reward, deadline mode + remaining time, availability COUNT
 * (never an assignment list), and aggregate rating.
 *
 * The title link is visually stretched across the card so the hover
 * affordance matches the real hit area. Rarity is a shape + color +
 * label cue (defect #5: one distinct glyph per tier, the accent never
 * carrying meaning alone); the card itself carries the tier as a
 * restrained border + wash tint (data-rarity), and NORMAL stays
 * neutral — neutral IS its corresponding color.
 */
import Link from "next/link";

import {
  ClockIcon,
  RarityEpicIcon,
  RarityLegendaryIcon,
  RarityNormalIcon,
  RarityRareIcon,
  SlotsIcon,
} from "@/components/shell/navIcons";

import type { TaskCardDto } from "./api";
import { cardAvailabilityMeta, deadlineView, ratingText, rarityView } from "./display";

/** One distinct shape per tier (defect #5) — shape separable without color. */
const RARITY_GLYPHS = {
  NORMAL: RarityNormalIcon,
  RARE: RarityRareIcon,
  EPIC: RarityEpicIcon,
  LEGENDARY: RarityLegendaryIcon,
} as const;

export interface TaskCardProps {
  card: TaskCardDto;
  /** Island clock (epoch ms) — display-only countdown source. */
  nowMs: number;
}

export function TaskCard({ card, nowMs }: TaskCardProps) {
  const rarity = rarityView(card.rarity);
  const deadline = deadlineView(card, nowMs);
  const RarityGlyph = RARITY_GLYPHS[rarity.rarity];
  const depleted = card.assignments_available === 0;
  return (
    <article
      className="task-card"
      data-rarity={rarity.rarity}
      data-depleted={depleted ? "true" : undefined}
    >
      <div className="task-card-head">
        <h3 className="task-card-title">
          <Link href={`/tasks/${card.id}`}>{card.title}</Link>
        </h3>
        <span className="task-card-rarity" data-rarity={rarity.rarity}>
          <RarityGlyph />
          {rarity.label}
        </span>
      </div>

      <p className="task-card-reward">
        <span>基础</span>
        <strong>{card.base_reward_points}</strong>
        <span className="task-card-reward-unit">积分</span>
      </p>

      <div className="task-card-meta">
        {/* The deadline item is the card's CLOCK-DERIVED string — the
         * visual suite masks .meta-deadline (square + dashboard
         * discovery) so baselines stay stable across capture times
         * until the world mint pins its clock. */}
        <span className="task-card-meta-item meta-deadline" suppressHydrationWarning>
          <ClockIcon />
          {deadline.line}
        </span>
        <span className="task-card-meta-item meta-num">
          <SlotsIcon />
          {cardAvailabilityMeta(card.assignments_available)}
        </span>
        <span className="task-card-meta-item" suppressHydrationWarning>
          {ratingText(card.rating)}
        </span>
      </div>

      {deadline.urgency === "near" ? (
        <span className="badge badge-warning">临近截止</span>
      ) : null}
      {deadline.urgency === "closed" ? (
        <span className="badge badge-danger">已截止</span>
      ) : null}
      {/* Defect #20 (QA 2026-10-03): a fully-claimed task stays
          browsable on the square but carries an explicit depleted MARK
          — a text badge (never a color-only cue), sibling of the
          deadline badges. Batch ④ (2026-10-10): data-depleted drives
          the restrained recede treatment in CSS. */}
      {card.assignments_available === 0 ? (
        <span className="badge badge-muted">已被领完</span>
      ) : null}
    </article>
  );
}
