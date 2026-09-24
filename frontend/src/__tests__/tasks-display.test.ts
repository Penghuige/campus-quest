/**
 * Task 3 (Plan 09): task-card display mapping — the pure derivations
 * TaskCard/TaskDetailView render (spec §42 card fields; design-system §4
 * rarity accents, §9 status wording; §42 overdue copy).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, test } from "node:test";

import {
  availabilityText,
  claimRewardLine,
  claimStepView,
  claimStatusView,
  deadlineView,
  isActiveClaim,
  isRevisionClaim,
  NEAR_CUTOFF_MS,
  rarityView,
  ratingText,
} from "../features/tasks/display";

const NOW = Date.parse("2026-09-21T08:00:00Z");
const iso = (offsetMs: number) =>
  new Date(NOW + offsetMs).toISOString().replace(".000Z", "Z");

describe("rarity view (token-driven accents)", () => {
  test("every backend rarity maps to its accent key and zh label", () => {
    assert.deepEqual(rarityView("NORMAL"), { rarity: "NORMAL", label: "普通" });
    assert.deepEqual(rarityView("RARE"), { rarity: "RARE", label: "稀有" });
    assert.deepEqual(rarityView("EPIC"), { rarity: "EPIC", label: "史诗" });
    assert.deepEqual(rarityView("LEGENDARY"), {
      rarity: "LEGENDARY",
      label: "传说",
    });
  });

  test("the accent keys are exactly the CSS data-rarity token variants", () => {
    // Pins the contract with globals.css: every key produced here must have
    // a rarity-badge[data-rarity=...] rule backed by a --rarity-* token.
    const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");
    for (const key of ["NORMAL", "RARE", "EPIC", "LEGENDARY"] as const) {
      assert.match(css, new RegExp(`\\.rarity-badge\\[data-rarity="${key}"\\]`));
      assert.match(css, new RegExp(`--rarity-${key.toLowerCase()}:`));
    }
  });

  test("unknown rarity degrades to the NORMAL accent (contract drift)", () => {
    assert.deepEqual(rarityView("MYTHIC"), { rarity: "NORMAL", label: "普通" });
    assert.deepEqual(rarityView(""), { rarity: "NORMAL", label: "普通" });
  });
});

describe("rating and availability text", () => {
  test("aggregate rating text with one-decimal average", () => {
    assert.equal(
      ratingText({ average: 4.623, count: 12 }),
      "评分 4.6 · 12 条评价",
    );
  });

  test("no ratings yet (community module absent) reads 暂无评分", () => {
    assert.equal(ratingText(null), "暂无评分");
    assert.equal(ratingText({ average: 0, count: 0 }), "暂无评分");
  });

  test("availability is a COUNT, zero reads as depleted — never a list", () => {
    assert.equal(availabilityText(7), "可领取 7 个");
    assert.equal(availabilityText(0), "已被领完");
  });
});

describe("deadline view (display-only, §42)", () => {
  test("FIXED far from cutoff: absolute + remaining, no urgency", () => {
    const view = deadlineView(
      {
        deadline_mode: "FIXED",
        fixed_deadline_at: iso(48 * 60 * 60 * 1000),
        duration_minutes: null,
      },
      NOW,
    );
    assert.equal(view.modeLabel, "固定截止");
    assert.equal(view.urgency, "none");
    assert.match(view.line, /还剩/);
  });

  test("FIXED near cutoff (within the 4h default window) flags near", () => {
    const view = deadlineView(
      {
        deadline_mode: "FIXED",
        fixed_deadline_at: iso(NEAR_CUTOFF_MS - 1000),
        duration_minutes: null,
      },
      NOW,
    );
    assert.equal(view.urgency, "near");
    // ...and exactly at the threshold it is still near (inclusive edge).
    assert.equal(
      deadlineView(
        {
          deadline_mode: "FIXED",
          fixed_deadline_at: iso(NEAR_CUTOFF_MS),
          duration_minutes: null,
        },
        NOW,
      ).urgency,
      "near",
    );
  });

  test("FIXED past the deadline flags closed", () => {
    const view = deadlineView(
      {
        deadline_mode: "FIXED",
        fixed_deadline_at: iso(-60 * 1000),
        duration_minutes: null,
      },
      NOW,
    );
    assert.equal(view.urgency, "closed");
    assert.match(view.line, /已截止/);
  });

  test("RELATIVE: countdown only starts at claim time, no urgency hint", () => {
    const view = deadlineView(
      { deadline_mode: "RELATIVE", fixed_deadline_at: null, duration_minutes: 240 },
      NOW,
    );
    assert.equal(view.modeLabel, "领取后计时");
    assert.equal(view.line, "领取后 240 分钟内提交");
    assert.equal(view.urgency, "none");
  });

  test("FIXED without a timestamp stays neutral (no server instant yet)", () => {
    const view = deadlineView(
      { deadline_mode: "FIXED", fixed_deadline_at: null, duration_minutes: null },
      NOW,
    );
    assert.equal(view.urgency, "none");
    assert.equal(view.line, "截止时间待定");
  });
});

describe("claim status wording (design §9: product words, no raw enums)", () => {
  test("every backend status maps to product wording + tone", () => {
    const expected: Record<string, [string, string]> = {
      CLAIMED: ["待提交", "info"],
      VALIDATING: ["校验中", "info"],
      UNDER_REVIEW: ["待审核", "info"],
      REVISION_REQUIRED: ["需修改", "warning"],
      COMPLETED: ["已完成", "success"],
      ABANDONED: ["已放弃", "muted"],
      EXPIRED: ["已过期", "muted"],
    };
    for (const [status, [label, tone]] of Object.entries(expected)) {
      assert.deepEqual(claimStatusView(status), { label, tone });
    }
  });

  test("unknown status degrades to a neutral label, never the raw enum", () => {
    const view = claimStatusView("SOMETHING_NEW");
    assert.notEqual(view.label, "SOMETHING_NEW");
    assert.equal(view.tone, "info");
  });

  test("dashboard grouping: active vs revision slots", () => {
    assert.equal(isActiveClaim("CLAIMED"), true);
    assert.equal(isActiveClaim("UNDER_REVIEW"), true);
    assert.equal(isActiveClaim("REVISION_REQUIRED"), false);
    assert.equal(isActiveClaim("COMPLETED"), false);
    assert.equal(isRevisionClaim("REVISION_REQUIRED"), true);
    assert.equal(isRevisionClaim("CLAIMED"), false);
  });
});

describe("claim reward line (§42 non-punitive overdue copy)", () => {
  const base = 160;
  const deadline = NOW + 60 * 60 * 1000;
  const grace = deadline + 24 * 60 * 60 * 1000;

  test("before the deadline: the design-§14 preferred reward copy", () => {
    assert.equal(claimRewardLine(base, deadline, grace, NOW), "当前可获得 160 积分");
  });

  test("overdue within grace: non-punitive copy WITHOUT an invented number", () => {
    const line = claimRewardLine(base, deadline, grace, deadline + 1000);
    assert.match(line, /当前仍可获得积分/);
    assert.doesNotMatch(line, /扣除|扣了|被扣/);
    // The decayed figure is a server settlement (patterns §3) — the client
    // must not fabricate one (e.g. 80% of the snapshot).
    assert.doesNotMatch(line, /128/);
  });

  test("at/after grace: window closed", () => {
    assert.equal(claimRewardLine(base, deadline, grace, grace), "提交窗口已关闭");
  });
});

// --- claimStepView (Plan 11 §9 progress strip) ---------------------------------

describe("claimStepView (the five-step progress strip)", () => {
  test("every in-flight status maps to exactly one current step", () => {
    // COMPLETED is terminal (its own test below): all done, no current.
    const cases: Array<[string, number]> = [
      ["CLAIMED", 2],
      ["VALIDATING", 3],
      ["UNDER_REVIEW", 4],
      ["REVISION_REQUIRED", 2],
    ];
    for (const [status, currentIndex] of cases) {
      const view = claimStepView(status);
      assert.equal(view.linear, true, status);
      assert.equal(view.steps.length, 5, status);
      assert.equal(
        view.steps.filter((step) => step.state === "current").length,
        1,
        `${status}: exactly one current step`,
      );
      assert.equal(
        view.steps[currentIndex - 1].state,
        "current",
        `${status}: step ${currentIndex} is current`,
      );
      // Everything before current is done; everything after is future.
      view.steps.forEach((step, index) => {
        const expected = index < currentIndex - 1 ? "done" : index === currentIndex - 1 ? "current" : "future";
        assert.equal(step.state, expected, `${status}: step ${index + 1}`);
      });
    }
  });

  test("COMPLETED is all-done with no current step", () => {
    const view = claimStepView("COMPLETED");
    assert.equal(view.steps.every((step) => step.state === "done"), true);
  });

  test("non-linear terminal states render no strip", () => {
    for (const status of ["ABANDONED", "EXPIRED", "SOMETHING_NEW"]) {
      const view = claimStepView(status);
      assert.equal(view.linear, false, status);
      assert.equal(view.steps.length, 0, status);
    }
  });

  test("step labels ride in the fixed order", () => {
    assert.deepEqual(
      claimStepView("CLAIMED").steps.map((step) => step.label),
      ["领取", "提交", "校验", "审核", "完成"],
    );
  });
});
