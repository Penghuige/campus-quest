"use client";
/**
 * Notification inbox island (spec §28; patterns §4/§7/§8): offset pages
 * newest-first from the server, a URL-ridden unread filter (the page
 * remounts this island per filter — every fetch keys off the tab), and
 * owner-only mark-read.
 *
 * Mark-read is one of the two sanctioned OPTIMISTIC mutations (patterns
 * §7: read/unread notification): the row flips (or, on the unread tab,
 * leaves the filtered list) immediately, the POST's echo is authoritative
 * on success, and a failure REVERTS the row, the unread count, and shows
 * the code-mapped error beside that row. Ownership is the server's
 * verdict (a foreign id answers PERMISSION_DENIED; the shell's session
 * gate already turned 401 into the login CTA) — the client renders the
 * typed failure, it never decides access itself.
 *
 * No mark-all in V1: the backend exposes per-item mark-read only, and
 * fanning out N POSTs client-side is neither cheap nor atomic, so the UI
 * offers no 全部已读 action.
 *
 * The unread badge (this header) and the bell run INDEPENDENT fetches of
 * the same server truth (unread first page total) — no shared client
 * cache to invalidate; the bell's focus/interval revalidation reconciles
 * it within its fresh window.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import type { SectionErrorView } from "@/lib/errors";
import { describeSectionError } from "@/lib/errors";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import {
  fetchUnreadCount,
  listNotifications,
  markNotificationRead,
  NOTIFICATION_PAGE_LIMIT,
  type NotificationItemDto,
} from "./api";
import {
  canLoadMore,
  inboxItemView,
  INBOX_FILTERS,
  type InboxFilterKey,
} from "./inboxView";

export interface NotificationInboxProps {
  /** The URL filter the page parsed (remounts per tab; patterns §4). */
  filter: InboxFilterKey;
}

