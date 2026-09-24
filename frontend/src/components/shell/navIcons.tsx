/**
 * Plan 11 Task 3 navigation icons (brief §8 iconography, order 2: a
 * small local set of simple stroke SVGs — no third-party package).
 *
 * Stroke inherits `currentColor` so the icons ride the nav link's
 * color state (muted → primary on aria-current) without their own
 * palette; 20px grid, 1.8 stroke, round caps — one visual language.
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
