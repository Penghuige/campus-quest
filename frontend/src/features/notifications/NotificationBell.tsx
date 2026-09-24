"use client";
/**
 * Notification bell (Plan 09 Task 7; spec §28): the topbar affordance
 * linking to /notifications with a live unread count.
 *
 * Count source: the inbox endpoint has no count field, so the bell polls
 * the UNREAD first page (limit 1) and reads its `total` — the server's
 * own count (`fetchUnreadCount`, api.ts). POLLING CONTRACT (patterns §4
 * server state; the session hook's documented SWR-style stance, plus an
 * interval because a bell is expected to tick while a tab sits open):
 * - fetch on mount; then every POLL_INTERVAL_MS (60s);
 * - refetch on tab focus/visibility when the last result is older than
 *   the 30s fresh window (stale-while-revalidate — navigating back from
 *   the inbox refreshes the badge instead of showing a stale count);
 * - failures are SILENT: a badge poll must never break the shell, so the
 *   previous count stays displayed (no badge until the first success).
 *   401 is moot (the shell renders this only when authenticated); a
 *   staff token's 403 PERMISSION_DENIED simply never shows a badge.
 *
 * The badge is aria-hidden; the link's accessible name (bellLabel)
 * carries the count, so screen readers hear "未读通知（3 条）" exactly
 * once per change.
 */
import Link from "next/link";
import { useEffect, useState } from "react";

import { BellIcon } from "@/components/shell/navIcons";
import { fetchUnreadCount } from "@/features/notifications/api";
import { bellLabel, unreadBadgeText } from "@/features/notifications/inboxView";

const POLL_INTERVAL_MS = 60_000;
const FRESH_WINDOW_MS = 30_000;

export function NotificationBell() {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    let fetchedAt = 0;

    const load = () => {
      fetchedAt = Date.now();
      fetchUnreadCount().then(
        (value) => {
          if (!cancelled) {
            setCount(value);
          }
        },
        () => {
          // Silent by contract (module docstring): keep the last count.
        },
      );
    };

    const revalidateIfStale = () => {
      if (Date.now() - fetchedAt >= FRESH_WINDOW_MS) {
        load();
      }
    };

    load();
    const timer: ReturnType<typeof setInterval> = setInterval(
      revalidateIfStale,
      POLL_INTERVAL_MS,
    );
    document.addEventListener("visibilitychange", revalidateIfStale);
    window.addEventListener("focus", revalidateIfStale);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", revalidateIfStale);
      window.removeEventListener("focus", revalidateIfStale);
    };
  }, []);

  const badge = count === null ? null : unreadBadgeText(count);

  return (
    <Link href="/notifications" className="topbar-bell" aria-label={bellLabel(count)}>
      <span className="bell-glyph" aria-hidden="true">
        <BellIcon />
      </span>
      {badge !== null ? (
        <span className="bell-badge" aria-hidden="true">
          {badge}
        </span>
      ) : null}
    </Link>
  );
}
