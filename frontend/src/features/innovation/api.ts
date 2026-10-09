import { apiRequest } from "@/lib/api";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];
export type ProjectDraftDto = Schemas["ProjectDraftResponse"];
export type ProjectDraftPageDto = Schemas["ProjectDraftListResponse"];
export type DraftFields = Pick<ProjectDraftDto, "title" | "summary" | "direction" | "stage" | "team_status">;

const PATH = "/api/v1/ie/me/project-drafts";

// Private reads and mutation responses must not enter a later account's UI.
async function inDraftSession<T>(request: () => Promise<T>): Promise<T> {
  const epoch = getAuthEpoch();
  const result = await request();
  if (getAuthEpoch() !== epoch) throw new DOMException("账号会话已改变", "AbortError");
  return result;
}

export function listProjectDrafts(query: { limit?: number; offset?: number; signal?: AbortSignal } = {}): Promise<ProjectDraftPageDto> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  const suffix = params.toString();
  return inDraftSession(() => apiRequest<ProjectDraftPageDto>(`${PATH}${suffix ? `?${suffix}` : ""}`, { signal: query.signal }));
}

export function getProjectDraft(id: string): Promise<ProjectDraftDto> {
  return inDraftSession(() => apiRequest<ProjectDraftDto>(`${PATH}/${encodeURIComponent(id)}`));
}

export function createProjectDraft(fields: DraftFields, requestId: string): Promise<ProjectDraftDto> {
  return inDraftSession(() => apiRequest<ProjectDraftDto>(PATH, { method: "POST", body: { ...fields, request_id: requestId } }));
}

export function updateProjectDraft(id: string, fields: DraftFields, version: number): Promise<ProjectDraftDto> {
  return inDraftSession(() => apiRequest<ProjectDraftDto>(`${PATH}/${encodeURIComponent(id)}`, { method: "PATCH", body: { ...fields, version } }));
}
