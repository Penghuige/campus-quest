import { apiRequest, type ApiRequestInit } from "@/lib/api";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

type Schema = components["schemas"];
export type RecruitmentDto = Schema["RecruitmentDraftResponse"];
export type RecruitmentFieldsDto = Schema["RecruitmentFields"];
export type RecruitmentActionDto = Schema["RecruitmentActionResult"];
export type PublicRecruitmentDto = Schema["PublicRecruitmentResponse"];
export type ContactChannel = Schema["ContactInput"]["channel"];
export type VersionCommand = Schema["RecruitmentVersionCommand"];
export type RevokeCommand = Schema["RevokeRecruitmentContactCommand"];
const encoded = encodeURIComponent;
const ownerPath = (p: string, r?: string) => `/api/v1/ie/me/project-drafts/${encoded(p)}/recruitments${r ? `/${encoded(r)}` : ""}`;

async function request<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const epoch = getAuthEpoch();
  const result = await apiRequest<T>(path, { signal: AbortSignal.timeout(15_000), ...init, cache: "no-store" });
  if (epoch !== getAuthEpoch()) throw new DOMException("账号会话已改变", "AbortError");
  return result;
}
export const createRecruitment = (p: string, body: Schema["RecruitmentDraftCreate"]) => request<RecruitmentDto>(ownerPath(p), { method: "POST", body });
export const listRecruitments = (p: string, offset = 0) => request<Schema["RecruitmentDraftListResponse"]>(`${ownerPath(p)}?limit=20&offset=${offset}`);
export const getRecruitment = (p: string, r: string) => request<RecruitmentDto>(ownerPath(p, r));
export const updateRecruitment = (p: string, r: string, body: Schema["RecruitmentDraftUpdate"]) => request<RecruitmentDto>(ownerPath(p, r), { method: "PATCH", body });
export const publishRecruitment = (p: string, r: string, body: VersionCommand) => request<RecruitmentActionDto>(`${ownerPath(p, r)}/publish`, { method: "POST", body });
export const closeRecruitment = (p: string, r: string, body: VersionCommand) => request<RecruitmentActionDto>(`${ownerPath(p, r)}/close`, { method: "POST", body });
export const reopenRecruitment = (p: string, r: string, body: VersionCommand) => request<RecruitmentActionDto>(`${ownerPath(p, r)}/reopen`, { method: "POST", body });
export const confirmActiveRecruitment = (p: string, r: string, body: VersionCommand) => request<RecruitmentActionDto>(`${ownerPath(p, r)}/confirm-active`, { method: "POST", body });
export const revokeRecruitmentContact = (p: string, r: string, body: RevokeCommand) => request<RecruitmentActionDto>(`${ownerPath(p, r)}/revoke-contact`, { method: "POST", body });
export const getPublicProjects = (params = new URLSearchParams()) => request<Schema["PublicProjectListResponse"]>(`/api/v1/ie/projects?${params}`);
export const getPublicProject = (id: string, params = new URLSearchParams()) => request<Schema["PublicProjectDetailResponse"]>(`/api/v1/ie/projects/${encoded(id)}?${params}`);
export const getPublicRecruitments = (params = new URLSearchParams()) => request<Schema["PublicRecruitmentListResponse"]>(`/api/v1/ie/recruitments?${params}`);
export const getPublicRecruitment = (id: string) => request<PublicRecruitmentDto>(`/api/v1/ie/recruitments/${encoded(id)}`);
export const getRecruitmentContacts = (id: string) => request<Schema["RecruitmentContactsResponse"]>(`/api/v1/ie/recruitments/${encoded(id)}/contacts`);
