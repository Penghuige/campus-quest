/**
 * Task 5 (Plan 09): growth field mapping (spec §19) — month points /
 * rank, total earned, completed + on-time ratio, streak (consecutive
 * on-time COMPLETIONS), best historical monthly rank, and the honors
 * rows (§18 type wording, period passthrough, granted-date formatting
 * in the business timezone).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import type { GrowthDto, OwnedHonorDto } from "../features/rankings/api";
import {
  formatRatioPercent,
  growthSummaryView,
  growthView,
  honorRowView,
  honorTypeLabel,
  HONOR_TYPE_LABELS,
} from "../features/rankings/growthView";

function honor(overrides: Partial<OwnedHonorDto>): OwnedHonorDto {
  return {
    honor_id: "66666666-6666-4666-8666-666666666666",
    name: "月度卷王",
    honor_type: "MONTHLY_RANK",
    period: "2026-09",
    granted_at: "2026-09-30T16:00:00Z",
    ...overrides,
  };
}

function growth(overrides: Partial<GrowthDto>): GrowthDto {
  return {
    month_points: 120,
    month_rank: 3,
    total_earned_points: 420,
    completed_count: 20,
    on_time_count: 18,
    on_time_ratio: 0.9,
    current_streak: 4,
    best_month_rank: 2,
    honors: [honor({})],
    ...overrides,
  };
}

describe("growth summary mapping", () => {
  test("every §19 figure maps 1:1 onto display text", () => {
    const view = growthSummaryView(growth({}));
    assert.equal(view.monthPoints, 120);
    assert.equal(view.monthRankText, "第 3 名");
    assert.equal(view.totalEarnedPoints, 420);
    assert.equal(view.completedCount, 20);
    assert.equal(view.onTimeText, "18 / 20 次（90%）");
    assert.equal(view.onTimeRatioPercent, 90);
    assert.equal(view.streakText, "连续按时 4 次");
    assert.equal(view.bestRankText, "第 2 名");
  });

  test("null ranks read as 未上榜 / 暂无 (the server's no-score verdict)", () => {
    const view = growthSummaryView(growth({ month_rank: null, best_month_rank: null }));
    assert.equal(view.monthRankText, "未上榜");
    assert.equal(view.bestRankText, null);
  });

  test("no completions yet: no ratio figure instead of a fake 0%", () => {
    const view = growthSummaryView(
      growth({
        completed_count: 0,
        on_time_count: 0,
        on_time_ratio: 0.0,
        current_streak: 0,
      }),
    );
    assert.equal(view.onTimeRatioPercent, null);
    assert.equal(view.onTimeText, "0 / 0 次");
    assert.equal(view.streakText, "连续按时 0 次");
  });

  test("ratio percent keeps one decimal when the float needs it", () => {
    assert.equal(formatRatioPercent(0.875), "87.5%");
    assert.equal(formatRatioPercent(0.9), "90%");
    assert.equal(formatRatioPercent(1), "100%");
  });
});

describe("honors rows (§18)", () => {
  test("the frozen auto types + commemorative map to zh-CN wording", () => {
    assert.equal(honorTypeLabel("TOTAL_COMPLETED"), "累计完成");
    assert.equal(honorTypeLabel("ON_TIME_STREAK"), "按时连续");
    assert.equal(honorTypeLabel("DAILY_RANK"), "日榜");
    assert.equal(honorTypeLabel("MONTHLY_RANK"), "月榜");
    assert.equal(honorTypeLabel("TOTAL_EARNED_POINTS"), "累计积分");
    assert.equal(honorTypeLabel("COMMEMORATIVE"), "纪念");
    // Unknown types degrade to wording, never the raw enum (design §9).
    assert.equal(honorTypeLabel("SOMETHING_NEW"), "荣誉");
    assert.equal(Object.keys(HONOR_TYPE_LABELS).length, 6);
  });

  test("a honor row carries name/type/period + a business-tz date", () => {
    const row = honorRowView(honor({}));
    assert.equal(row.name, "月度卷王");
    assert.equal(row.typeLabel, "月榜");
    assert.equal(row.period, "2026-09");
    // 2026-09-30T16:00:00Z is 2026-10-01 in Asia/Shanghai — the date
    // must come from the timezone database, never a +8 constant.
    assert.equal(row.grantedLabel, "2026年10月1日");
  });

  test("periodic-less honors pass a null period through", () => {
    const row = honorRowView(honor({ honor_type: "TOTAL_COMPLETED", period: null }));
    assert.equal(row.period, null);
  });

  test("growthView composes summary + honors", () => {
    const view = growthView(growth({ honors: [] }));
    assert.equal(view.honors.length, 0);
    assert.equal(view.summary.monthRankText, "第 3 名");
  });
});
