/**
 * CampusQuest shared stroke icon set (brief §8 iconography, order 2: a
 * small local set of simple stroke SVGs — no third-party package).
 *
 * Started as the navigation set (Plan 11 Task 3) and now also carries
 * the 14px context glyphs (motif 2) — one visual language: stroke
 * inherits `currentColor`, 20px grid, 1.8 stroke, round caps.
 */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function StrokeIcon({ children, ...props }: IconProps) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

export function HomeIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <path d="M3.5 8.8 10 3.5l6.5 5.3" />
      <path d="M5.5 8.5V16h9V8.5" />
      <path d="M8.5 16v-4h3v4" />
    </StrokeIcon>
  );
}

export function TasksIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <path d="M4 5.5h9M4 10h12M4 14.5h7" />
      <circle cx="16" cy="5.5" r="1.2" />
    </StrokeIcon>
  );
}

export function ReviewIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <rect x="4.5" y="3.5" width="11" height="13" rx="1.5" />
      <path d="M7.5 8l1.5 1.5 3-3M7.5 13h5" />
    </StrokeIcon>
  );
}

export function TrophyIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <path d="M6.5 3.5h7v4a3.5 3.5 0 0 1-7 0v-4Z" />
      <path d="M6.5 4.5H4a2 2 0 0 0 2 3.5M13.5 4.5H16a2 2 0 0 1-2 3.5" />
      <path d="M10 11v2.5M7 16.5h6M8 16.5c0-1.7.9-3 2-3s2 1.3 2 3" />
    </StrokeIcon>
  );
}

export function GiftIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <rect x="3.5" y="7" width="13" height="9" rx="1.5" />
      <path d="M3.5 10.5h13M10 7v9" />
      <path d="M10 7C9 7 6.8 6.7 6.3 5.2 5.9 4 6.8 3 8 3.2c1.6.3 2 2.4 2 3.8Zm0 0c1 0 3.2-.3 3.7-1.8.4-1.2-.5-2.2-1.7-2-1.6.3-2 2.4-2 3.8Z" />
    </StrokeIcon>
  );
}

export function UserIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <circle cx="10" cy="6.5" r="2.8" />
      <path d="M4.5 16.5c.8-3 3-4.5 5.5-4.5s4.7 1.5 5.5 4.5" />
    </StrokeIcon>
  );
}

export function InboxIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <path d="M3.5 10.5 5.5 4h9l2 6.5v4a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5v-4Z" />
      <path d="M3.5 10.5H8a2 2 0 0 0 4 0h4.5" />
    </StrokeIcon>
  );
}

export function BellIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <path d="M5.5 8.5a4.5 4.5 0 0 1 9 0c0 4 1.5 4.7 1.5 4.7H4s1.5-.7 1.5-4.7Z" />
      <path d="M8.2 15.2a2 2 0 0 0 3.6 0" />
    </StrokeIcon>
  );
}

export function MenuIcon(props: IconProps) {
  return (
    <StrokeIcon {...props}>
      <path d="M4 6h12M4 10h12M4 14h12" />
    </StrokeIcon>
  );
}

/*
 * Defect #3 (QA 2026-09-30): the rail collapse/expand affordance — a
 * double chevron pointing where the rail will go. 16px like the context
 * glyphs: a small affordance, not a navigation mark.
 */
export function PanelCollapseIcon(props: IconProps) {
  return (
    <StrokeIcon width="16" height="16" {...props}>
      <path d="M9.5 4.5 4.5 10l5 5.5" />
      <path d="M15.5 4.5 10.5 10l5 5.5" />
    </StrokeIcon>
  );
}

export function PanelExpandIcon(props: IconProps) {
  return (
    <StrokeIcon width="16" height="16" {...props}>
      <path d="M4.5 4.5 9.5 10l-5 5.5" />
      <path d="M10.5 4.5 15.5 10l-5 5.5" />
    </StrokeIcon>
  );
}

/*
 * Context glyphs (Plan 11 review round 2 motif 2): one coherent
 * 14px semantic set for HIGH-FREQUENCY metadata only — deadline,
 * reward, availability, review state, rank movement. Same stroke
 * language as the navigation set; never decorate every label.
 */

export function ClockIcon(props: IconProps) {
  return (
    <StrokeIcon width="14" height="14" strokeWidth="1.6" {...props}>
      <circle cx="10" cy="10" r="6.5" />
      <path d="M10 6.8V10l2.2 1.6" />
    </StrokeIcon>
  );
}

export function SlotsIcon(props: IconProps) {
  return (
    <StrokeIcon width="14" height="14" strokeWidth="1.6" {...props}>
      <circle cx="7" cy="8" r="2.6" />
      <circle cx="13.4" cy="8" r="2.6" />
      <path d="M3 15c.6-2 2.2-3 4-3s3.4 1 4 3M9 15c.6-2 2.2-3 4-3s2.8 1 3.4 2.4" />
    </StrokeIcon>
  );
}

/*
 * Defect #5 (QA 2026-09-30): the rarity mark set — one distinct 14px
 * SHAPE per tier so the difficulty is separable by shape alone (color
 * never carries meaning by itself), inheriting the accent color from
 * the badge text. Same stroke language; every path is centered on the
 * StrokeIcon's (10, 10) viewBox center so the glyph neither shrinks
 * nor drifts to a corner (Codex P2, PR #19).
 */
export function RarityNormalIcon(props: IconProps) {
  return (
    <StrokeIcon width="14" height="14" strokeWidth="1.6" {...props}>
      <circle cx="10" cy="10" r="5.5" />
    </StrokeIcon>
  );
}

export function RarityRareIcon(props: IconProps) {
  return (
    <StrokeIcon width="14" height="14" strokeWidth="1.6" {...props}>
      <path d="M10 3.5 16.5 10 10 16.5 3.5 10Z" />
    </StrokeIcon>
  );
}

export function RarityEpicIcon(props: IconProps) {
  return (
    <StrokeIcon width="14" height="14" strokeWidth="1.6" {...props}>
      <path d="M10 3.5 15.6 6.75v6.5L10 16.5 4.4 13.25v-6.5Z" />
    </StrokeIcon>
  );
}

export function RarityLegendaryIcon(props: IconProps) {
  return (
    <StrokeIcon width="14" height="14" strokeWidth="1.6" {...props}>
      <path d="M10 3 11.8 8.2 17 10l-5.2 1.8L10 17 8.2 11.8 3 10l5.2-1.8Z" />
    </StrokeIcon>
  );
}