export function NotificationInbox({ filter }: NotificationInboxProps) {
  const [items, setItems] = useState<NotificationItemDto[]>([]);
  const [total, setTotal] = useState(0);
  const [unreadTotal, setUnreadTotal] = useState<number | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  // FOLD (T7 review): a failed load-more used to vanish silently; the
  // inline message + retry below keep the failure observable.
  const [moreError, setMoreError] = useState<unknown>(null);
  const [markErrors, setMarkErrors] = useState<Record<string, SectionErrorView>>(
    {},
  );
  const [reloadSeed, setReloadSeed] = useState(0);

  useEffect(() => {
    // Lint-driven state shape (the CommentThread note): the effect only
    // STARTS the fetches; every setState lands in async callbacks. Page
    // load and the unread total are INDEPENDENT -> parallel, no waterfall
    // (patterns §5). The badge fetch failing is non-fatal (null = hidden).
    let cancelled = false;
    listNotifications({
      unread: filter === "unread",
      limit: NOTIFICATION_PAGE_LIMIT,
      offset: 0,
    }).then(
      (page) => {
        if (!cancelled) {
          setItems(page.items);
          setTotal(page.total);
          setPhase("ready");
        }
      },
      (cause: unknown) => {
        if (!cancelled) {
          setError(cause);
          setPhase("error");
        }
      },
    );
    fetchUnreadCount().then(
      (count) => {
        if (!cancelled) {
          setUnreadTotal(count);
        }
      },
      () => {},
    );
    return () => {
      cancelled = true;
    };
  }, [filter, reloadSeed]);

  const retry = useCallback(() => {
    setPhase("loading");
    setReloadSeed((seed) => seed + 1);
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listNotifications({
        unread: filter === "unread",
        limit: NOTIFICATION_PAGE_LIMIT,
        offset: items.length,
      });
      // Offset pages can overlap under concurrent writes; merge by id.
      const seen = new Set(items.map((item) => item.id));
      const fresh = page.items.filter((item) => !seen.has(item.id));
      setItems([...items, ...fresh]);
      setTotal(page.total);
    } catch (cause) {
      // Non-fatal (the loaded rows stay usable), but no longer SILENT:
      // the mapped inline message + retry render below the button (T8 fold).
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [filter, items, loadingMore]);

  const markRead = useCallback(
    async (item: NotificationItemDto) => {
      const wasUnread = item.read_at === null;
      const index = items.findIndex((row) => row.id === item.id);
      // Optimistic (patterns §7): flip the row, tick the counts down.
      setMarkErrors((previous) => {
        const next = { ...previous };
        delete next[item.id];
        return next;
      });
      setUnreadTotal((previous) =>
        previous === null || !wasUnread ? previous : Math.max(previous - 1, 0),
      );
      if (filter === "unread") {
        // A read row no longer matches the filter: it leaves the list.
        setItems((previous) => previous.filter((row) => row.id !== item.id));
        setTotal((previous) => Math.max(previous - 1, 0));
      } else {
        setItems((previous) =>
          previous.map((row) =>
            row.id === item.id
              ? { ...row, read_at: new Date().toISOString() }
              : row,
          ),
        );
      }
      try {
        const echo = await markNotificationRead(item.id);
        if (filter !== "unread") {
          // The echo carries the FIRST read_at (idempotent server).
          setItems((previous) =>
            previous.map((row) => (row.id === echo.id ? echo : row)),
          );
        }
      } catch (cause: unknown) {
        // Failure: restore the exact pre-click state and say why.
        setUnreadTotal((previous) =>
          previous === null || !wasUnread ? previous : previous + 1,
        );
        if (filter === "unread") {
          setItems((previous) => {
            const restored = [...previous];
            restored.splice(Math.min(index, restored.length), 0, item);
            return restored;
          });
          setTotal((previous) => previous + 1);
        } else {
          setItems((previous) =>
            previous.map((row) => (row.id === item.id ? item : row)),
          );
        }
        setMarkErrors((previous) => ({
          ...previous,
          [item.id]: describeSectionError(cause),
        }));
      }
    },
    [filter, items],
  );

  const rows =
    phase === "ready"
      ? items.map((item) => ({
          item,
          view: inboxItemView(item, parseServerInstant),
        }))
      : [];

  return (
    <section className="section" aria-label="通知收件箱">
      <div className="section-head">
        <h2 className="section-title">消息</h2>
        {unreadTotal !== null && unreadTotal > 0 ? (
          <span className="badge badge-info">
            未读 <span className="meta-num">{unreadTotal}</span> 条
          </span>
        ) : null}
      </div>

      <nav className="tab-bar" aria-label="通知筛选">
        {INBOX_FILTERS.map((tab) => (
          <Link
            key={tab.key}
            className="tab-link"
            href={tab.key === "all" ? "/notifications" : "/notifications?filter=unread"}
            aria-current={tab.key === filter ? "page" : undefined}
          >
            {tab.label}
          </Link>
        ))}
      </nav>

      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError error={error} onRetry={retry} retryLabel="重新加载" />
      ) : (
        <>
          {rows.length === 0 ? (
            filter === "unread" ? (
              <EmptyState
                title="没有未读通知"
                hint="所有消息都已读过了"
              >
                <Link className="link" href="/notifications">
                  查看全部通知
                </Link>
              </EmptyState>
            ) : (
              <EmptyState
                title="暂无通知"
                hint="任务审核结果、截止提醒和兑换动态会出现在这里"
              />
            )
          ) : (
            <ol className="notif-list">
              {rows.map(({ item, view }) => (
                <NotificationRow
                  key={view.id}
                  view={view}
                  item={item}
                  markError={markErrors[view.id] ?? null}
                  onMarkRead={markRead}
                />
              ))}
            </ol>
          )}

          {canLoadMore(items.length, total) ? (
            <div className="load-more">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                aria-busy={loadingMore}
              >
                {loadingMore ? (
                  <span className="spinner" aria-hidden="true" />
                ) : null}
                <span>
                  加载更多通知（{items.length}/{total}）
                </span>
              </button>
              {moreError !== null ? (
                <SectionError error={moreError} onRetry={() => void loadMore()} />
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

// --- one inbox row ------------------------------------------------------------------

interface NotificationRowProps {
  view: ReturnType<typeof inboxItemView>;
  item: NotificationItemDto;
  markError: SectionErrorView | null;
  onMarkRead: (item: NotificationItemDto) => void;
}

/**
 * One inbox message: type label + distinct glyph (design §9 — text
 * always, tone supplemental), the server's read verdict as a 未读 badge
 * (a non-color cue; §12), absolute time in the business timezone
 * (patterns §14), and the owner-only 标为已读 action while unread.
 */
function NotificationRow({ view, item, markError, onMarkRead }: NotificationRowProps) {
  return (
    <li className="notif-item" data-read={view.isRead ? "true" : "false"}>
      <span className="notif-icon" data-tone={view.tone} aria-hidden="true">
        {view.glyph}
      </span>
      <div className="notif-main">
        <div className="notif-item-head">
          <span className="notif-type">{view.typeLabel}</span>
          {!view.isRead ? (
            <span className="badge notif-unread-badge">未读</span>
          ) : null}
          <time
            className="notif-time"
            dateTime={new Date(view.createdAtMs).toISOString()}
          >
            {formatDeadlineDateTime(view.createdAtMs)}
          </time>
        </div>
        <p className="notif-title">{view.title}</p>
        <p className="notif-body">{view.body}</p>
        {!view.isRead ? (
          <div className="notif-actions">
            <button
              type="button"
              className="btn btn-ghost comment-action"
              onClick={() => onMarkRead(item)}
            >
              标为已读
            </button>
          </div>
        ) : null}
        {markError !== null ? (
          <p className="notif-item-error" role="alert">
            <span className="alert-marker" aria-hidden="true">!</span>
            未读状态更新失败：{markError.message}
            {markError.requestId !== null
              ? `（请求 ID：${markError.requestId}）`
              : ""}
          </p>
        ) : null}
      </div>
    </li>
  );
}
