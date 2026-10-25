import { apiRequest } from "@/lib/api";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

export type OperationsGrantDto = components["schemas"]["OperationsGrantResponse"];
export type OperationsGrantSave = components["schemas"]["OperationsGrantSave"];
export type InnovationCapabilitiesDto = components["schemas"]["InnovationCapabilitiesResponse"];

async function inCurrentSession<T>(request: () => Promise<T>): Promise<T> {
  const epoch = getAuthEpoch();
  const result = await request();
  if (epoch !== getAuthEpoch()) throw new DOMException("账号会话已改变", "AbortError");
  return result;
}

export function getInnovationCapabilities(): Promise<InnovationCapabilitiesDto> {
  return inCurrentSession(() => apiRequest<InnovationCapabilitiesDto>("/api/v1/ie/me/capabilities"));
}

export function getOperationsGrant(userId: string): Promise<OperationsGrantDto> {
  return inCurrentSession(() => apiRequest<OperationsGrantDto>(`/api/v1/admin/ie/operations-grants/${encodeURIComponent(userId)}`));
}

export function saveOperationsGrant(userId: string, body: OperationsGrantSave): Promise<OperationsGrantDto> {
  return inCurrentSession(() => apiRequest<OperationsGrantDto>(`/api/v1/admin/ie/operations-grants/${encodeURIComponent(userId)}`, { method: "PUT", body }));
}
