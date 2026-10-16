/**
 * Platform registry view pins (platform-registry spec 2026-10-16
 * §3/§4; TDD red-first): the display-name fallback rule, the admin
 * form validation bands, and the code-shape rule (stable ASCII slug —
 * immutable by contract).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  platformDisplayMap,
  platformDisplayName,
  validatePlatformCode,
  validatePlatformDisplayName,
  validatePlatformSortOrder,
  type PlatformDto,
} from "../features/platforms/platformView";

const DICT: PlatformDto[] = [
  { code: "xiaohongshu", display_name: "小红书", sort_order: 1 },
  { code: "boss-zhipin", display_name: "BOSS直聘", sort_order: 2 },
  { code: "zhaopin", display_name: "智联招聘", sort_order: 3 },
];

describe("platformDisplayName (the student-facing rule)", () => {
  const names = platformDisplayMap(DICT);

  test("registry codes render their display_name", () => {
    assert.equal(platformDisplayName("xiaohongshu", names), "小红书");
    assert.equal(platformDisplayName("boss-zhipin", names), "BOSS直聘");
  });

  test("unknown codes fall back to the RAW code — never blank, never a guess", () => {
    // An existing claim may name a just-disabled platform the enabled-only
    // dictionary no longer carries; the honest rendering is the code.
    assert.equal(platformDisplayName("lietou", names), "lietou");
  });

  test("the empty dictionary degrades to codes everywhere", () => {
    const empty = platformDisplayMap([]);
    assert.equal(platformDisplayName("douyin", empty), "douyin");
  });
});

describe("validatePlatformCode (the immutable stable slug)", () => {
  test("accepts the seeded shapes", () => {
    for (const code of ["boss-zhipin", "58city", "zhaopin", "a", "z2"]) {
      assert.equal(validatePlatformCode(code), undefined, code);
    }
  });

  test("rejects empty, overlong, and non-slug shapes with actionable copy", () => {
    assert.ok(validatePlatformCode("") !== undefined);
    assert.ok(validatePlatformCode("x".repeat(65)) !== undefined);
    assert.ok(validatePlatformCode("BOSS") !== undefined); // uppercase
    assert.ok(validatePlatformCode("-boss") !== undefined); // leading dash
    assert.ok(validatePlatformCode("boss-") !== undefined); // trailing dash
    assert.ok(validatePlatformCode("boss zhipin") !== undefined); // space
  });

  test("trims before validating", () => {
    assert.equal(validatePlatformCode("  boss-zhipin  "), undefined);
  });
});

describe("validatePlatformDisplayName / sortOrder bands", () => {
  test("display name: non-empty 1-32 chars", () => {
    assert.equal(validatePlatformDisplayName("BOSS直聘"), undefined);
    assert.ok(validatePlatformDisplayName("") !== undefined);
    assert.ok(validatePlatformDisplayName("   ") !== undefined);
    assert.ok(validatePlatformDisplayName("名".repeat(33)) !== undefined);
  });

  test("sort order: integers in band", () => {
    assert.equal(validatePlatformSortOrder("0"), undefined);
    assert.equal(validatePlatformSortOrder("42"), undefined);
    assert.equal(validatePlatformSortOrder("9999"), undefined);
    assert.ok(validatePlatformSortOrder("abc") !== undefined);
    assert.ok(validatePlatformSortOrder("-1") !== undefined);
    assert.ok(validatePlatformSortOrder("10000") !== undefined);
  });
});
