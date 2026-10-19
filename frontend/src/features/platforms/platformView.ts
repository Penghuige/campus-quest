/**
 * The platform registry's display layer (platform-registry spec
 * 2026-10-16 §3/§4; the frontend half) — pure decisions + the
 * hand-written contract types the backend PR will replace with
 * generated aliases (merge-time discipline, PR #72).
 *
 * - the student-facing rule: wherever a claim/assignment carries the
 *   platform CODE, the UI renders the registry's display_name; the
 *   dictionary is enabled-only, read-after-login, cached like every
 *   other dictionary (the dataCache useSection pattern);
 * - fallback: a code the dictionary does not carry (an older session's
 *   snapshot, a just-disabled platform an existing claim still names)
 *   renders the RAW CODE — never a blank, never a wrong name;
 * - admin rows carry the full shape (code immutable, display_name /
 *   sort_order / enabled editable — no delete; a referenced platform
 *   only ever toggles).
 */
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** The student dictionary row (the generated §3 contract: enabled-only, read-only). */
export type PlatformDto = Schemas["PlatformPublic"];

/** The admin row (the generated admin contract: lifecycle flag + audit instants). */
export type AdminPlatformDto = Schemas["PlatformAdminPublic"];

export type PlatformDictionary = readonly PlatformDto[];

/** code -> display_name lookup table (built once per dictionary). */
export function platformDisplayMap(
  dictionary: PlatformDictionary,
): ReadonlyMap<string, string> {
  return new Map(dictionary.map((row) => [row.code, row.display_name]));
}

/** Render name for a platform code: the registry name, else the code. */
export function platformDisplayName(
  code: string,
  names: ReadonlyMap<string, string>,
): string {
  return names.get(code) ?? code;
}

/** Client band for the create form's sort order (spec §4: int). */
export const PLATFORM_SORT_MIN = 0;
export const PLATFORM_SORT_MAX = 9999;

/** Validate the create form's code: stable ASCII slug, immutable. */
export function validatePlatformCode(value: string): string | undefined {
  const trimmed = value.trim();
  if (trimmed.length === 0 || trimmed.length > 64) {
    return "请输入 1-64 个字符的平台代码";
  }
  if (!/^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/.test(trimmed)) {
    return "仅小写字母、数字与连字符，且不以连字符开头或结尾";
  }
  return undefined;
}

/** Validate a display name: non-empty, human-length. */
export function validatePlatformDisplayName(value: string): string | undefined {
  const trimmed = value.trim();
  if (trimmed.length === 0 || trimmed.length > 32) {
    return "请输入 1-32 个字符的显示名";
  }
  return undefined;
}

/** Validate the sort order: the int band, client-mirrored. */
export function validatePlatformSortOrder(value: string): string | undefined {
  const trimmed = value.trim();
  if (!/^-?\d+$/.test(trimmed)) {
    return "请输入整数";
  }
  const parsed = Number(trimmed);
  if (parsed < PLATFORM_SORT_MIN || parsed > PLATFORM_SORT_MAX) {
    return `排序值需在 ${PLATFORM_SORT_MIN}-${PLATFORM_SORT_MAX} 之间`;
  }
  return undefined;
}

/** The admin mutation-error view (the shared admin envelope pattern). */
export interface PlatformAdminErrorView {
  message: string;
  requestId: string | null;
}

import { COPY_GENERIC_FAIL, COPY_NETWORK_FAIL, COPY_SERVICE_FAIL, isApiError } from "@/lib/errors";

export function describePlatformAdminError(
  error: unknown,
  fallback: string,
): PlatformAdminErrorView {
  if (!isApiError(error)) {
    return { message: COPY_NETWORK_FAIL, requestId: null };
  }
  if (error.code === "CONFLICT") {
    return {
      message: `${error.message || fallback}（与当前状态冲突：平台代码可能已存在，请刷新后重试）`,
      requestId: error.requestId,
    };
  }
  return { message: error.message || fallback, requestId: error.requestId };
}
