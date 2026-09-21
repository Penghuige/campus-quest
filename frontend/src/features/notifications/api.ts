/**
 * Typed wrappers for the notifications endpoints (spec §25/§28; backend
 * `app/modules/notifications/router.py`, Stream S3).
 *
 * HAND-WRITTEN CONTRACT, NOT GENERATED: `openapi.snapshot.json` on this
 * branch predates the notifications module (it ends at the S1/S2
 * surface), so these DTOs are transcribed field-by-field from the S3
 * router's Pydantic models. MERGE-TIME STEP: when the S3 stream merges,
 * point `npm run api:types` at the combined backend and replace these
 * local shapes with `components["schemas"]["NotificationItemResponse"]`
 * etc. (`git grep NotificationItemResponse src/features/notifications`
 * finds every seam). The unit tests in `__tests__/notifications-api.test.ts`
 * pin the wire paths/methods against the S3 router so drift is caught at
 * that boundary.
 *
 * PRIVACY BY CONSTRUCTION (router docstring): the inbox DTO carries the
 * caller's OWN rows only — id/event_type/title/body/read_at/created_at.
 * Provider internals, delivery-channel state, and other users' rows are
 * absent from the wire shape; nothing here derives them either. There is
 * no mark-all endpoint in V1 (mark-read is per item), so no such wrapper
 * exists.
 */
import { apiRequest } from "@/lib/api";

/**
 * `NotificationItemResponse` — one inbox message: the logical
 * Notification's own fields only.
 */
export interface NotificationItemDto {
  id: string;
  /** One of the frozen `NotificationEventType` values (see inboxView). */
  event_type: string;
  title: string;
  body: string;
  /** null exactly while unread (the mark-read endpoint fills it). */
  read_at: string | null;
  created_at: string;
}

/** `NotificationInboxResponse` — one offset page, newest first. */
export interface NotificationInboxDto {
  items: NotificationItemDto[];
  total: number;
  limit: number;
  offset: number;
}

/** Backend default page size (router `DEFAULT_PAGE_LIMIT`). */
export const NOTIFICATION_PAGE_LIMIT = 20;

export interface NotificationListQuery {
  /** true -> `?unread=true` (read_at IS NULL only). */
  unread?: boolean;
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}

/**
 * The caller's own inbox page, newest first
 * (GET /api/v1/notifications?unread=&limit=&offset=).
 */
export function listNotifications(
  query: NotificationListQuery = {},
): Promise<NotificationInboxDto> {
  const params = new URLSearchParams();
  if (query.unread === true) {
    params.set("unread", "true");
  }
  if (query.limit !== undefined) {
    params.set("limit", String(query.limit));
  }
  if (query.offset !== undefined) {
    params.set("offset", String(query.offset));
  }
  const encoded = params.toString();
  return apiRequest<NotificationInboxDto>(
    `/api/v1/notifications${encoded.length > 0 ? `?${encoded}` : ""}`,
    { signal: query.signal },
  );
}

/**
 * The caller's unread count for the bell: the inbox endpoint has no
 * dedicated count field, so this polls the UNREAD first page (limit 1)
 * and takes its `total` — the server's own count of read_at IS NULL rows
 * for this user, never a client-side derivation.
 */
export function fetchUnreadCount(signal?: AbortSignal): Promise<number> {
  return listNotifications({ unread: true, limit: 1, signal }).then(
    (page) => page.total,
  );
}

/**
 * Mark one OWN notification read (POST, idempotent — a re-read keeps the
 * FIRST read_at; a foreign id answers PERMISSION_DENIED, a missing id
 * NOT_FOUND). Returns the message with the authoritative read_at.
 */
export function markNotificationRead(
  notificationId: string,
): Promise<NotificationItemDto> {
  return apiRequest<NotificationItemDto>(
    `/api/v1/notifications/${encodeURIComponent(notificationId)}/read`,
    { method: "POST" },
  );
}
