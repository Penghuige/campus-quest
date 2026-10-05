/**
 * Defect #4 (QA 2026-09-30) — avatar view derivations: client-side
 * pre-checks (convenience only, the server re-validates by magic
 * number per the avatar proposal D2), the square-crop math, and the
 * capability gate that keeps the whole avatar section unrendered until
 * the backend merges has_avatar into MePublic (schema regen flips the
 * gate; before that the section must NOT render — no silent no-ops).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  AVATAR_ACCEPTED_TYPES,
  AVATAR_DOWNSCALE_TARGET_PX,
  AVATAR_MAX_BYTES,
  avatarErrorText,
  avatarSupported,
  squareCropRect,
  validateAvatarSize,
  validateAvatarType,
} from "../features/auth/avatarView";
import { ApiError } from "../lib/errors";

describe("avatar pre-checks (D2 contract: <=2MB, png/jpeg/webp)", () => {
  test("the policy constants mirror the contract", () => {
    assert.equal(AVATAR_MAX_BYTES, 2 * 1024 * 1024);
    assert.deepEqual(AVATAR_ACCEPTED_TYPES, ["image/png", "image/jpeg", "image/webp"]);
    assert.equal(AVATAR_DOWNSCALE_TARGET_PX, 512);
  });

  test("the TYPE gate passes accepted types on the raw pick", () => {
    for (const type of ["image/png", "image/jpeg", "image/webp"]) {
      assert.equal(validateAvatarType({ type }), null, type);
    }
  });

  test("the TYPE gate names the accepted set on anything else", () => {
    for (const type of ["image/gif", "image/svg+xml", "video/mp4", ""]) {
      assert.match(validateAvatarType({ type })!, /PNG|JPEG|WebP/, type);
    }
  });

  test("the SIZE gate runs on the CROPPED bytes (r1 review rationale)", () => {
    // At the ceiling passes; over it gets the limit copy. A large RAW
    // photo is deliberately NOT this gate's input — the crop may
    // legally shrink it under the ceiling before this ever runs.
    assert.equal(validateAvatarSize(AVATAR_MAX_BYTES), null);
    assert.match(validateAvatarSize(AVATAR_MAX_BYTES + 1)!, /2\s*MB/);
    assert.match(validateAvatarSize(8 * 1024 * 1024)!, /2\s*MB/);
  });
});

describe("square-crop math (center max-square, D2: crop then upload)", () => {
  test("landscape keeps the centered square", () => {
    assert.deepEqual(squareCropRect(1000, 600), { sx: 200, sy: 0, size: 600 });
  });

  test("portrait keeps the centered square", () => {
    assert.deepEqual(squareCropRect(600, 1000), { sx: 0, sy: 200, size: 600 });
  });

  test("already-square sources pass through", () => {
    assert.deepEqual(squareCropRect(800, 800), { sx: 0, sy: 0, size: 800 });
  });

  test("non-positive dimensions are rejected, not math-ed", () => {
    assert.throws(() => squareCropRect(0, 100), /positive/);
    assert.throws(() => squareCropRect(100, -1), /positive/);
  });
});

describe("the capability gate (has_avatar lands with the backend PR)", () => {
  test("a MePublic WITHOUT the field keeps the section hidden", () => {
    assert.equal(avatarSupported({ id: "u", nickname: "同学" }), false);
  });

  test("a non-boolean has_avatar does not open the gate", () => {
    assert.equal(avatarSupported({ has_avatar: "yes" }), false);
    assert.equal(avatarSupported({ has_avatar: 1 }), false);
    assert.equal(avatarSupported({ has_avatar: null }), false);
  });

  test("a real boolean has_avatar opens the gate either way", () => {
    assert.equal(avatarSupported({ has_avatar: false }), true);
    assert.equal(avatarSupported({ has_avatar: true }), true);
  });
});

describe("avatar error copy (§29: code branching, never message parsing)", () => {
  const apiError = (code: string, message: string) =>
    new ApiError({ code, message, status: 400 });

  test("rate-limited changes get the 10-minute copy", () => {
    // RATE_LIMITED is the backend's real frozen code (verified by the
    // reviewer against the avatar branch) — not a fictional sample.
    assert.match(avatarErrorText(apiError("RATE_LIMITED", "任意服务端文案")), /10 分钟/);
  });

  test("oversize payloads get the size copy", () => {
    assert.match(
      avatarErrorText(apiError("PAYLOAD_TOO_LARGE", "任意服务端文案")),
      /2 MB/,
    );
  });

  test("other server verdicts pass their own message through", () => {
    assert.equal(
      avatarErrorText(apiError("VALIDATION_ERROR", "图片格式不受支持")),
      "图片格式不受支持",
    );
  });

  test("non-ApiError failures get the generic retry copy", () => {
    assert.equal(avatarErrorText(new Error("network")), "头像操作失败，请稍后重试。");
    assert.equal(avatarErrorText("weird"), "头像操作失败，请稍后重试。");
  });
});
