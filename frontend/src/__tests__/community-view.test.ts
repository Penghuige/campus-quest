/**
 * Task 6 (Plan 09): community pure views — the identity preview (spec
 * §21.4 explicit anonymous choice), the XSS-as-text pin (§33.1), the
 * composer content mirror (§21.1), two-level thread grouping with
 * tombstones (§21.2/§21.3), report validation (§23), the typed rating
 * eligibility copy (§20/§29), and the vote toggle value shape (§22).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  DEFAULT_IDENTITY_MODE,
  identityPreview,
  isIdentityMode,
  codePointLength,
  normalizeCommentDraft,
  commentExcerpt,
  commentRowView,
  commentThreadView,
  parseCommentSort,
  reportDraftView,
  nextVoteValue,
  describeCommunityError,
  RATING_NOT_ELIGIBLE_COPY,
  TOMBSTONE_TEXT,
} from "../features/community/communityView";
import type { CommentDto } from "../features/community/api";
import { ApiError } from "../lib/errors";

function comment(overrides: Partial<CommentDto> = {}): CommentDto {
  return {
    id: "c1",
    task_id: "t1",
    parent_id: null,
    content: "内容",
    is_anonymous: false,
    author_display: "同学甲",
    created_at: "2026-09-20T10:00:00Z",
    updated_at: "2026-09-20T10:00:00Z",
    edited: false,
    deleted: false,
    ...overrides,
  };
}

// --- identity choice (§21.4: explicit before posting) -------------------------------

describe("identity preview (anonymous must be explicit)", () => {
  test("default mode is 公开昵称 (the documented V1 ruling)", () => {
    assert.equal(DEFAULT_IDENTITY_MODE, "named");
    assert.equal(isIdentityMode("named"), true);
    assert.equal(isIdentityMode("anonymous"), true);
    assert.equal(isIdentityMode("pseudonymous"), false);
  });

  test("named preview names the actual nickname being published", () => {
    const preview = identityPreview("named", "小鱼干");
    assert.ok(preview.includes("「小鱼干」"));
    assert.ok(preview.includes("公开昵称"));
    assert.ok(preview.includes("可见"));
  });

  test("named preview degrades when the nickname is not loaded yet", () => {
    assert.equal(
      identityPreview("named", null),
      "将以公开昵称发布，所有同学可见",
    );
  });

  test("anonymous preview shows the 匿名用户 label and never the nickname", () => {
    const preview = identityPreview("anonymous", "小鱼干");
    assert.ok(preview.includes("匿名用户"));
    assert.ok(preview.includes("不显示你的昵称"));
    assert.equal(preview.includes("小鱼干"), false);
  });
});

// --- XSS pin (§33.1: user text is plain text) ----------------------------------------

describe("XSS payloads stay data (rendered as text)", () => {
  const PAYLOAD = '<script>alert("xss")</script><img src=x onerror=alert(1)>';

  test("row view carries the payload VERBATIM as a plain string field", () => {
    const row = commentRowView(comment({ content: PAYLOAD }));
    assert.equal(row.content, PAYLOAD);
    // No escaping/mangling happened — the component layer renders this
    // as a React text node (nothing in the view interprets markup).
  });

  test("row objects carry EXACTLY the public display fields", () => {
    const row = commentRowView(comment());
    assert.deepEqual(Object.keys(row), [
      "id",
      "authorDisplay",
      "isAnonymous",
      "content",
      "createdAtMs",
      "edited",
      "deleted",
    ]);
    // No task_id/parent_id/updated_at ride the display row, and the
    // DTO has no user-id/contact field to begin with.
  });

  test("serialized thread views contain no author identity beyond display fields", () => {
    const view = commentThreadView([
      comment({ is_anonymous: true, author_display: "匿名用户" }),
    ]);
    const serialized = JSON.stringify(view);
    assert.ok(serialized.includes("匿名用户"));
    // No phone/email/username-shaped keys anywhere in the view.
    assert.equal(/phone|email|username|student_no|user_id/.test(serialized), false);
  });
});

// --- composer content mirror (§21.1; backend normalize_comment_content) -------------

describe("composer content mirror", () => {
  test("trims and strips control characters except \\n and \\t", () => {
    const draft = normalizeCommentDraft("  \0A\rB\tC\nD\u001b  ", 2000);
    assert.ok(draft.ok);
    // NUL/ESC/\r disappear; \t and \n survive; the result is trimmed.
    assert.equal(draft.value, "AB\tC\nD");
  });

  test("whitespace-only content is rejected", () => {
    for (const blank of ["", "   ", " \n\t "]) {
      const draft = normalizeCommentDraft(blank, 2000);
      assert.equal(draft.ok, false);
      if (!draft.ok) {
        assert.equal(draft.reason, "empty");
      }
    }
  });

  test("the 2000 boundary is inclusive; 2001 rejects", () => {
    assert.ok(normalizeCommentDraft("a".repeat(2000), 2000).ok);
    const over = normalizeCommentDraft("a".repeat(2001), 2000);
    assert.equal(over.ok, false);
    if (!over.ok) {
      assert.equal(over.reason, "too-long");
      assert.equal(over.maxLength, 2000);
    }
  });

  test("the cap counts CODE POINTS (Python len), not UTF-16 units", () => {
    assert.ok(normalizeCommentDraft("🎉".repeat(2000), 2000).ok); // .length would be 4000
    const over = normalizeCommentDraft("🎉".repeat(2001), 2000);
    if (!over.ok) {
      assert.equal(over.reason, "too-long");
    }
    assert.equal(codePointLength("🎉🎉"), 2);
  });
});

// --- excerpts ------------------------------------------------------------------------

describe("reply-context excerpt", () => {
  test("tombstone content excerpts to the uniform marker", () => {
    assert.equal(commentExcerpt(null), TOMBSTONE_TEXT);
  });

  test("long content truncates with an ellipsis; short content stays verbatim", () => {
    assert.equal(commentExcerpt("短评", 60), "短评");
    const truncated = commentExcerpt("x".repeat(61), 60);
    assert.ok(truncated.endsWith("…"));
    assert.equal(Array.from(truncated).length, 61);
  });
});

// --- two-level thread grouping (§21.2) + tombstones (§21.3) --------------------------

describe("thread grouping: deep replies flatten into the root group", () => {
  function chain() {
    // A <- B <- C <- D (three levels deep under the root) + a second root E.
    return [
      comment({ id: "d", parent_id: "c", content: "D", created_at: "2026-09-20T10:03:00Z" }),
      comment({ id: "c", parent_id: "b", content: "C", created_at: "2026-09-20T10:02:00Z" }),
      comment({ id: "b", parent_id: "a", content: "B", created_at: "2026-09-20T10:01:00Z" }),
      comment({ id: "a", parent_id: null, content: "A", created_at: "2026-09-20T10:00:00Z" }),
      comment({ id: "e", parent_id: null, content: "E", created_at: "2026-09-20T09:00:00Z" }),
    ];
  }

  test("a three-deep chain renders ONE root + flat replies at the second level", () => {
    const view = commentThreadView(chain());
    assert.equal(view.groups.length, 2);
    const groupA = view.groups.find((group) => group.root.id === "a");
    assert.ok(groupA !== undefined);
    assert.equal(groupA.root.content, "A");
    // B, C, D ALL sit in the root's reply list — the view has no third
    // level for them to occupy (comment-depth-2 is the deepest render).
    assert.deepEqual(groupA.replies.map((row) => row.content), ["B", "C", "D"]);
  });

  test("replies read oldest-first inside the group; groups keep SERVER page order", () => {
    const view = commentThreadView(chain());
    // The flat page arrives newest-first (d, c, b, a, e); root groups
    // surface in that same server order — a before e — never re-sorted.
    assert.deepEqual(view.groups.map((group) => group.root.id), ["a", "e"]);
  });

  test("a reply whose parent is outside the loaded set surfaces as an orphan root", () => {
    const view = commentThreadView([
      comment({ id: "x", parent_id: "missing", content: "X" }),
    ]);
    assert.equal(view.groups.length, 1);
    assert.equal(view.groups[0].root.id, "x");
    assert.equal(view.groups[0].orphanRoot, true);
    assert.equal(view.orphanCount, 1);
  });
});

describe("tombstones keep surviving children (§21.3)", () => {
  test("a deleted parent renders a tombstone row and its replies survive", () => {
    const view = commentThreadView([
      comment({
        id: "root",
        deleted: true,
        content: null,
        author_display: TOMBSTONE_TEXT,
      }),
      comment({ id: "child", parent_id: "root", content: "孩子还在" }),
    ]);
    assert.equal(view.groups.length, 1);
    const group = view.groups[0];
    assert.equal(group.root.deleted, true);
    assert.equal(group.root.content, null);
    assert.equal(group.replies.length, 1);
    assert.equal(group.replies[0].content, "孩子还在");
  });

  test("the row view never exposes stored text for a deleted comment", () => {
    // Defensive: even a contract-drifted body with content present
    // renders the tombstone marker only.
    const row = commentRowView(comment({ deleted: true, content: "残留" }));
    assert.equal(row.content, null);
    assert.equal(TOMBSTONE_TEXT, "该评论已删除");
  });

  test("edited flag rides the row (§21.3 显示已编辑)", () => {
    assert.equal(commentRowView(comment({ edited: true })).edited, true);
  });
});

// --- sort tabs (§24 server sort) ------------------------------------------------------

describe("comment sort parsing", () => {
  test("latest/hot pass through; garbage degrades to latest", () => {
    assert.equal(parseCommentSort("latest"), "latest");
    assert.equal(parseCommentSort("hot"), "hot");
    assert.equal(parseCommentSort(undefined), "latest");
    assert.equal(parseCommentSort("top"), "latest");
    assert.equal(parseCommentSort(""), "latest");
  });
});

// --- report draft (§23) ---------------------------------------------------------------

describe("report form validation", () => {
  test("a category is required", () => {
    const draft = reportDraftView("", "", 500);
    assert.equal(draft.ok, false);
    if (!draft.ok) {
      assert.equal(draft.reason, "category-required");
    }
  });

  test("the category must be an exact closed-set member (lowercase rejects)", () => {
    const draft = reportDraftView("spam", "", 500);
    assert.equal(draft.ok, false);
    if (!draft.ok) {
      assert.equal(draft.reason, "category-invalid");
    }
    assert.ok(reportDraftView("SPAM", "", 500).ok);
  });

  test("note trims; blank means none; trimmed text over the cap rejects", () => {
    const ok = reportDraftView("PRIVACY", "  恶意骚扰  ", 500);
    assert.ok(ok.ok);
    if (ok.ok) {
      assert.equal(ok.note, "恶意骚扰");
    }
    const blank = reportDraftView("PRIVACY", "   ", 500);
    assert.ok(blank.ok);
    if (blank.ok) {
      assert.equal(blank.note, null);
    }
    const tooLong = reportDraftView("OTHER", "字".repeat(501), 500);
    assert.equal(tooLong.ok, false);
    if (!tooLong.ok) {
      assert.equal(tooLong.reason, "note-too-long");
    }
  });
});

// --- rating eligibility copy (§20/§29) + community error tiers ------------------------

describe("typed community error copy", () => {
  function apiError(code: string, message = "服务器消息", status = 403): ApiError {
    return new ApiError({ code, message, status });
  }

  test("RATING_NOT_ELIGIBLE maps to the completer-gate copy", () => {
    const view = describeCommunityError(apiError("RATING_NOT_ELIGIBLE"));
    assert.equal(view.message, RATING_NOT_ELIGIBLE_COPY);
    assert.equal(RATING_NOT_ELIGIBLE_COPY, "完成任务后才能评价该任务");
  });

  test("RATE_LIMITED and VALIDATION_ERROR carry stable copy, not server prose", () => {
    assert.equal(
      describeCommunityError(apiError("RATE_LIMITED", "whatever")).message,
      "操作过于频繁，请稍后再试",
    );
    assert.equal(
      describeCommunityError(apiError("VALIDATION_ERROR", "whatever")).message,
      "内容不符合要求，请检查后重试",
    );
  });

  test("system/unknown/network failures fall through the shared tiers", () => {
    const system = describeCommunityError(
      apiError("INTERNAL_ERROR", "boom", 500),
    );
    assert.ok(system.message.length > 0);
    assert.notEqual(system.message, "boom");
    const network = describeCommunityError(new TypeError("fetch failed"));
    assert.equal(network.message, "网络异常，请检查连接后重试");
  });
});

// --- vote value shape (§22: same direction again = remove) ---------------------------

describe("next vote value", () => {
  test("pressing the matching direction removes the vote", () => {
    assert.equal(nextVoteValue(1, 1), 0);
    assert.equal(nextVoteValue(-1, -1), 0);
  });

  test("pressing the other direction switches; no stance sets it", () => {
    assert.equal(nextVoteValue(-1, 1), 1);
    assert.equal(nextVoteValue(1, -1), -1);
    assert.equal(nextVoteValue(0, 1), 1);
    assert.equal(nextVoteValue(0, -1), -1);
  });
});
