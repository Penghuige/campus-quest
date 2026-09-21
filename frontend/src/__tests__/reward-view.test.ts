/**
 * Task 4: reward display — the SERVER-VALUE-ONLY pin (spec §11.2/§42;
 * patterns §3). Every number on screen is the claim DTO's snapshot
 * VERBATIM; the §9.3 decay ladder (100/80/50/20) is never computed
 * client-side, so no tier percentage can ever render.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { rewardStatusView } from "../features/submissions/rewardView";

const NOW = Date.parse("2026-09-21T12:00:00Z");

function claim(overrides: Record<string, unknown> = {}) {
  return {
    status: "CLAIMED",
    base_reward_points_snapshot: 160,
    deadline_at: "2026-09-21T20:00:00Z",
    grace_deadline_at: "2026-09-22T20:00:00Z",
    ...overrides,
  };
}

/** No tier machinery anywhere in any line (the never-compute pin). */
function assertNoClientMath(view: { lines: string[] }): void {
  for (const line of view.lines) {
    assert.doesNotMatch(line, /%/, "no percentage may render");
    assert.doesNotMatch(line, /档位\s*\d/, "no client tier figure");
  }
}

describe("server-value-only pin", () => {
  test("pre-deadline: the snapshot renders verbatim for any value", () => {
    for (const snapshot of [160, 200, 20, 1, 9999]) {
      const view = rewardStatusView(claim({ base_reward_points_snapshot: snapshot }), NOW);
      assert.equal(view.lines[0], `当前可获得 ${snapshot} 积分`);
      assertNoClientMath(view);
    }
  });

  test("the number is the snapshot itself, never a ladder multiple", () => {
    // A 0.8x client ladder would turn 200 into 160 — pin the verbatim value.
    const view = rewardStatusView(claim({ base_reward_points_snapshot: 200 }), NOW);
    assert.equal(view.lines[0], "当前可获得 200 积分");
    assert.ok(!view.lines[0]!.includes("160"));
  });

  test("overdue within grace: NO invented number (decay is server settlement)", () => {
    const view = rewardStatusView(claim(), Date.parse("2026-09-21T21:00:00Z"));
    assert.equal(
      view.lines[0],
      "已超过截止时间，当前仍可获得积分（以提交时结算为准）",
    );
    assert.ok(!/\d+\s*积分/.test(view.lines[0]!));
  });

  test("past grace: the window is closed", () => {
    const view = rewardStatusView(claim(), Date.parse("2026-09-23T00:00:00Z"));
    assert.equal(view.lines[0], "提交窗口已关闭");
  });
});

describe("state-specific copy", () => {
  test("REVISION_REQUIRED: preserved lock + verbatim snapshot (§11.3/§42)", () => {
    const view = rewardStatusView(claim({ status: "REVISION_REQUIRED" }), NOW);
    assert.equal(view.lines[0], "老师已退回修改，奖励档位已保留");
    assert.equal(view.lines[1], "当前仍可获得 160 积分");
    assert.equal(view.tone, "warning");
    assertNoClientMath(view);
  });

  test("UNDER_REVIEW points at the review, not a number", () => {
    const view = rewardStatusView(claim({ status: "UNDER_REVIEW" }), NOW);
    assert.deepEqual(view.lines, ["已提交，等待老师审核，积分以审核结果为准"]);
  });

  test("COMPLETED", () => {
    const view = rewardStatusView(claim({ status: "COMPLETED" }), NOW);
    assert.deepEqual(view.lines, ["任务已完成，积分已发放"]);
    assert.equal(view.tone, "success");
  });

  test("ABANDONED / EXPIRED are quiet", () => {
    assert.deepEqual(rewardStatusView(claim({ status: "ABANDONED" }), NOW).lines, [
      "任务未完成，未获得积分",
    ]);
    assert.deepEqual(rewardStatusView(claim({ status: "EXPIRED" }), NOW).lines, [
      "任务未完成，未获得积分",
    ]);
  });

  test("VALIDATING explains the in-flight state", () => {
    const view = rewardStatusView(claim({ status: "VALIDATING" }), NOW);
    assert.deepEqual(view.lines, ["正在校验刚提交的文件，请稍候"]);
  });
});
