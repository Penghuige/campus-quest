/**
 * Task 7 (Plan 09): pure inbox/bell view mapping — the frozen event-type
 * map (distinct glyph + product label per type, graceful unknown
 * fallback), the URL filter parser, the row mapping's shape and read
 * states, offset pagination, and the badge/label derivations the bell
 * renders. Everything here is pure: same input in, same view out.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import type { NotificationItemDto } from "../features/notifications/api";
import {
  bellLabel,
  canLoadMore,
  inboxItemView,
  parseInboxFilter,
  notificationEventView,
  UNKNOWN_EVENT_VIEW,
  unreadBadgeText,
} from "../features/notifications/inboxView";

// --- the event-type map ------------------------------------------------------------

/** The frozen backend `NotificationEventType` values (enums.py, V1: 8). */
const FROZEN_EVENT_TYPES = [
  "ASSIGNMENT_DEADLINE_24H",
  "ASSIGNMENT_DEADLINE_4H",
  "REVISION_REQUIRED",
  "SUBMISSION_APPROVED",
  "SUBMISSION_VALIDATION_FAILED",
  "REWARD_REDEMPTION_APPROVED",
  "REWARD_REDEMPTION_REJECTED",
  "ACCOUNT_SECURITY",
] as const;

describe("event-type map (distinct icons + product wording)", () => {
  test("every frozen backend type maps to a label + a DISTINCT glyph", () => {
    const glyphs = new Set<string>();
    for (const eventType of FROZEN_EVENT_TYPES) {
      const view = notificationEventView(eventType);
      assert.notEqual(view, UNKNOWN_EVENT_VIEW, `${eventType} must be mapped`);
      assert.ok(view.label.length > 0, `${eventType} needs a label`);
      // Product wording, never the raw enum (design §9 status badges).
      assert.notEqual(view.label, eventType);
      glyphs.add(view.glyph);
    }
    assert.equal(glyphs.size, FROZEN_EVENT_TYPES.length, "glyphs must be distinct");
  });

  test("tones come from the semantic set (color is supplemental only)", () => {
    for (const eventType of FROZEN_EVENT_TYPES) {
      assert.ok(
        ["info", "success", "warning", "danger"].includes(
          notificationEventView(eventType).tone,
        ),
      );
    }
  });

  test("an unknown future type degrades to the generic entry, never a throw", () => {
    const view = notificationEventView("SOME_FUTURE_EVENT");
    assert.equal(view, UNKNOWN_EVENT_VIEW);
    assert.equal(view.label, "通知");
  });
});

// --- the URL filter ----------------------------------------------------------------

describe("parseInboxFilter (?filter= URL state)", () => {
  test("unread passes through; all/undefined/garbage stay on 全部", () => {
    assert.equal(parseInboxFilter("unread"), "unread");
    assert.equal(parseInboxFilter("all"), "all");
    assert.equal(parseInboxFilter(undefined), "all");
    assert.equal(parseInboxFilter("hot"), "all");
    assert.equal(parseInboxFilter(""), "all");
  });
});

// --- rows ----------------------------------------------------------------------------

function item(overrides: Partial<NotificationItemDto> = {}): NotificationItemDto {
  return {
    id: "n-1",
    event_type: "SUBMISSION_APPROVED",
    title: "任务审核通过",
    body: "恭喜，你的提交已通过审核。",
    read_at: null,
    created_at: "2026-09-21T08:00:00Z",
    ...overrides,
  };
}

describe("inboxItemView (the row choke point)", () => {
  test("unread row: server order, type mapping, parsed time, isRead false", () => {
    const view = inboxItemView(item(), (iso) => Date.parse(iso));
    assert.equal(view.isRead, false);
    assert.equal(view.typeLabel, "审核通过");
    assert.equal(view.glyph, "✅");
    assert.equal(view.tone, "success");
    assert.equal(view.createdAtMs, Date.parse("2026-09-21T08:00:00Z"));
  });

  test("read row: read_at != null is the one read verdict", () => {
    const view = inboxItemView(
      item({ read_at: "2026-09-21T09:00:00Z" }),
      (iso) => Date.parse(iso),
    );
    assert.equal(view.isRead, true);
  });

  test("the row shape derives NOTHING beyond the public inbox fields", () => {
    const view = inboxItemView(item(), (iso) => Date.parse(iso));
    assert.deepEqual(Object.keys(view).sort(), [
      "body",
      "createdAtMs",
      "eventType",
      "glyph",
      "id",
      "isRead",
      "title",
      "tone",
      "typeLabel",
    ]);
    // No provider/delivery/identity fields exist to leak (router DTO pin).
    const serialized = JSON.stringify(view);
    for (const absent of ["user_id", "channel", "attempts", "last_error", "phone"]) {
      assert.ok(!serialized.includes(absent), `row must not carry ${absent}`);
    }
  });

  test("server order is preserved verbatim (the view never re-sorts)", () => {
    const newest = item({ id: "newest", created_at: "2026-09-21T12:00:00Z" });
    const older = item({ id: "older", created_at: "2026-09-20T12:00:00Z" });
    const rows = [newest, older].map((row) => inboxItemView(row, (iso) => Date.parse(iso)));
    assert.deepEqual(
      rows.map((row) => row.id),
      ["newest", "older"],
    );
  });
});

// --- pagination -----------------------------------------------------------------------

describe("canLoadMore (offset accumulation)", () => {
  test("more pages exist exactly while fewer rows are loaded than total", () => {
    assert.equal(canLoadMore(0, 0), false);
    assert.equal(canLoadMore(20, 20), false);
    assert.equal(canLoadMore(20, 41), true);
  });
});

// --- badge + label ---------------------------------------------------------------------

describe("unreadBadgeText + bellLabel", () => {
  test("zero hides the badge; 1-99 render digits; 100+ collapses to 99+", () => {
    assert.equal(unreadBadgeText(0), null);
    assert.equal(unreadBadgeText(1), "1");
    assert.equal(unreadBadgeText(9), "9");
    assert.equal(unreadBadgeText(99), "99");
    assert.equal(unreadBadgeText(100), "99+");
    assert.equal(unreadBadgeText(1000), "99+");
  });

  test("the bell's accessible name carries the count exactly when there is one", () => {
    assert.equal(bellLabel(null), "未读通知");
    assert.equal(bellLabel(0), "未读通知");
    assert.equal(bellLabel(3), "未读通知（3 条）");
  });
});
