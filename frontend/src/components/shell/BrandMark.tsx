/**
 * CampusQuest brand mark (Plan 11 review round 2: the rail gets a real
 * brand object, not plain text) — a restrained quest-path monogram:
 * two nodes joined by a rising path, terminal flag/check, inside a
 * rounded tile. One-color via currentColor: inverse on the ink rail,
 * monochrome anywhere else (favicon/PWA can reuse the same geometry).
 * No mascot, no gradient — "product", not "school portal logo".
 */
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 28 28"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="1" y="1" width="26" height="26" rx="7" fill="currentColor" opacity="0.14" />
      <rect x="1" y="1" width="26" height="26" rx="7" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M7.5 19.5c2.8 0 3.4-4 6-4s3.2 2.5 6 2.5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <circle cx="7.5" cy="19.5" r="2" fill="currentColor" />
      <path d="M19.5 15.5v7M19.5 15.5l3.2 1.6-3.2 1.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
