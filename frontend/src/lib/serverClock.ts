/**
 * Process-wide server-clock offset estimate (spec §9.3; patterns §14).
 *
 * `lib/time.estimateServerClockOffset` derives the offset from one HTTP
 * `Date` response header; this module is the SHARED HOME of the latest
 * estimate so every consumer counts down against the same server time:
 *
 * - `apiRequest` feeds the store from every response's `Date` header
 *   (`lib/api.ts` wiring — the one place all API traffic already flows);
 * - `useNow` reads `currentServerClockOffset()` per tick, so countdowns
 *   render server-relative time without each caller plumbing an offset.
 *
 * Estimation contract (mirrors `estimateServerClockOffset`): the `Date`
 * header has one-second resolution and no RTT measurement, so the sample
 * runs late by ~half a round trip — ample for display text, useless for
 * enforcement (the backend remains the sole deadline authority).
 *
 * Update policy: LATEST VALID SAMPLE WINS, and a sample beyond ±24h is
 * discarded as proxy/CDN garbage rather than shifting every countdown by
 * hours. SSR-safe: the store is only written from `apiRequest` responses,
 * which browser sessions drive; a server-render pass keeps offset 0.
 */

import { estimateServerClockOffset } from "./time";

/** A sample a full day off is clock garbage, not drift. */
const MAX_ABS_OFFSET_MS = 24 * 60 * 60 * 1000;

let offsetMs = 0;

/**
 * Feed one response `Date` header into the estimate. Missing/unparseable
 * headers (and absurd samples) leave the previous offset untouched.
 */
export function observeServerDateHeader(dateHeader: string | null): void {
  const sample = estimateServerClockOffset(dateHeader, Date.now());
  if (sample === null || Math.abs(sample) > MAX_ABS_OFFSET_MS) {
    return;
  }
  offsetMs = sample;
}

/** The current best offset to add to the local clock (0 until fed). */
export function currentServerClockOffset(): number {
  return offsetMs;
}

/** Test seam: reset the store to the no-estimate default. */
export function resetServerClockForTests(): void {
  offsetMs = 0;
}
