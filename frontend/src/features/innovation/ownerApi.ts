import { apiRequest } from "@/lib/api";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

export type OwnerProfileDto = components["schemas"]["OwnerProfileResponse"];
export type OwnerProfileReadDto = components["schemas"]["OwnerProfileReadResponse"];
export type OwnerFields = Pick<OwnerProfileDto, "name" | "student_no" | "major" | "grade">;
const PATH = "/api/v1/ie/me/owner-profile";

async function inOwnerSession<T>(request: () => Promise<T>): Promise<T> {
  const epoch = getAuthEpoch();
  const response = await request();
  if (getAuthEpoch() !== epoch) throw new DOMException("账号会话已改变", "AbortError");
  return response;
}

export function getOwnerProfile(): Promise<OwnerProfileReadDto> {
  return inOwnerSession(() => apiRequest<OwnerProfileReadDto>(PATH));
}

export function saveOwnerProfile(fields: OwnerFields, version: number): Promise<OwnerProfileDto> {
  return inOwnerSession(() => apiRequest<OwnerProfileDto>(PATH, { method: "PUT", body: { ...fields, version } }));
}
