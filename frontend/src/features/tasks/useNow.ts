"use client";
/**
 * Ticking display clock (patterns §14; spec §42 "倒计时仅用于 UX").
 *
 * Re-renders the island with a fresh timestamp on an interval so countdown
 * text stays current. The clock runs SERVER-RELATIVE: every tick adds the
 * shared server-clock offset estimated from response `Date` headers
 * (`lib/serverClock.ts`, fed by `apiRequest`), so a drifted client clock
 * does not skew countdown text (spec §9.3).
 *
 * The result is still DISPLAY-ONLY: when a countdown crosses a deadline
 * boundary the owning surface refetches the authoritative state instead
 * of trusting this clock (patterns §14; spec §9.3).
 */
import { useEffect, useState } from "react";

import { currentServerClockOffset } from "@/lib/serverClock";
import { serverNow } from "@/lib/time";

export function useNow(intervalMs: number): number {
  // Offset 0 until the first API response feeds the estimate, so SSR and
  // pre-fetch renders stay stable (hydration-safe local time).
  const [now, setNow] = useState(() => serverNow(currentServerClockOffset()));
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(serverNow(currentServerClockOffset()));
    }, intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
