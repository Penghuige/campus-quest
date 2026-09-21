import type { Metadata } from "next";

import { NotificationInbox } from "@/features/notifications/NotificationInbox";
import { parseInboxFilter } from "@/features/notifications/inboxView";

export const metadata: Metadata = {
  title: "通知 · CampusQuest",
  description: "任务审核结果、截止提醒与兑换动态",
};

interface NotificationsPageProps {
  searchParams: Promise<{ filter?: string | string[] }>;
}

/**
 * Notification inbox (spec §28; patterns §4 — the unread filter is URL
 * state). The server parses `?filter=unread` (garbage degrades to 全部)
 * and REMOUNTS the island per filter, so every fetch keys off the tab,
 * never client state — the rankings page's exact pattern.
 *
 * Owner-only: the endpoint is the caller's own rows; the shell's session
 * gate already turned 401 into the login CTA, and a 403 surfaces as the
 * section's mapped error — no client-side access decision exists here.
 */
export default async function NotificationsPage({
  searchParams,
}: NotificationsPageProps) {
  const params = await searchParams;
  const raw = params.filter;
  const filter = parseInboxFilter(Array.isArray(raw) ? raw[0] : raw);

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">通知</h1>
        <p className="page-subtitle">任务审核结果、截止提醒与兑换动态</p>
      </div>
      <NotificationInbox key={filter} filter={filter} />
    </>
  );
}
