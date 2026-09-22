"use client";
/**
 * OTP resend countdown (patterns §14: presentation-only derivation).
 *
 * The backend owns the real cooldown (§33.2); this hook only mirrors it for
 * display so the user sees WHY the resend button is disabled and when it
 * comes back. One interval while running, cleaned up on unmount or when the
 * countdown reaches zero.
 */
import { useCallback, useEffect, useState } from "react";

export interface UseCooldownResult {
  /** Whole seconds left; 0 means "not cooling down". */
  remaining: number;
  /** Begin a countdown of `seconds` (minimum 1 so the button always visibly disables). */
  start: (seconds: number) => void;
  /** Cancel immediately (e.g. the challenge was reset). */
  clear: () => void;
}

export function useCooldown(): UseCooldownResult {
  const [remaining, setRemaining] = useState(0);
  const running = remaining > 0;

  useEffect(() => {
    if (!running) {
      return;
    }
    const timer = setInterval(() => {
      setRemaining((value) => (value > 0 ? value - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [running]);

  const start = useCallback((seconds: number) => {
    setRemaining(Math.max(1, Math.ceil(seconds)));
  }, []);

  const clear = useCallback(() => setRemaining(0), []);

  return { remaining, start, clear };
}
