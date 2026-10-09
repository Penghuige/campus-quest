import { apiRequest } from "@/lib/api";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

export type OwnerQualificationDto = components["schemas"]["QualificationState"];
export type OwnerQualificationSummaryDto = components["schemas"]["QualificationQueueItem"];
export type OwnerQualificationPageDto = components["schemas"]["QualificationQueue"];
export type OwnerQualificationDetailDto = components["schemas"]["QualificationDetail"];

const SELF_PATH = "/api/v1/ie/me/owner-qualification";
const ADMIN_PATH = "/api/v1/admin/ie/owner-qualifications";

async function inQualificationSession<T>(request: () => Promise<T>): Promise<T> {
  const epoch = getAuthEpoch();
  const result = await request();
  if (epoch !== getAuthEpoch()) throw new DOMException("账号会话已改变", "AbortError");
  return result;
}

export function getOwnerQualification(): Promise<OwnerQualificationDto> {
  return inQualificationSession(() => apiRequest<OwnerQualificationDto>(SELF_PATH, { cache: "no-store" }));
}

export function applyOwnerQualification(version: number, profileVersion: number): Promise<OwnerQualificationDto> {
  const body: components["schemas"]["QualificationApply"] = { version, profile_version: profileVersion };
  return inQualificationSession(() => apiRequest<OwnerQualificationDto>(SELF_PATH, { method: "POST", body }));
}

export function listOwnerQualificationApplications(query: { limit?: number; offset?: number } = {}): Promise<OwnerQualificationPageDto> {
  const params = new URLSearchParams({ limit: String(query.limit ?? 20), offset: String(query.offset ?? 0) });
  return inQualificationSession(() => apiRequest<OwnerQualificationPageDto>(`${ADMIN_PATH}?${params}`, { cache: "no-store" }));
}

export function getOwnerQualificationApplication(userId: string): Promise<OwnerQualificationDetailDto> {
  return inQualificationSession(() => apiRequest<OwnerQualificationDetailDto>(`${ADMIN_PATH}/${encodeURIComponent(userId)}`, { cache: "no-store" }));
}

export function approveOwnerQualification(userId: string, version: number): Promise<OwnerQualificationDto> {
  const body: components["schemas"]["QualificationApprove"] = { version };
  return inQualificationSession(() => apiRequest<OwnerQualificationDto>(`${ADMIN_PATH}/${encodeURIComponent(userId)}/approve`, { method: "POST", body }));
}
