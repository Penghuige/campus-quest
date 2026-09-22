/**
 * Pure inbox/bell views (spec §25/§28; patterns §3/§4).
 *
 * PRIVACY PIN: a rendered row carries EXACTLY the inbox DTO fields —
 * event type, title, body, read_at, created_at. The wire shape has no
 * provider/delivery/identity data (see api.ts), and this module derives
 * none. The unit tests pin the mapping and the serialized row shape.
 *
 * Event types mirror the FROZEN backend enum `NotificationEventType`
 * (backend `notifications/enums.py`; membership frozen by
 * docs/architecture/interfaces.md — eight V1 values). The wire field is a
 * plain string, and the backend may grow a ninth value before this
 * mirror catches up, so `notificationEventView` degrades unknown values
 * to a generic entry instead of throwing — an unmapped type must never
 * blank a row out of the inbox.
 *
 * Rows render in SERVER order (newest first — the router orders, the
 * view never re-sorts) and the read state is the server's `read_at`
 * verdict only. Per design §9/§12 the type label always renders as text;
 * the tone tint is supplemental, and every mapping pairs a DISTINCT glyph
 * with its label so icon alone stays distinguishable.
 */
import type { NotificationItemDto } from "@/features/notifications/api";

// --- event-type map (the frozen backend enum, mirrored for the UI) ------------------

/** Semantic tone (design §4) — a tint; the text label is the real signal. */
export type NotificationTone = "info" | "success" | "warning" | "danger";

export interface NotificationEventView {
  /** Product wording (never the raw enum string; design §9 status badges). */
  label: string;
  /** Distinct glyph per event type (aria-hidden; the label carries meaning). */
  glyph: string;
  tone: NotificationTone;
}

const EVENT_VIEWS: Readonly<Record<string, NotificationEventView>> = {
  ASSIGNMENT_DEADLINE_24H: {
    label: "截止提醒",
    glyph: "⏰",
    tone: "warning",
  },
  ASSIGNMENT_DEADLINE_4H: { label: "截止临近", glyph: "⏱", tone: "warning" },
  REVISION_REQUIRED: { label: "需修改", glyph: "✏️", tone: "warning" },
  SUBMISSION_APPROVED: { label: "审核通过", glyph: "✅", tone: "success" },
  SUBMISSION_VALIDATION_FAILED: {
    label: "校验未通过",
    glyph: "⚠️",
    tone: "danger",
  },
  REWARD_REDEMPTION_APPROVED: {
    label: "兑换成功",
    glyph: "🎁",
    tone: "success",
  },
  REWARD_REDEMPTION_REJECTED: {
    label: "兑换未通过",
    glyph: "📭",
    tone: "danger",
  },
  ACCOUNT_SECURITY: { label: "账号安全", glyph: "🔐", tone: "info" },
};

/** Degrade an unknown event type to a generic entry (never throws). */
export const UNKNOWN_EVENT_VIEW: NotificationEventView = {
  label: "通知",
  glyph: "🔔",
  tone: "info",
};

/** The view for one event_type: a known mapping or the generic fallback. */
export function notificationEventView(eventType: string): NotificationEventView {
  return EVENT_VIEWS[eventType] ?? UNKNOWN_EVENT_VIEW;
}

// --- filter tabs (patterns §4: the filter rides the URL) ---------------------------

export type InboxFilterKey = "all" | "unread";

export const INBOX_FILTERS: readonly { key: InboxFilterKey; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "unread", label: "未读" },
];

export const DEFAULT_INBOX_FILTER: InboxFilterKey = "all";

/** Parse a ?filter= value with the calm default; garbage cannot 500 a page. */
export function parseInboxFilter(
  value: string | undefined,
  fallback: InboxFilterKey = DEFAULT_INBOX_FILTER,
): InboxFilterKey {
  return value === "unread" || value === "all" ? value : fallback;
}

// --- rows ---------------------------------------------------------------------------

/** EXACTLY the public inbox fields (verbatim copy) + presentation bits. */
export interface InboxItemView {
  id: string;
  eventType: string;
  typeLabel: string;
  glyph: string;
  tone: NotificationTone;
  title: string;
  body: string;
  /** The server's read verdict (read_at !== null). */
  isRead: boolean;
  /** Epoch ms (created_at parsed once, here). */
  createdAtMs: number;
}

/**
 * The display row for one inbox message. This function is the single
 * choke point every rendered row goes through (the leaderboardView
 * precedent); `read_at` itself is NOT copied — `isRead` is the only read
 * signal a row needs, and mark-read replaces rows from the server echo.
 */
export function inboxItemView(
  item: NotificationItemDto,
  parseInstant: (iso: string) => number,
): InboxItemView {
  const event = notificationEventView(item.event_type);
  return {
    id: item.id,
    eventType: item.event_type,
    typeLabel: event.label,
    glyph: event.glyph,
    tone: event.tone,
    title: item.title,
    body: item.body,
    isRead: item.read_at !== null,
    createdAtMs: parseInstant(item.created_at),
  };
}

/** More pages exist on the server (offset accumulation view). */
export function canLoadMore(loadedCount: number, total: number): boolean {
  return loadedCount < total;
}

// --- unread badge (the bell + the inbox header) -------------------------------------

/**
 * Badge text for an unread count: null hides the badge (zero or unknown);
 * counts beyond two digits collapse to "99+" so the pill never widens
 * the topbar. Tabular-friendly plain digits.
 */
export function unreadBadgeText(count: number): string | null {
  if (count <= 0) {
    return null;
  }
  return count > 99 ? "99+" : String(count);
}

/** Accessible bell label: the destination plus the live unread count. */
export function bellLabel(count: number | null): string {
  return count !== null && count > 0
    ? `未读通知（${count} 条）`
    : "未读通知";
}
