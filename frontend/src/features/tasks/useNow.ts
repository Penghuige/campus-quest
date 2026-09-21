"use client";
/**
 * Ticking display clock (patterns §14; spec §42 "倒计时仅用于 UX").
 *
 * Re-renders the island with a fresh `Date.now()` on an interval so
 * countdown text stays current. SERVER TIME IS AUTHORITATIVE: when a
 * countdown crosses a deadline boundary the owning surface refetches the
 * authoritative state instead of trusting this clock (spec §9.3).
 */
import { useEffect, useState } from "react";

export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
