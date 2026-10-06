/**
 * Defect #4 (QA 2026-09-30) — profile tab parsing: the "我" page gains
 * a 个人信息 subpage via the repo's URL-state tab pattern (patterns §4;
 * the rankings/notifications precedent). Pure derivations only.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  DEFAULT_PROFILE_TAB,
  parseProfileTab,
  PROFILE_TABS,
} from "../features/profile/profileTabs";

describe("profile tabs (URL-state subpage, patterns §4)", () => {
  test("exactly two tabs in display order: growth first, info second", () => {
    assert.deepEqual(PROFILE_TABS, [
      { key: "growth", label: "我的档案" },
      { key: "info", label: "个人信息" },
    ]);
  });

  test("the landing tab stays 我的档案 (zero-surprise default)", () => {
    assert.equal(DEFAULT_PROFILE_TAB, "growth");
  });

  test("undefined (bare /profile) parses to the default", () => {
    assert.equal(parseProfileTab(undefined), "growth");
  });

  test("known keys pass through", () => {
    assert.equal(parseProfileTab("info"), "info");
    assert.equal(parseProfileTab("growth"), "growth");
  });

  test("array form (repeated params) keeps the FIRST value", () => {
    assert.equal(parseProfileTab(["info", "growth"]), "info");
    assert.equal(parseProfileTab(["garbage", "info"]), "growth");
  });

  test("garbage degrades to the default, never throws", () => {
    for (const garbage of ["", "personal", "INFO", "null", "?tab=info"]) {
      assert.equal(parseProfileTab(garbage), "growth", JSON.stringify(garbage));
    }
  });
});
