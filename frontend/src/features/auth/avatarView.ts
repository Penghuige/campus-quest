/**
 * Defect #4 (QA 2026-09-30) — avatar pure derivations (the avatar
 * proposal D1–D6 contract, backend half in flight on
 * avatar-profile-backend).
 *
 * The client checks here are CONVENIENCE pre-checks only — the server
 * re-validates size and sniffs the magic bytes (D2), so a spoofed
 * content type never gets past POST /me/avatar. The capability gate
 * (`avatarSupported`) keeps the whole avatar section unrendered until
 * the backend merges `has_avatar` into MePublic and the schema is
 * regenerated: absent/false-y field = hidden, real boolean = live.
 * That is feature detection on the live contract, not a stub.
 */

import { ApiError } from "@/lib/errors";

/** D2: hard upload ceiling. */
export const AVATAR_MAX_BYTES = 2 * 1024 * 1024;

/** D2: accepted upload types (server checks magic numbers, not names). */
export const AVATAR_ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

/**
 * D2 "frontend crops to square before upload": the crop also downsizes
 * — larger sources are drawn to this square canvas edge so an 8MP
 * photo cannot become a 2MB-payload failure every time.
 */
export const AVATAR_DOWNSCALE_TARGET_PX = 512;

/**
 * Convenience pre-check: null = looks acceptable, a string = the
 * user-facing problem. Mirrors uploadFlow's picker pre-check idiom.
 */
export function validateAvatarFile(file: { size: number; type: string }): string | null {
  if (!AVATAR_ACCEPTED_TYPES.includes(file.type as (typeof AVATAR_ACCEPTED_TYPES)[number])) {
    return "头像仅支持 PNG、JPEG、WebP 图片";
  }
  if (file.size > AVATAR_MAX_BYTES) {
    return "头像文件过大（最大 2 MB）";
  }
  return null;
}

export interface CropRect {
  /** Top-left of the centered max-square, in source pixels. */
  sx: number;
  sy: number;
  /** The square's edge length in source pixels. */
  size: number;
}

/**
 * Center-crop math for the square avatar: the largest centered square
 * of the source. Pure so the crop can be unit-pinned (the canvas call
 * itself is a thin wrapper over these numbers).
 */
export function squareCropRect(width: number, height: number): CropRect {
  if (width <= 0 || height <= 0) {
    throw new RangeError(`squareCropRect needs positive dimensions, got ${width}x${height}`);
  }
  const size = Math.min(width, height);
  return { sx: Math.floor((width - size) / 2), sy: Math.floor((height - size) / 2), size };
}

/** The square canvas edge a source of `sourceSize` renders onto. */
export function avatarDisplaySize(sourceSize: number): number {
  return Math.min(sourceSize, AVATAR_DOWNSCALE_TARGET_PX);
}

type MaybeAvatarFlag = { has_avatar?: unknown };

/**
 * The capability gate: the section renders only when the /me payload
 * actually carries a boolean has_avatar — i.e. the backend avatar PR
 * is deployed. Absent or non-boolean keeps the section hidden.
 */
export function avatarSupported(me: object): boolean {
  return "has_avatar" in me && typeof (me as MaybeAvatarFlag).has_avatar === "boolean";
}

/**
 * Upload/delete failure copy. Exact business codes arrive with the
 * backend avatar PR; the rate limit (D2: one change per 10 minutes)
 * and the size ceiling are matched defensively on the code, everything
 * else shows the server's own message (§29: switch on code, never
 * parse the Chinese message — the two known-shaped cases only).
 */
export function avatarErrorText(cause: unknown): string {
  if (cause instanceof ApiError) {
    if (/RATE_LIMIT/i.test(cause.code)) {
      return "头像更换过于频繁，请 10 分钟后再试。";
    }
    if (/PAYLOAD_TOO_LARGE|FILE_TOO_LARGE|REQUEST_ENTITY_TOO_LARGE/i.test(cause.code)) {
      return "头像文件过大（最大 2 MB）。";
    }
    return cause.message;
  }
  return "头像操作失败，请稍后重试。";
}
