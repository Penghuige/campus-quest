/**
 * Platform registry API wrappers (platform-registry spec §3/§4) —
 * contract-first: the shapes are hand-written from the frozen spec
 * until the backend PR's OpenAPI regen lands (then they flip to
 * generated aliases, the merge-time discipline).
 *
 * Two surfaces:
 * - the student dictionary (GET /platforms — enabled-only, read-after-
 *   login; consumed via platformDictionary's cache);
 * - the admin CRUD (GET/POST /admin/platforms, PATCH by code; every
 *   mutation is audited server-side, code is immutable, no delete).
 */
import { apiRequest } from "@/lib/api";

import type { AdminPlatformDto, PlatformDto } from "./platformView";

/** The student dictionary, server-sorted by sort_order (spec §3). */
export function listPlatforms(): Promise<PlatformDto[]> {
  return apiRequest<PlatformDto[]>("/api/v1/platforms");
}

/** The admin listing: EVERY row, disabled included (spec §4). */
export function listAdminPlatforms(): Promise<AdminPlatformDto[]> {
  return apiRequest<AdminPlatformDto[]>("/api/v1/admin/platforms");
}

/** Create one platform (code immutable from here on; audited). */
export function createAdminPlatform(body: {
  code: string;
  display_name: string;
  sort_order: number;
}): Promise<AdminPlatformDto> {
  return apiRequest<AdminPlatformDto>("/api/v1/admin/platforms", {
    method: "POST",
    body,
  });
}

/**
 * Edit one platform: presence-based partial (absent = unchanged) —
 * display_name / sort_order / enabled only; the code is the path key.
 */
export function updateAdminPlatform(
  code: string,
  body: { display_name?: string; sort_order?: number; enabled?: boolean },
): Promise<AdminPlatformDto> {
  return apiRequest<AdminPlatformDto>(
    `/api/v1/admin/platforms/${encodeURIComponent(code)}`,
    { method: "PATCH", body },
  );
}
