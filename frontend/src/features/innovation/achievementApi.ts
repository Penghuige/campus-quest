import { apiRequest } from "@/lib/api";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

export type AchievementDraftDto = components["schemas"]["AchievementDraftResponse"];
export type AchievementPageDto = components["schemas"]["AchievementDraftListResponse"];
export type AchievementFields = Pick<AchievementDraftDto, "title" | "description" | "work_url" | "award_text">;

function path(projectId: string) {
  return `/api/v1/ie/me/project-drafts/${encodeURIComponent(projectId)}/achievements`;
}

async function inSession<T>(request: () => Promise<T>): Promise<T> {
  const epoch = getAuthEpoch();
  const result = await request();
  if (getAuthEpoch() !== epoch) throw new DOMException("账号会话已改变", "AbortError");
  return result;
}

export function listAchievementDrafts(projectId: string, query: { limit?: number; offset?: number } = {}): Promise<AchievementPageDto> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  return inSession(() => apiRequest<AchievementPageDto>(`${path(projectId)}${params.size ? `?${params}` : ""}`));
}

export function getAchievementDraft(projectId: string, id: string): Promise<AchievementDraftDto> {
  return inSession(() => apiRequest<AchievementDraftDto>(`${path(projectId)}/${encodeURIComponent(id)}`));
}

export function createAchievementDraft(projectId: string, fields: AchievementFields, requestId: string): Promise<AchievementDraftDto> {
  return inSession(() => apiRequest<AchievementDraftDto>(path(projectId), { method: "POST", body: { ...fields, request_id: requestId } }));
}

export function updateAchievementDraft(projectId: string, id: string, fields: AchievementFields, version: number): Promise<AchievementDraftDto> {
  return inSession(() => apiRequest<AchievementDraftDto>(`${path(projectId)}/${encodeURIComponent(id)}`, { method: "PATCH", body: { ...fields, version } }));
}
