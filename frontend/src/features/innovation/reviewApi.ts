import { apiRequest, type ApiRequestInit } from "@/lib/api";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

type Schema = components["schemas"];
export type WorkflowDto = Schema["AchievementWorkflowResponse"];
export type EvidenceDto = Schema["EvidenceResponse"];
export type RevisionCommand = Schema["SavedRevisionCommand"];
export type WithdrawalCommand = Schema["WithdrawCommand"];
export type ReviewItemDto = Schema["ReviewQueueItem"];
export type ReviewDetailDto = Schema["ReviewPrivateDetail"];
export type DecisionCommand = Schema["ReviewDecisionCommand"];
export type PublicAchievementDto = Schema["PublicAchievementResponse"];
const encoded = encodeURIComponent;
const ownerPath = (p: string, a: string) => `/api/v1/ie/me/project-drafts/${encoded(p)}/achievements/${encoded(a)}`;
const opsPath = (c: string) => `/api/v1/ie/ops/achievement-reviews/${encoded(c)}`;

function assertSession(epoch: number) { if (epoch !== getAuthEpoch()) throw new DOMException("账号会话已改变", "AbortError"); }
async function request<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const epoch = getAuthEpoch(); const result = await apiRequest<T>(path, { cache: "no-store", ...init }); assertSession(epoch); return result;
}
export const getWorkflow = (p: string, a: string) => request<WorkflowDto>(`${ownerPath(p, a)}/workflow`);
export const listEvidence = (p: string, a: string) => request<Schema["EvidenceListResponse"]>(`${ownerPath(p, a)}/evidence`);
export const completeEvidence = (p: string, a: string, e: string) => request<EvidenceDto>(`${ownerPath(p, a)}/evidence/${encoded(e)}/complete`, { method: "POST" });
export const removeEvidence = (p: string, a: string, e: string) => request<void>(`${ownerPath(p, a)}/evidence/${encoded(e)}`, { method: "DELETE" });
export const readEvidence = (p: string, a: string, e: string) => request<Blob>(`${ownerPath(p, a)}/evidence/${encoded(e)}/content`, { responseFormat: "blob", cache: "no-store" });
export const submitRevision = (p: string, a: string, body: RevisionCommand) => request<WorkflowDto>(`${ownerPath(p, a)}/submit`, { method: "POST", body });
export const submitUpdateRevision = (p: string, a: string, body: RevisionCommand) => request<WorkflowDto>(`${ownerPath(p, a)}/submit-update`, { method: "POST", body });
export const withdrawReview = (p: string, a: string, body: WithdrawalCommand) => request<WorkflowDto>(`${ownerPath(p, a)}/withdraw`, { method: "POST", body });
export const listReviewQueue = (offset = 0) => request<Schema["app__modules__innovation__review_schemas__ReviewQueueResponse"]>(`/api/v1/ie/ops/achievement-reviews?limit=20&offset=${offset}`);
export const claimReview = (c: string, version: number) => request<Schema["ReviewCaseSummary"]>(`${opsPath(c)}/claim`, { method: "POST", body: { version } });
export const declareConflict = (c: string, version: number) => request<void>(`${opsPath(c)}/conflict`, { method: "POST", body: { version } });
export const getReviewDetail = (c: string) => request<ReviewDetailDto>(opsPath(c), { cache: "no-store" });
export const decideReview = (c: string, body: DecisionCommand) => request<Schema["ReviewCaseSummary"]>(`${opsPath(c)}/decision`, { method: "POST", body });
export const readReviewEvidence = (c: string, e: string) => request<Blob>(`${opsPath(c)}/evidence/${encoded(e)}/content`, { responseFormat: "blob", cache: "no-store" });
export const listPublicAchievements = (offset = 0) => request<Schema["PublicAchievementListResponse"]>(`/api/v1/ie/achievements?limit=20&offset=${offset}`);
export const getPublicAchievement = (a: string) => request<PublicAchievementDto>(`/api/v1/ie/achievements/${encoded(a)}`);

/** Signed intent is transient. It is never cached, logged or attached to DOM. */
export async function uploadEvidence(p: string, a: string, file: Blob, requestId: string): Promise<EvidenceDto> {
  const epoch = getAuthEpoch();
  const intent = await request<Schema["EvidenceIntentResponse"]>(`${ownerPath(p, a)}/evidence`, { method: "POST", body: { request_id: requestId, content_type: file.type, size: file.size } });
  assertSession(epoch);
  if (intent.pinned_content_length !== file.size) throw new Error("上传大小与服务端约定不一致，请重新选择文件。");
  const response = await fetch(intent.upload_url, { method: "PUT", headers: intent.client_headers, body: file, credentials: "omit", redirect: "error" });
  assertSession(epoch);
  // Write-once replay: the check endpoint, rather than this PUT, proves readiness.
  if (!response.ok && response.status !== 412) throw new Error("文件上传尚未确认，请重试原文件或移除此上传意向。");
  return completeEvidence(p, a, intent.evidence.id);
}

export function saveProofBlob(file: Blob) {
  const extension = file.type.includes("pdf") ? "pdf" : file.type.includes("png") ? "png" : "jpg";
  const url = URL.createObjectURL(file);
  const link = document.createElement("a"); link.href = url; link.download = `evidence.${extension}`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
}
